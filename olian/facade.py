"""The project facade: the ONE way anything (agent, human, scraper, MCP client) changes project state.

Pattern from the field: the agent is just another client of an operational API. It never touches the DB;
permissions are enforced here, per actor; every call is written to the audit log.
"""
from __future__ import annotations

import fnmatch
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .rbac import Forbidden, Role, allowed

_ORDER = {Role.VIEWER: 1, Role.MANAGER: 2, Role.ADMIN: 3}


class ActionError(Exception):
    pass


@dataclass
class Actor:
    label: str
    role: Role
    scopes: str = ""  # comma-separated fnmatch patterns over action names; empty = all the role allows

    def may(self, action: str, min_role: Role) -> bool:
        if _ORDER[self.role] < _ORDER[min_role]:
            return False
        pats = [p.strip() for p in self.scopes.split(",") if p.strip()]
        return not pats or any(fnmatch.fnmatch(action, p) for p in pats)


def narrow(a: Actor, b: Actor) -> Actor:
    """Intersection of two actors: lower role, scopes of whichever restricts (both -> must satisfy both)."""
    role = a.role if _ORDER[a.role] <= _ORDER[b.role] else b.role
    pa = [p.strip() for p in a.scopes.split(",") if p.strip()]
    pb = [p.strip() for p in b.scopes.split(",") if p.strip()]
    if pa and pb:  # keep patterns of b that a also admits, else a's patterns that b admits
        scopes = [p for p in pb if any(fnmatch.fnmatch(p, q) or fnmatch.fnmatch(q, p) for q in pa)] or ["-none-"]
    else:
        scopes = pa or pb
    return Actor(f"{a.label}|{b.label}", role, ",".join(scopes))


@dataclass
class Action:
    name: str
    summary: str
    min_role: Role
    params: dict  # JSON-schema "properties"
    required: list[str]
    fn: Callable[..., Any]
    agent_ok: bool = True  # False: the action itself drives the agent, so the agent may not call it (no recursion)


class Facade:
    def __init__(self, app):
        self.app = app
        self.actions: dict[str, Action] = {}
        self._acting: dict[str, Actor] = {}
        self._lock = threading.Lock()
        self.project_locks: dict[str, threading.RLock] = {}

    # --- registration
    def register(self, name: str, summary: str, min_role: Role, params: dict | None = None,
                 required: list[str] | None = None, agent_ok: bool = True):
        def deco(fn):
            self.actions[name] = Action(name, summary, min_role, params or {}, required or [], fn, agent_ok)
            return fn
        return deco

    # --- acting-as: while a human's message is being processed by the agent, the agent's own tool calls are
    # limited to what that human may do (intersection with the agent token). Autonomous work runs as system.
    def project_lock(self, pid: str) -> threading.RLock:
        with self._lock:
            return self.project_locks.setdefault(pid, threading.RLock())

    class _Acting:
        def __init__(self, f: "Facade", pid: str, actor: Actor):
            self.f, self.pid, self.actor = f, pid, actor

        def __enter__(self):
            self.f.project_lock(self.pid).acquire()
            self.f._acting[self.pid] = self.actor

        def __exit__(self, *exc):
            self.f._acting.pop(self.pid, None)
            self.f.project_lock(self.pid).release()

    def acting(self, pid: str, actor: Actor) -> "_Acting":
        return Facade._Acting(self, pid, actor)

    # --- invocation
    def call(self, pid: str, actor: Actor, name: str, args: dict | None = None, via_agent: bool = False) -> Any:
        args = args or {}
        eff = actor
        if via_agent and pid in self._acting:
            eff = narrow(actor, self._acting[pid])
        act = self.actions.get(name)
        if act is None:
            raise ActionError(f"unknown action {name!r}")
        ok, result = False, ""
        try:
            if via_agent and not act.agent_ok:
                raise Forbidden(f"{name} is not available to the agent")
            if not eff.may(name, act.min_role):
                raise Forbidden(f"{eff.label} ({eff.role.value}) may not {name}")
            missing = [r for r in act.required if r not in args]
            if missing:
                raise ActionError(f"missing arguments: {', '.join(missing)}")
            unknown = [k for k in args if k not in act.params]
            if unknown:
                raise ActionError(f"unknown arguments: {', '.join(unknown)}")
            out = act.fn(self.app, pid, eff, **args)
            ok, result = True, json.dumps(out, ensure_ascii=False, default=str)[:500]
            return out
        except Exception as e:
            result = f"{type(e).__name__}: {e}"[:500]
            raise
        finally:  # every call leaves a trace, success or not
            self.app.db.x("INSERT INTO audit(project_id,actor,action,args,ok,result,created_at) VALUES(?,?,?,?,?,?,?)",
                          (pid, eff.label, name, json.dumps(_redact(args), ensure_ascii=False, default=str)[:2000],
                           int(ok), result, time.time()))

    # --- discovery
    def visible(self, actor: Actor, pid: str, via_agent: bool = False) -> list[Action]:
        eff = narrow(actor, self._acting[pid]) if via_agent and pid in self._acting else actor
        return [a for a in self.actions.values()
                if eff.may(a.name, a.min_role) and (a.agent_ok or not via_agent)]

    def mcp_tools(self, actor: Actor, pid: str, via_agent: bool = True) -> list[dict]:
        return [{"name": a.name.replace(".", "_"), "description": a.summary,
                 "inputSchema": {"type": "object", "properties": a.params, "required": a.required}}
                for a in self.visible(actor, pid, via_agent)]

    def mcp_name_to_action(self, tool: str) -> str | None:
        for n in self.actions:
            if n.replace(".", "_") == tool:
                return n
        return None

    def openapi(self, actor: Actor, pid: str, base: str = "") -> dict:
        paths = {}
        for a in self.visible(actor, pid):
            paths[f"/p/actions/{a.name}"] = {"post": {
                "summary": a.summary, "operationId": a.name.replace(".", "_"),
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": a.params, "required": a.required}}}},
                "responses": {"200": {"description": "result"}}}}
        return {"openapi": "3.0.3", "info": {"title": f"Olian facade: {pid}", "version": "1"},
                "servers": [{"url": base}] if base else [], "paths": paths,
                "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
                "security": [{"bearer": []}]}


def _redact(obj):
    """Secrets never reach the audit log, at any nesting depth."""
    if isinstance(obj, dict):
        return {k: ("***" if any(s in str(k).lower() for s in ("token", "secret", "password", "key", "authorization"))
                    else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj
