import json
import os
import sqlite3

import pytest

from olian import cli


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OLIAN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OLIAN_HERMES_MODE", "external")
    monkeypatch.delenv("OLIAN_BACKUP_KEY", raising=False)
    monkeypatch.delenv("OLIAN_PII_SALT", raising=False)
    return tmp_path


def test_setup_writes_private_env_once(env, capsys):
    assert cli.main(["setup"]) == 0
    p = env / ".env"
    vals = dict(l.split("=", 1) for l in p.read_text().splitlines())
    assert p.stat().st_mode & 0o077 == 0
    assert len(vals["OLIAN_API_KEY"]) >= 32 and len(vals["OLIAN_PII_SALT"]) == 64 and vals["OLIAN_BACKUP_KEY"].endswith("=")
    assert cli.main(["setup"]) == 1                                         # never overwrites existing secrets
    assert vals["OLIAN_API_KEY"] in p.read_text()


def test_monitor_init_creates_a_working_starter_setup(env, capsys):
    cli.main(["project", "create", "bali", "--name", "Bali", "--sandbox", "local"])
    assert cli.main(["monitor", "init", "bali", "--bot-token", "999:MON_TOKEN_abcdefghijklmnopqrstuvwxyz", "--topics",
                     "вілл*,глемпінг*", "--category", "stay", "--profile", "туристи", "--langs", "uk,ru"]) == 0
    from olian.app import App
    core = App()
    cfg = core.projects.get("bali")["cfg"]["monitor"]
    assert cfg["enabled"] and cfg["author"] == "hash" and cfg["profile"] == "туристи"
    rule = core.monitor.rules("bali")[0]
    assert rule["category"] == "stay" and "шукаю" in rule["require_any"] and "ищу" in rule["require_any"] and "looking for" not in rule["require_any"]
    core.monitor.add_source("bali", "-100111")
    assert core.monitor.ingest_message("bali", "-100111", 1, "Шукаю віллу на Балі", author_id=1)["result"] == "queued"
    assert core.monitor.ingest_message("bali", "-100111", 2, "Вілла на Балі була чудова", author_id=1)["result"] == "no_match"   # topic w/o intent
    assert core.monitor.ingest_message("bali", "-100111", 3, "Продам віллу, шукаю покупця", author_id=1)["result"] == "no_match"  # excluded
    assert cli.main(["monitor", "init", "bali", "--bot-token", "x", "--topics", "a", "--mode", "llm"]) == 0


def test_backup_is_encrypted_and_restores_round_trip(env, monkeypatch, capsys):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("OLIAN_BACKUP_KEY", Fernet.generate_key().decode())
    cli.main(["project", "create", "bali", "--name", "Bali Secret Project", "--sandbox", "local"])
    from olian.app import App
    App().s.pii_salt()
    assert cli.main(["backup", "--out", "b.tar.gz.enc"]) == 0
    raw = (env / "b.tar.gz.enc").read_bytes()
    assert b"Bali Secret Project" not in raw and b"SQLite" not in raw       # ciphertext, nothing readable
    assert (env / "b.tar.gz.enc").stat().st_mode & 0o077 == 0
    assert cli.main(["restore", "b.tar.gz.enc", "--to", str(env / "restored")]) == 0
    row = sqlite3.connect(env / "restored" / "olian.db").execute("select name from projects").fetchone()
    assert row[0] == "Bali Secret Project" and (env / "restored" / ".pii_salt").exists()
    monkeypatch.setenv("OLIAN_BACKUP_KEY", Fernet.generate_key().decode())
    assert cli.main(["restore", "b.tar.gz.enc", "--to", str(env / "r2")]) == 1  # wrong key is refused


def test_plain_backup_needs_explicit_consent(env):
    cli.main(["project", "create", "bali", "--name", "B", "--sandbox", "local"])
    assert cli.main(["backup"]) == 1
    assert cli.main(["backup", "--plain", "--out", "p.tar.gz"]) == 0 and (env / "p.tar.gz").exists()


def test_evaluate_command(env, capsys):
    cli.main(["project", "create", "bali", "--name", "B", "--sandbox", "local"])
    cli.main(["monitor", "init", "bali", "--bot-token", "1:x_abcdefghijklmnopqrstuvwxyz", "--topics", "вілл*"])
    (env / "s.json").write_text(json.dumps([{"text": "Шукаю віллу на Балі, підкажіть?", "expected": "relevant"},
                                            {"text": "Погода гарна", "expected": "irrelevant"}]))
    capsys.readouterr()
    assert cli.main(["monitor", "evaluate", "bali", "s.json"]) == 0
    assert '"precision": 1.0' in capsys.readouterr().out


def test_tokens_can_be_listed_rotated_and_revoked(env, capsys):
    from olian.app import App
    cli.main(["project", "create", "bali", "--name", "B", "--sandbox", "local"])
    capsys.readouterr()
    cli.main(["token", "bali", "--label", "operator", "--role", "manager"])
    old = capsys.readouterr().out.strip()
    cli.main(["token", "bali", "--label", "operator", "--role", "manager", "--replace"])
    new = capsys.readouterr().out.strip()
    core = App()
    assert core.projects.token_info(old) is None and core.projects.token_info(new)["label"] == "operator"   # rotation kills the old one
    cli.main(["tokens", "bali"]); listing = capsys.readouterr().out
    assert "operator" in listing and "agent" in listing and new not in listing                             # never prints secrets
    cli.main(["token-revoke", "bali", "operator"])
    assert App().projects.token_info(new) is None


def test_token_revocation_over_the_admin_api(core, proj):
    from fastapi.testclient import TestClient
    from olian.api import create_app
    t = core.projects.issue_token(proj, "tmp", "manager")
    with TestClient(create_app(core, workers=False)) as c:
        H = {"Authorization": "Bearer admin-key"}
        assert c.get("/p/whoami", headers={"Authorization": f"Bearer {t}"}).status_code == 200
        assert any(x["label"] == "tmp" for x in c.get(f"/projects/{proj}/tokens", headers=H).json())
        assert c.delete(f"/projects/{proj}/tokens/tmp", headers=H).json() == {"revoked": 1}
        assert c.get("/p/whoami", headers={"Authorization": f"Bearer {t}"}).status_code == 401
        assert c.delete(f"/projects/{proj}/tokens/tmp", headers={"Authorization": "Bearer nope"}).status_code == 401
