"""Project registry: create/provision projects, members, chat bindings, scoped tokens."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time

from . import hermes
from .config import Settings
from .db import DB
from .rbac import Role, parse_role

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}$")


class NotFound(Exception):
    pass


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Projects:
    def __init__(self, db: DB, s: Settings, runtime: hermes.Runtime):
        self.db, self.s, self.rt = db, s, runtime

    # ----- projects
    def get(self, pid: str) -> dict:
        row = self.db.one("SELECT * FROM projects WHERE id=?", (pid,))
        if not row:
            raise NotFound(f"project {pid!r} not found")
        row["cfg"] = json.loads(row["config"] or "{}")
        return row

    def list(self) -> list[dict]:
        rows = self.db.q("SELECT * FROM projects ORDER BY id")
        for r in rows:
            r["cfg"] = json.loads(r["config"] or "{}")
            r["running"] = self.rt.alive(r["id"])
        return rows

    def _next_port(self) -> int:
        used = {r["api_port"] for r in self.db.q("SELECT api_port FROM projects")}
        port = self.s.port_base
        while port in used:
            port += 1
        return port

    def create(self, pid: str, name: str, goal: str = "", cfg: dict | None = None) -> dict:
        if not SLUG.match(pid):
            raise ValueError("project id: 2-39 chars, lowercase letters, digits, dashes")
        if self.db.one("SELECT 1 FROM projects WHERE id=?", (pid,)):
            raise ValueError(f"project {pid!r} already exists")
        self.validate_cfg(cfg or {})
        self.db.x("INSERT INTO projects(id,name,goal,api_port,api_key,config,created_at) VALUES(?,?,?,?,?,?,?)",
                  (pid, name, goal, self._next_port(), secrets.token_urlsafe(24),
                   json.dumps(cfg or {}, ensure_ascii=False), time.time()))
        (self.s.projects_dir / pid).mkdir(parents=True, exist_ok=True)
        return self.provision(pid)

    def provision(self, pid: str) -> dict:
        """(Re)write the project's Hermes profile from its config. The agent token is kept stable across
        re-provisioning (a running gateway holds it); it is rotated only if missing/changed."""
        project = self.get(pid)
        scopes = project["cfg"].get("agent", {}).get("scopes", "")
        token = self._current_agent_token(pid)
        info = self.token_info(token) if token else None
        if not info or info["scopes"] != scopes or info["project_id"] != pid:
            token = self.issue_token(pid, "agent", Role.MANAGER, scopes=scopes, replace=True)
        hermes.provision(project, project["cfg"], self.s, agent_token=token)
        return project

    def _current_agent_token(self, pid: str) -> str:
        env = self.s.hermes_root / pid / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("OLIAN_AGENT_TOKEN="):
                    return line.split("=", 1)[1].strip()
        return ""

    @staticmethod
    def validate_cfg(cfg: dict) -> None:
        mon = cfg.get("monitor", {})
        chat_tok = cfg.get("channels", {}).get("telegram", {}).get("bot_token")
        if mon.get("bot_token") and mon.get("bot_token") == chat_tok:
            raise ValueError("monitor.bot_token must differ from the chat bot token (two consumers of one getUpdates conflict)")
        if mon.get("author", "hash") not in ("hash", "raw", "none"):
            raise ValueError("monitor.author must be hash|raw|none")
        if mon.get("mode", "rules") not in ("rules", "llm", "hybrid"):
            raise ValueError("monitor.mode must be rules|llm|hybrid")

    def update_config(self, pid: str, patch: dict) -> dict:
        project = self.get(pid)
        cfg = _merge(project["cfg"], patch)
        self.validate_cfg(cfg)
        self.db.x("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), pid))
        return self.provision(pid)

    def delete(self, pid: str) -> None:
        self.rt.stop(pid)
        self.db.x("DELETE FROM projects WHERE id=?", (pid,))

    def start(self, pid: str) -> int:
        project = self.get(pid)
        pidnum = self.rt.start(project)
        self.db.x("UPDATE projects SET status='running' WHERE id=?", (pid,))
        return pidnum

    def stop(self, pid: str) -> None:
        self.rt.stop(pid)
        self.db.x("UPDATE projects SET status='stopped' WHERE id=?", (pid,))

    def client(self, pid: str) -> hermes.Client:
        return hermes.Client(self.get(pid))

    # ----- people
    def add_member(self, pid: str, platform: str, user_id: str, role: str, name: str = "", scopes: str = "") -> None:
        self.get(pid)
        parse_role(role)
        self.db.x("INSERT OR REPLACE INTO members(project_id,platform,user_id,name,role,scopes) VALUES(?,?,?,?,?,?)",
                  (pid, platform, str(user_id), name, role.lower(), scopes))
        self._sync_allowlists(pid)

    def remove_member(self, pid: str, platform: str, user_id: str) -> None:
        self.db.x("DELETE FROM members WHERE project_id=? AND platform=? AND user_id=?", (pid, platform, str(user_id)))
        self._sync_allowlists(pid)

    def members(self, pid: str) -> list[dict]:
        return self.db.q("SELECT * FROM members WHERE project_id=? ORDER BY platform,user_id", (pid,))

    def _sync_allowlists(self, pid: str) -> None:
        """Hermes' own gateway must admit exactly this project's members, nobody else."""
        project = self.get(pid)
        cfg = project["cfg"]
        ch = cfg.setdefault("channels", {})
        for plat in ("telegram", "whatsapp"):
            users = [m["user_id"] for m in self.members(pid) if m["platform"] == plat]
            if users:
                ch.setdefault(plat, {})["allowed_users"] = users
        self.db.x("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), pid))
        self.provision(pid)

    def bind(self, pid: str, platform: str, chat_id: str) -> None:
        self.get(pid)
        self.db.x("INSERT OR REPLACE INTO bindings(platform,chat_id,project_id) VALUES(?,?,?)",
                  (platform, str(chat_id), pid))

    def role_of(self, pid: str, platform: str, user_id: str) -> tuple[Role | None, str]:
        m = self.db.one("SELECT role,scopes FROM members WHERE project_id=? AND platform=? AND user_id=?",
                        (pid, platform, str(user_id)))
        return (Role(m["role"]), m["scopes"]) if m else (None, "")

    def resolve(self, platform: str, chat_id: str, user_id: str) -> tuple[str | None, Role | None, str]:
        """chat → project (binding), else a user who belongs to exactly one project (DM). Returns (pid, role, scopes)."""
        b = self.db.one("SELECT project_id FROM bindings WHERE platform=? AND chat_id=?", (platform, str(chat_id)))
        if b:
            role, scopes = self.role_of(b["project_id"], platform, user_id)
            return b["project_id"], role, scopes
        mine = self.db.q("SELECT project_id,role,scopes FROM members WHERE platform=? AND user_id=?",
                         (platform, str(user_id)))
        if len(mine) == 1:
            return mine[0]["project_id"], Role(mine[0]["role"]), mine[0]["scopes"]
        return None, None, ""

    # ----- tokens (API clients: the agent, scrapers, MCP, scripts)
    def issue_token(self, pid: str, label: str, role: Role | str, scopes: str = "", replace: bool = False) -> str:
        if replace:
            self.db.x("DELETE FROM tokens WHERE project_id=? AND label=?", (pid, label))
        token = "olp_" + secrets.token_urlsafe(32)
        self.db.x("INSERT INTO tokens(hash,project_id,label,role,scopes,created_at) VALUES(?,?,?,?,?,?)",
                  (hash_token(token), pid, label, Role(role).value, scopes, time.time()))
        return token

    def revoke_token(self, pid: str, label: str) -> int:
        """Immediately invalidate every token with this label. The agent's own token is re-issued by provision()."""
        n = self.db.one("SELECT COUNT(*) n FROM tokens WHERE project_id=? AND label=?", (pid, label))["n"]
        self.db.x("DELETE FROM tokens WHERE project_id=? AND label=?", (pid, label))
        return n

    def list_tokens(self, pid: str) -> list[dict]:
        """Labels, roles, scopes, age. Never the token itself (only a hash is stored)."""
        return self.db.q("SELECT label, role, scopes, created_at FROM tokens WHERE project_id=? ORDER BY created_at", (pid,))

    def token_info(self, token: str) -> dict | None:
        return self.db.one("SELECT * FROM tokens WHERE hash=?", (hash_token(token),))


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out
