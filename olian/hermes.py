"""Everything that touches Hermes Agent.

One project == one isolated Hermes profile (its own HERMES_HOME: config, .env, SOUL.md, memory, skills,
sessions, cron) served by its own gateway process on its own API-server port. Hermes is used unmodified,
so upgrading it never conflicts with this code.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from .config import Settings


class HermesError(RuntimeError):
    pass


# --------------------------------------------------------------------------- provisioning
def render_soul(project: dict, cfg: dict) -> str:
    agent = cfg.get("agent", {})
    lines = [
        f"# {agent.get('role', 'Project agent')} — {project['name']}",
        "",
        f"You work exclusively on the project «{project['name']}» (id: {project['id']}).",
        f"Project goal: {project.get('goal') or 'not set yet'}",
    ]
    if agent.get("tone"):
        lines += ["", f"Tone of voice: {agent['tone']}"]
    if agent.get("language"):
        lines += [f"Default language: {agent['language']}"]
    if agent.get("rules"):
        lines += ["", "Hard rules:"] + [f"- {r}" for r in agent["rules"]]
    lines += [
        "",
        "How you act: all changes to the project go through your `olian` tools (tasks, goals, leads, outreach, scrapers,",
        "events, notify). They enforce permissions for the person you are serving and are audited; never try to",
        "work around them. When you cannot do something yourself (no access, payments, calls, accounts), do not just",
        "say so: create a human task with tasks_add (executor=human) and concrete `steps` [{text, link, copy}] so the",
        "person only clicks and pastes. For outreach use outreach_compose: a human approves every message before",
        "it is sent. Never contact anyone who said stop.",
        "",
        "Boundaries:",
        "- You know nothing about other projects and must never ask for or guess their data.",
        "- Outreach messages are only drafted by you; sending goes through the project's outreach queue.",
        "- When a job needs a human, say exactly what the person must do and why.",
    ]
    return "\n".join(lines) + "\n"


def render_config(project: dict, cfg: dict, s: Settings) -> dict:
    out: dict[str, Any] = {}
    model = {**{k: v for k, v in (("default", s.default_model), ("provider", s.default_provider)) if v},
             **cfg.get("model", {})}
    if model:
        out["model"] = model
    sandbox = cfg.get("sandbox", {})
    backend = sandbox.get("backend", "docker")
    term: dict[str, Any] = {"backend": backend}
    if backend == "docker":
        term.update(cwd="/workspace", docker_image=sandbox.get("image", s.sandbox_image))
    out["terminal"] = term
    if cfg.get("toolsets"):
        out["toolsets"] = cfg["toolsets"]
    # The project's operational facade, exposed to the agent as native MCP tools (token comes from .env).
    out["mcp_servers"] = {"olian": {"url": f"{s.agent_url}/mcp",
                                    "headers": {"Authorization": "Bearer ${OLIAN_AGENT_TOKEN}"}}}
    return out


def render_env(project: dict, cfg: dict, s: Settings, agent_token: str = "") -> str:
    ch = cfg.get("channels", {})
    env = {
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": str(project["api_port"]),
        "API_SERVER_KEY": project["api_key"],
    }
    if agent_token:
        env["OLIAN_AGENT_TOKEN"] = agent_token
    tg = ch.get("telegram", {})
    # By default Olian owns the Telegram bot (so per-person RBAC applies); "via: hermes" hands it to Hermes' gateway.
    if tg.get("bot_token") and tg.get("via", "olian") == "hermes":
        env["TELEGRAM_BOT_TOKEN"] = tg["bot_token"]
        if tg.get("allowed_users"):
            env["TELEGRAM_ALLOWED_USERS"] = ",".join(str(u) for u in tg["allowed_users"])
        if tg.get("allowed_chats"):
            env["TELEGRAM_ALLOWED_CHATS"] = ",".join(str(u) for u in tg["allowed_chats"])
    wa = ch.get("whatsapp", {})  # team-facing WhatsApp via Hermes' Baileys bridge (NOT for cold outreach)
    if wa.get("enabled"):
        env["WHATSAPP_ENABLED"] = "true"
        env["WHATSAPP_MODE"] = wa.get("mode", "bot")
        env["WHATSAPP_ALLOWED_USERS"] = ",".join(str(u) for u in wa.get("allowed_users", [])) or "none"
    for k, v in cfg.get("env", {}).items():  # provider keys etc.
        env[str(k)] = str(v)
    return "".join(f"{k}={v}\n" for k, v in env.items())


def provision(project: dict, cfg: dict, s: Settings, agent_token: str = "") -> Path:
    """Create/refresh the project's Hermes home. Idempotent; never touches memory/sessions."""
    home = s.hermes_root / project["id"]
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(render_config(project, cfg, s), sort_keys=False, allow_unicode=True))
    env_path = home / ".env"
    env_path.write_text(render_env(project, cfg, s, agent_token))
    env_path.chmod(0o600)
    (home / "SOUL.md").write_text(render_soul(project, cfg))
    return home


