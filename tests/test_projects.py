import pytest

from olian import hermes
from olian.facade import Actor
from olian.rbac import Forbidden, Role


def test_projects_are_isolated_islands(core):
    a = core.projects.create("alpha", "Alpha", "goal A", {"agent": {"role": "Content", "tone": "warm"}})
    b = core.projects.create("beta", "Beta", "goal B", {"sandbox": {"backend": "docker", "image": "img:b"}})
    assert a["api_port"] != b["api_port"] and a["api_key"] != b["api_key"]
    ha, hb = core.s.hermes_root / "alpha", core.s.hermes_root / "beta"
    assert ha != hb and (ha / "SOUL.md").exists() and (hb / "config.yaml").exists()
    assert "goal A" in (ha / "SOUL.md").read_text() and "goal B" not in (ha / "SOUL.md").read_text()
    assert "image" not in (ha / "config.yaml").read_text() or "img:b" not in (ha / "config.yaml").read_text()
    assert "img:b" in (hb / "config.yaml").read_text()
    env_a, env_b = (ha / ".env").read_text(), (hb / ".env").read_text()
    assert f"API_SERVER_PORT={a['api_port']}" in env_a and f"API_SERVER_PORT={b['api_port']}" in env_b
    assert (ha / ".env").stat().st_mode & 0o077 == 0  # secrets file is private
    tok_a = [l for l in env_a.splitlines() if l.startswith("OLIAN_AGENT_TOKEN=")][0]
    assert tok_a not in env_b


def test_agent_token_is_stable_across_reprovision_and_mcp_wired(core):
    core.projects.create("alpha", "Alpha")
    env = (core.s.hermes_root / "alpha" / ".env")
    before = env.read_text()
    core.projects.provision("alpha")
    core.projects.add_member("alpha", "telegram", "9", "viewer")
    assert [l for l in before.splitlines() if "OLIAN_AGENT_TOKEN" in l] == \
           [l for l in env.read_text().splitlines() if "OLIAN_AGENT_TOKEN" in l]
    cfg = (core.s.hermes_root / "alpha" / "config.yaml").read_text()
    assert "mcp_servers" in cfg and "${OLIAN_AGENT_TOKEN}" in cfg and "/mcp" in cfg
    assert "TELEGRAM_BOT_TOKEN" not in env.read_text()  # Olian owns Telegram by default (per-person RBAC)


def test_members_sync_hermes_allowlist_for_native_telegram(core):
    core.projects.create("alpha", "Alpha", cfg={"channels": {"telegram": {"bot_token": "TKN", "via": "hermes"}}})
    core.projects.add_member("alpha", "telegram", "42", "manager")
    env = (core.s.hermes_root / "alpha" / ".env").read_text()
    assert "TELEGRAM_BOT_TOKEN=TKN" in env and "TELEGRAM_ALLOWED_USERS=42" in env


def test_bad_ids_and_duplicates(core):
    with pytest.raises(ValueError):
        core.projects.create("Bad Id!", "x")
    core.projects.create("ok-1", "x")
    with pytest.raises(ValueError):
        core.projects.create("ok-1", "x")


def test_chat_resolution_by_binding_and_dm(core):
    core.projects.create("alpha", "A"); core.projects.create("beta", "B")
    core.projects.add_member("alpha", "telegram", "1", "admin")
    core.projects.add_member("alpha", "telegram", "2", "viewer")
    core.projects.add_member("beta", "telegram", "2", "manager")
    core.projects.bind("beta", "telegram", "-100")
    assert core.projects.resolve("telegram", "-100", "2")[:2] == ("beta", Role.MANAGER)
    assert core.projects.resolve("telegram", "555", "1")[:2] == ("alpha", Role.ADMIN)      # DM, single project
    assert core.projects.resolve("telegram", "555", "2")[0] is None                          # DM, ambiguous
    assert core.projects.resolve("telegram", "-100", "99")[1] is None                        # stranger in a bound chat


def test_hermes_client_against_fake_server(core):
    import threading, socket, uvicorn
    from fastapi import FastAPI, Request
    srv = FastAPI(); seen = {}

    @srv.get("/health")
    def h(): return {"status": "ok"}

    @srv.post("/v1/chat/completions")
    async def c(req: Request):
        seen["h"], seen["b"] = dict(req.headers), await req.json()
        return {"choices": [{"message": {"content": "pong"}}]}
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    server = uvicorn.Server(uvicorn.Config(srv, port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    cl = hermes.Client({"id": "alpha", "api_port": port, "api_key": "k"})
    assert cl.wait_ready(10)
    assert cl.chat("ping", session="s1") == "pong"
    assert seen["h"]["authorization"] == "Bearer k" and seen["h"]["x-hermes-session-id"] == "s1"
    assert seen["h"]["x-hermes-session-key"].startswith("olian:alpha:")
    server.should_exit = True
    with pytest.raises(hermes.HermesError):
        hermes.Client({"id": "x", "api_port": 1, "api_key": "k"}, timeout=2).chat("hi")


def test_gateway_lifecycle_survives_a_new_orchestrator_process(core, tmp_path):
    """start in one process object, stop from a fresh Runtime (== a later `olian project stop`): no orphans."""
    import os, stat
    fake = tmp_path / "hermes"
    fake.write_text("#!/bin/sh\nexec sleep 300 'gateway' \"$@\"\n")  # cmdline contains 'gateway'
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    core.s.hermes_mode, core.s.hermes_bin = "process", str(fake)
    core.projects.create("alpha", "A")
    n = core.projects.start("alpha")
    assert core.runtime.alive("alpha") and core.projects.start("alpha") == n           # idempotent
    fresh = hermes.Runtime(core.s)                                                      # a different "process"
    assert fresh.alive("alpha")
    fresh.stop("alpha")
    assert not fresh.alive("alpha") and not (core.s.hermes_root / "alpha" / "olian-gateway.pid").exists()
    core.runtime.procs["alpha"].poll()                                                  # (init would reap it for us)
    assert not __import__("pathlib").Path(f"/proc/{n}").exists()                       # really gone


def test_missing_hermes_binary_is_a_clear_error(core):
    core.s.hermes_mode, core.s.hermes_bin = "process", "/nonexistent/hermes"
    core.projects.create("alpha", "A")
    with pytest.raises(hermes.HermesError, match="not found"):
        core.projects.start("alpha")
