"""HTTP surface.

  admin (OLIAN_API_KEY)      /projects ...                       create projects, members, bindings, tokens
  project token (olp_...)    /p/actions/{name}  /p/openapi.json  the facade over REST
                             /mcp                                the same facade as MCP tools (Hermes, Claude, ...)
                             /ingest                             scrapers/monitors push events
  signed/verified inbound    /webhooks/{project}/{source}        HMAC-signed events from any service
                             /wa/webhook                         Meta WhatsApp Cloud API (replies + team chat)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

from .app import App
from .channels import ChannelError, WhatsAppCloud
from .facade import ActionError, Actor
from .hermes import HermesError
from .projects import NotFound
from .rbac import Forbidden, Role

log = logging.getLogger("olian.api")


def create_app(core: App | None = None, workers: bool | None = None) -> FastAPI:
    core = core or App()
    if workers is None:
        workers = os.environ.get("OLIAN_WORKER", "1") != "0"

    @asynccontextmanager
    async def lifespan(_):
        if workers:
            core.start_workers()
        yield
        core.shutdown()

    api = FastAPI(title="Olian", version="0.1.0", lifespan=lifespan)
    api.state.core = core

    # ------------------------------------------------------------------ auth
    def bearer(authorization: str | None, x_key: str | None) -> str:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return (x_key or "").strip()

    def admin(authorization: str | None = Header(None), x_olian_key: str | None = Header(None)) -> None:
        key = core.s.api_key
        if not key:
            raise HTTPException(503, "OLIAN_API_KEY is not set; admin API disabled")
        if not hmac.compare_digest(bearer(authorization, x_olian_key), key):
            raise HTTPException(401, "bad admin key")

    def principal(authorization: str | None = Header(None), x_olian_key: str | None = Header(None),
                  x_olian_project: str | None = Header(None)):
        tok = bearer(authorization, x_olian_key)
        if tok and core.s.api_key and x_olian_project and hmac.compare_digest(tok, core.s.api_key):
            core.projects.get(x_olian_project)  # the admin key may act on a project it names, as admin
            return x_olian_project, Actor("admin-ui", Role.ADMIN), False
        info = core.projects.token_info(tok) if tok else None
        if not info:
            raise HTTPException(401, "bad token")
        return info["project_id"], Actor(info["label"], Role(info["role"]), info["scopes"]), info["label"] == "agent"

    @api.exception_handler(Forbidden)
    async def _forbidden(_, e):
        return JSONResponse({"error": str(e)}, 403)

    @api.exception_handler(NotFound)
    async def _nf(_, e):
        return JSONResponse({"error": str(e)}, 404)

    @api.exception_handler(ActionError)
    @api.exception_handler(ValueError)
    async def _bad(_, e):
        return JSONResponse({"error": str(e)}, 400)

    @api.exception_handler(HermesError)
    async def _agent(_, e):
        return JSONResponse({"error": str(e)}, 502)

    # ------------------------------------------------------------------ admin panel (static, strict CSP)
    static = Path(__file__).parent / "static"
    PANEL_HEADERS = {
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                                   "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
        "X-Frame-Options": "DENY"}

    def asset(name: str, media: str):
        return Response((static / name).read_bytes(), media_type=media, headers=PANEL_HEADERS)

    @api.get("/admin", include_in_schema=False)
    def panel():
        return asset("admin.html", "text/html; charset=utf-8")

    @api.get("/admin/app.js", include_in_schema=False)
    def panel_js():
        return asset("admin.js", "text/javascript; charset=utf-8")

    @api.get("/admin/app.css", include_in_schema=False)
    def panel_css():
        return asset("admin.css", "text/css; charset=utf-8")

    @api.get("/health")
    def health():
        return {"status": "ok", "projects": len(core.projects.list())}

    # ------------------------------------------------------------------ admin
    @api.post("/projects", dependencies=[Depends(admin)])
    def create_project(body: dict):
        p = core.projects.create(body["id"], body.get("name", body["id"]), body.get("goal", ""), body.get("config"))
        return _public(p)

    @api.get("/projects", dependencies=[Depends(admin)])
    def list_projects():
        return [_public(p) for p in core.projects.list()]

    @api.get("/projects/{pid}", dependencies=[Depends(admin)])
    def get_project(pid: str):
        p = core.projects.get(pid)
        return {**_public(p), "members": core.projects.members(pid),
                "healthy": core.client_factory(p).healthy()}

    @api.put("/projects/{pid}/config", dependencies=[Depends(admin)])
    def put_config(pid: str, body: dict):
        return _public(core.projects.update_config(pid, body))

    @api.post("/projects/{pid}/start", dependencies=[Depends(admin)])
    def start(pid: str):
        return {"pid": core.projects.start(pid)}

    @api.post("/projects/{pid}/stop", dependencies=[Depends(admin)])
    def stop(pid: str):
        core.projects.stop(pid)
        return {"ok": True}

    @api.delete("/projects/{pid}", dependencies=[Depends(admin)])
    def delete(pid: str):
        core.projects.delete(pid)
        return {"ok": True}

    @api.post("/projects/{pid}/members", dependencies=[Depends(admin)])
    def add_member(pid: str, body: dict):
        core.projects.add_member(pid, body["platform"], body["user_id"], body["role"], body.get("name", ""), body.get("scopes", ""))
        return {"ok": True}

    @api.post("/projects/{pid}/bindings", dependencies=[Depends(admin)])
    def bind(pid: str, body: dict):
        core.projects.bind(pid, body["platform"], body["chat_id"])
        return {"ok": True}

    @api.post("/projects/{pid}/tokens", dependencies=[Depends(admin)])
    def issue(pid: str, body: dict):
        core.projects.get(pid)
        return {"token": core.projects.issue_token(pid, body["label"], body.get("role", "manager"), body.get("scopes", ""))}

    @api.get("/projects/{pid}/tokens", dependencies=[Depends(admin)])
    def list_tokens(pid: str):
        core.projects.get(pid)
        return core.projects.list_tokens(pid)

    @api.delete("/projects/{pid}/tokens/{label}", dependencies=[Depends(admin)])
    def revoke_token(pid: str, label: str):
        core.projects.get(pid)
        return {"revoked": core.projects.revoke_token(pid, label)}

    @api.post("/chat", dependencies=[Depends(admin)])
    def chat(body: dict):
        return {"reply": core.chat.handle(body.get("platform", "api"), str(body.get("chat_id", "")), str(body["user_id"]),
                                          body["text"], body.get("name", ""))}

    # ------------------------------------------------------------------ facade over REST
    @api.get("/p/actions")
    def list_actions(pr=Depends(principal)):
        pid, actor, via_agent = pr
        return [{"name": a.name, "summary": a.summary, "min_role": a.min_role.value}
                for a in core.facade.visible(actor, pid, via_agent)]

    @api.get("/p/openapi.json")
    def openapi(request: Request, pr=Depends(principal)):
        pid, actor, via_agent = pr
        return core.facade.openapi(actor, pid, str(request.base_url).rstrip("/"))

    @api.post("/p/actions/{name}")
    def call_action(name: str, body: dict | None = None, pr=Depends(principal)):
        pid, actor, via_agent = pr
        return {"result": core.facade.call(pid, actor, name, body or {}, via_agent=via_agent)}

    @api.get("/p/whoami")
    def whoami(pr=Depends(principal)):
        pid, actor, _ = pr
        return {"project": pid, "name": core.projects.get(pid)["name"], "role": actor.role.value, "label": actor.label,
                "scopes": actor.scopes}

    @api.get("/p/monitor/export.csv")
    def export_csv(status: str | None = None, category: str | None = None, pr=Depends(principal)):
        pid, actor, via_agent = pr
        out = core.facade.call(pid, actor, "monitor.export", {k: v for k, v in (("status", status), ("category", category)) if v},
                               via_agent)
        return Response(out["csv"], media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="leads-{pid}.csv"', "Cache-Control": "no-store"})

    @api.post("/ingest")
    def ingest(body: dict, pr=Depends(principal)):
        pid, actor, via_agent = pr
        return core.facade.call(pid, actor, "events.ingest",
                                {"source": body.get("source", actor.label), "kind": body.get("kind", "item"),
                                 "payload": body.get("payload", body)}, via_agent)

    # ------------------------------------------------------------------ MCP (streamable HTTP, stateless JSON)
    def mcp_handle(msg: dict, pr) -> dict | None:
        pid, actor, via_agent = pr
        mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
        if method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                res = {"protocolVersion": params.get("protocolVersion", "2025-03-26"), "capabilities": {"tools": {}},
                       "serverInfo": {"name": f"olian-{pid}", "version": "0.1.0"}}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": core.facade.mcp_tools(actor, pid, via_agent)}
            elif method == "tools/call":
                action = core.facade.mcp_name_to_action(params.get("name", ""))
                try:
                    if not action:
                        raise ActionError(f"unknown tool {params.get('name')!r}")
                    out = core.facade.call(pid, actor, action, params.get("arguments") or {}, via_agent=via_agent)
                    res = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}]}
                except (Forbidden, ActionError, ValueError) as e:  # tool errors are results, not protocol errors
                    res = {"isError": True, "content": [{"type": "text", "text": str(e)}]}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # noqa: BLE001
            log.exception("mcp")
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}

    @api.post("/mcp")
    async def mcp(request: Request, pr=Depends(principal)):
        body = await request.json()
        batch = body if isinstance(body, list) else [body]
        out = [r for r in [await run_in_threadpool(mcp_handle, m, pr) for m in batch] if r is not None]
        if not out:
            return Response(status_code=202)
        return JSONResponse(out if isinstance(body, list) else out[0])

    # ------------------------------------------------------------------ signed webhooks
    @api.post("/webhooks/{pid}/{source}")
    async def webhook(pid: str, source: str, request: Request, x_olian_signature: str | None = Header(None)):
        project = core.projects.get(pid)
        secret = project["cfg"].get("webhook_secret") or core.s.webhook_secret
        if not secret:
            raise HTTPException(503, "webhook secret not configured")
        raw = await request.body()
        want = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, x_olian_signature or ""):
            raise HTTPException(401, "bad signature")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw.decode(errors="replace")}
        eid = core.signals.ingest(pid, f"webhook:{source}", "webhook", payload if isinstance(payload, dict) else {"data": payload})
        return {"event_id": eid}

    # ------------------------------------------------------------------ WhatsApp Cloud inbound
    @api.get("/wa/webhook")
    def wa_verify(request: Request):
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and core.s.wa_verify_token and \
                hmac.compare_digest(q.get("hub.verify_token", ""), core.s.wa_verify_token):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403, "verification failed")

    @api.post("/wa/webhook")
    async def wa_inbound(request: Request, x_hub_signature_256: str | None = Header(None)):
        if not core.s.wa_app_secret:
            raise HTTPException(503, "WHATSAPP_APP_SECRET not configured")
        raw = await request.body()
        want = "sha256=" + hmac.new(core.s.wa_app_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, x_hub_signature_256 or ""):
            raise HTTPException(401, "bad signature")
        await run_in_threadpool(handle_whatsapp, json.loads(raw))
        return {"ok": True}

    def project_for_number(pnid: str) -> str | None:
        for s in core.db.q("SELECT project_id, config FROM senders WHERE channel='whatsapp_cloud'"):
            if json.loads(s["config"]).get("phone_number_id") == pnid:
                return s["project_id"]
        for p in core.projects.list():
            if p["cfg"].get("channels", {}).get("whatsapp_cloud", {}).get("phone_number_id") == pnid:
                return p["id"]
        return None

    def handle_whatsapp(payload: dict) -> None:
        for entry in payload.get("entry", []):
            for ch in entry.get("changes", []):
                v = ch.get("value", {})
                pid = project_for_number(v.get("metadata", {}).get("phone_number_id", ""))
                if not pid:
                    continue
                names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in v.get("contacts", [])}
                for m in v.get("messages", []):
                    sender, text = m.get("from", ""), (m.get("text") or {}).get("body", "")
                    if not text:
                        continue
                    role, _ = core.projects.role_of(pid, "whatsapp", sender)
                    if role:  # a teammate talking to the project agent
                        reply = core.chat.handle("whatsapp", sender, sender, text, names.get(sender, ""))
                        wa = core.notifier.whatsapp_for(core.projects.get(pid))
                        if wa and reply:
                            try:
                                wa.send_text(sender, reply)
                            except ChannelError as e:
                                log.warning("wa reply failed: %s", e)
                    else:  # a lead answering outreach
                        core.outreach.handle_reply(pid, "whatsapp", sender, text, names.get(sender, ""))

    return api


def _public(p: dict) -> dict:
    cfg = json.loads(json.dumps(p.get("cfg", {})))
    for ch in cfg.get("channels", {}).values():
        for k in ("bot_token", "token"):
            if k in ch:
                ch[k] = "***"
    for secret in ("env", "webhook_secret"):
        cfg.pop(secret, None)
    return {"id": p["id"], "name": p["name"], "goal": p["goal"], "api_port": p["api_port"], "status": p["status"],
            "running": p.get("running"), "config": cfg}