# --------------------------------------------------------------------------- process control
def _proc_state(n: int) -> str:
    """Linux process state letter from /proc/<pid>/stat ('' where /proc is unavailable)."""
    try:
        return Path(f"/proc/{n}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return ""


def _proc_start(n: int) -> str:
    """Process start time in clock ticks since boot (field 22 of /proc/<pid>/stat); '' where /proc is unavailable."""
    try:
        return Path(f"/proc/{n}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


class Runtime:
    """Starts/stops one `hermes gateway run --force` per project, each with its own HERMES_HOME.

    Hermes folds profiles under ~/.hermes/profiles into ONE multiplexing host gateway by default. Our projects live
    outside that tree on purpose (separate processes, ports, secrets, optionally separate users), which Hermes
    documents as the `--force` case. State is a pidfile in the project's home, so any later `olian` invocation (or a
    restarted orchestrator) can still find and stop the gateway: no orphans.
    """

    def __init__(self, s: Settings):
        self.s = s
        self.procs: dict[str, subprocess.Popen] = {}

    def home(self, pid: str) -> Path:
        return self.s.hermes_root / pid

    def _pidfile(self, pid: str) -> Path:
        return self.home(pid) / "olian-gateway.pid"

    def _read_pid(self, pid: str) -> int | None:
        """The pidfile holds `pid:starttime`. A pid is ours only if it exists AND its kernel start time still matches, which
        defeats pid reuse without parsing command lines (those are unreliable while a process is mid-exec)."""
        try:
            raw = self._pidfile(pid).read_text().strip()
            n, _, started = raw.partition(":")
            n = int(n)
        except (OSError, ValueError):
            return None
        try:
            os.kill(n, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return n
        if _proc_state(n) in ("Z", "X"):  # exited, waiting to be reaped == dead
            return None
        if started and _proc_start(n) not in ("", started):
            return None
        return n

    def alive(self, pid: str) -> bool:
        p = self.procs.get(pid)
        if p is not None and p.poll() is not None:
            self.procs.pop(pid, None)
        return self._read_pid(pid) is not None

    def start(self, project: dict) -> int:
        if self.s.hermes_mode == "external":
            return 0
        pid = project["id"]
        if (n := self._read_pid(pid)) is not None:
            return n
        if not shutil.which(self.s.hermes_bin) and not Path(self.s.hermes_bin).exists():
            raise HermesError(
                f"hermes binary {self.s.hermes_bin!r} not found. Install Hermes (see README) or set OLIAN_HERMES_BIN.")
        home = self.home(pid)
        log = open(home / "gateway.log", "ab")
        env = {**os.environ, "HERMES_HOME": str(home)}
        p = subprocess.Popen([self.s.hermes_bin, "gateway", "run", "--force"], env=env, cwd=home,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.procs[pid] = p
        self._pidfile(pid).write_text(f"{p.pid}:{_proc_start(p.pid)}")
        return p.pid

    def stop(self, pid: str) -> None:
        n = self._read_pid(pid)
        if n is not None:
            try:
                os.killpg(n, signal.SIGTERM)  # own session => its whole process group (bridge, MCP children)
            except (ProcessLookupError, PermissionError):
                pass
            end = time.time() + 15
            while time.time() < end and self._read_pid(pid) is not None:
                p = self.procs.get(pid)
                if p is not None:
                    p.poll()  # reap if it is our child
                time.sleep(0.2)
            if self._read_pid(pid) is not None:
                try:
                    os.killpg(n, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.procs.pop(pid, None)
        self._pidfile(pid).unlink(missing_ok=True)

    def stop_all(self) -> None:
        for d in (self.s.hermes_root.iterdir() if self.s.hermes_root.exists() else []):
            if d.is_dir():
                self.stop(d.name)


# --------------------------------------------------------------------------- client
class Client:
    """Talks to a project's Hermes through its OpenAI-compatible API server."""

    def __init__(self, project: dict, base_url: str | None = None, timeout: float = 600):
        self.base = (base_url or f"http://127.0.0.1:{project['api_port']}").rstrip("/")
        self.project = project
        self.timeout = timeout

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.project['api_key']}"}

    def healthy(self) -> bool:
        try:
            return httpx.get(f"{self.base}/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def wait_ready(self, seconds: float = 60) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if self.healthy():
                return True
            time.sleep(1)
        return False

    def chat(self, message: str, session: str | None = None, system: str | None = None) -> str:
        """One turn. `session` keeps conversation continuity per chat; the project id scopes long-term memory."""
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": message}]
        headers = {**self._headers,
                   "X-Hermes-Session-Id": session or f"olian-{uuid.uuid4().hex[:12]}",
                   "X-Hermes-Session-Key": f"olian:{self.project['id']}:{session or 'default'}"[:256]}
        try:
            r = httpx.post(f"{self.base}/v1/chat/completions", headers=headers, timeout=self.timeout,
                           json={"model": "hermes-agent", "messages": msgs})
        except httpx.HTTPError as e:
            raise HermesError(f"project {self.project['id']}: agent unreachable ({e})") from e
        if r.status_code != 200:
            raise HermesError(f"project {self.project['id']}: agent error {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"] or ""
