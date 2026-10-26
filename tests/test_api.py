import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from olian.api import create_app

ADM = {"Authorization": "Bearer admin-key"}


@pytest.fixture
def client(core, proj):
    with TestClient(create_app(core, workers=False)) as c:
        yield c


def tok(core, proj, label="script", role="manager", scopes=""):
    return {"Authorization": "Bearer " + core.projects.issue_token(proj, label, role, scopes)}


def test_admin_api_needs_the_key(client):
    assert client.get("/projects").status_code == 401
    assert client.get("/projects", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/projects", headers=ADM).json()[0]["id"] == "alpha"
    assert client.get("/health").json()["status"] == "ok"


def test_admin_disabled_without_key(core, proj):
    core.s.api_key = ""
    with TestClient(create_app(core, workers=False)) as c:
        assert c.get("/projects", headers=ADM).status_code == 503


def test_create_project_over_http_hides_secrets(client, core):
    r = client.post("/projects", headers=ADM, json={"id": "beta", "name": "Beta", "goal": "g",
                                                    "config": {"channels": {"telegram": {"bot_token": "SECRETTOK"}}}})
    assert r.status_code == 200 and "SECRETTOK" not in r.text
    assert (core.s.hermes_root / "beta" / "config.yaml").exists()
    assert client.post("/projects", headers=ADM, json={"id": "beta"}).status_code == 400


def test_facade_over_rest_respects_token_role_and_scope(client, core, proj):
    ok = tok(core, proj)
    assert client.post("/p/actions/tasks.add", headers=ok, json={"title": "t"}).json()["result"]["title"] == "t"
    viewer = tok(core, proj, "v", "viewer")
    assert client.post("/p/actions/tasks.add", headers=viewer, json={"title": "t"}).status_code == 403
    assert client.post("/p/actions/tasks.list", headers=viewer, json={}).status_code == 200
    scoped = tok(core, proj, "rx", "manager", "leads.*")
    assert client.post("/p/actions/tasks.list", headers=scoped, json={}).status_code == 403
    assert client.post("/p/actions/leads.add", headers=scoped, json={"channel": "telegram", "address": "@a"}).status_code == 200
    assert client.post("/p/actions/tasks.add", headers=ok, json={}).status_code == 400
    assert client.post("/p/actions/tasks.list", json={}).status_code == 401


def test_token_is_bound_to_its_project(client, core, proj):
    core.projects.create("beta", "Beta")
    a = tok(core, proj)
    client.post("/p/actions/tasks.add", headers=a, json={"title": "alpha only"})
    b = {"Authorization": "Bearer " + core.projects.issue_token("beta", "script", "manager")}
    assert client.post("/p/actions/tasks.list", headers=b, json={}).json()["result"] == []


def test_openapi_lists_only_what_the_caller_may_do(client, core, proj):
    spec = client.get("/p/openapi.json", headers=tok(core, proj, "v", "viewer")).json()
    assert "/p/actions/tasks.list" in spec["paths"] and "/p/actions/tasks.add" not in spec["paths"]


def rpc(client, headers, method, params=None, id=1):
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})


def test_mcp_handshake_tools_and_calls(client, core, proj):
    agent_h = {"Authorization": "Bearer " + core.projects._current_agent_token(proj)}
    init = rpc(client, agent_h, "initialize", {"protocolVersion": "2025-03-26"}).json()["result"]
    assert init["serverInfo"]["name"] == "olian-alpha" and "tools" in init["capabilities"]
    assert client.post("/mcp", headers=agent_h, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    tools = {t["name"] for t in rpc(client, agent_h, "tools/list").json()["result"]["tools"]}
    assert {"tasks_add", "outreach_compose", "leads_add"} <= tools
    assert not tools & {"tasks_plan", "tasks_sweep", "outreach_approve", "outreach_draft", "audit_list", "chat_ask"}
    out = rpc(client, agent_h, "tools/call", {"name": "tasks_add", "arguments": {"title": "via mcp", "executor": "human",
              "steps": [{"text": "click", "copy": "paste"}]}}).json()["result"]
    assert not out.get("isError") and json.loads(out["content"][0]["text"])["steps"][0]["copy"] == "paste"
    bad = rpc(client, agent_h, "tools/call", {"name": "outreach_approve", "arguments": {"id": 1}}).json()["result"]
    assert bad["isError"]                                                    # the agent cannot approve its own drafts
    assert rpc(client, agent_h, "bogus").json()["error"]["code"] == -32601
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    assert core.db.one("SELECT 1 FROM audit WHERE actor='agent' AND action='tasks.add' AND ok=1")
    assert core.db.one("SELECT 1 FROM audit WHERE actor='agent' AND action='outreach.approve' AND ok=0")


def test_mcp_agent_is_limited_to_the_human_it_serves(client, core, proj):
    agent_h = {"Authorization": "Bearer " + core.projects._current_agent_token(proj)}
    with core.facade.acting(proj, __import__("olian.facade", fromlist=["Actor"]).Actor("vic", __import__("olian.rbac", fromlist=["Role"]).Role.VIEWER)):
        tools = {t["name"] for t in rpc(client, agent_h, "tools/list").json()["result"]["tools"]}
        assert "tasks_list" in tools and "tasks_add" not in tools
        res = rpc(client, agent_h, "tools/call", {"name": "tasks_add", "arguments": {"title": "x"}}).json()["result"]
        assert res["isError"]


def test_ingest_from_scraper_token(client, core, proj):
    h = tok(core, proj, "tgfarm", "manager", "events.ingest")
    r = client.post("/ingest", headers=h, json={"source": "farm", "kind": "message", "payload": {"text": "hi"}})
    assert r.status_code == 200 and core.signals.events(proj)[0]["source"] == "farm"
    assert client.post("/p/actions/tasks.add", headers=h, json={"title": "x"}).status_code == 403


def sig(secret, raw):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_signed_webhook(client, core, proj):
    raw = json.dumps({"text": "booking.com says hi"}).encode()
    r = client.post("/webhooks/alpha/booking", content=raw, headers={"X-Olian-Signature": sig("whsec", raw)})
    assert r.status_code == 200 and core.signals.events(proj)[0]["source"] == "webhook:booking"
    assert client.post("/webhooks/alpha/booking", content=raw, headers={"X-Olian-Signature": "sha256=bad"}).status_code == 401
    assert client.post("/webhooks/alpha/booking", content=raw).status_code == 401
    assert client.post("/webhooks/ghost/x", content=raw, headers={"X-Olian-Signature": sig("whsec", raw)}).status_code == 404


def wa_payload(frm, text, pnid="PN1"):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid},
            "contacts": [{"wa_id": frm, "profile": {"name": "Jan"}}],
            "messages": [{"from": frm, "type": "text", "text": {"body": text}}]}}]}]}


def wa_post(client, payload):
    raw = json.dumps(payload).encode()
    return client.post("/wa/webhook", content=raw, headers={"X-Hub-Signature-256": sig("appsecret", raw)})


def test_whatsapp_verify_and_signature(client):
    assert client.get("/wa/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=123").text == "123"
    assert client.get("/wa/webhook?hub.mode=subscribe&hub.verify_token=bad&hub.challenge=1").status_code == 403
    assert client.post("/wa/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401


def test_whatsapp_lead_reply_vs_team_chat(client, core, proj, agent, http):
    core.outreach.add_sender(proj, "wa", "whatsapp_cloud", {"phone_number_id": "PN1"})
    core.projects.update_config(proj, {"channels": {"whatsapp_cloud": {"phone_number_id": "PN1"}}})
    core.outreach.add_lead(proj, "whatsapp", "420777", name="Jan")
    agent.script = [json.dumps({"intent": "positive", "summary": "wants it", "reply": "great"})]
    assert wa_post(client, wa_payload("420777", "yes interested")).status_code == 200
    assert core.outreach.lead_by_addr(proj, "whatsapp", "420777")["status"] == "qualified"
    # a teammate (member on whatsapp) writes to the same number: that is a chat with the agent, not a lead
    core.projects.add_member(proj, "whatsapp", "421999", "manager", "mia")
    assert wa_post(client, wa_payload("421999", "/status")).status_code == 200
    reply = http.sent_to("PN1/messages")[-1]["json"]
    assert reply["to"] == "421999" and "Alpha Villas" in reply["text"]["body"]
    assert core.outreach.lead_by_addr(proj, "whatsapp", "421999") is None
    assert wa_post(client, wa_payload("1", "x", pnid="UNKNOWN")).status_code == 200   # unknown number: ignored


def test_chat_endpoint_and_binding(client, core, proj):
    client.post("/projects/alpha/bindings", headers=ADM, json={"platform": "telegram", "chat_id": "-5"})
    r = client.post("/chat", headers=ADM, json={"platform": "telegram", "chat_id": "-5", "user_id": "2", "text": "/status"})
    assert "Alpha Villas" in r.json()["reply"]


# ----------------------------------------------------------------------------- monitoring panel & endpoints
def test_panel_is_served_with_strict_csp_and_no_unsafe_dom_sinks(client):
    for path, kind in (("/admin", "text/html"), ("/admin/app.js", "javascript"), ("/admin/app.css", "text/css")):
        r = client.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"]
        csp = r.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    js = client.get("/admin/app.js").text
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in js, sink
    assert "<script>" not in client.get("/admin").text                      # no inline script: CSP would block it anyway


def test_whoami_and_admin_key_on_a_named_project(client, core, proj):
    r = client.get("/p/whoami", headers=tok(core, proj, "op", "manager"))
    assert r.json() == {"project": "alpha", "name": "Alpha Villas", "role": "manager", "label": "op", "scopes": ""}
    adm = {"Authorization": "Bearer admin-key", "X-Olian-Project": "alpha"}
    assert client.get("/p/whoami", headers=adm).json()["role"] == "admin"
    assert client.get("/p/whoami", headers={"Authorization": "Bearer admin-key"}).status_code == 401      # needs a project
    assert client.get("/p/whoami", headers={**adm, "X-Olian-Project": "ghost"}).status_code == 404
    assert client.get("/p/whoami", headers={"Authorization": "Bearer wrong", "X-Olian-Project": "alpha"}).status_code == 401


def test_csv_endpoint_respects_roles_and_is_formula_safe(client, core, proj):
    core.projects.update_config(proj, {"monitor": {"enabled": True}})
    core.monitor.save_rule(proj, {"name": "r", "phrases": ["вілл*"]})
    core.monitor.add_source(proj, "-100111", title="S")
    core.monitor.ingest_message(proj, "-100111", 1, "=2+2 шукаю віллу, підкажіть будь ласка скільки коштує?", author_id=1)
    core.monitor.process_pending(proj)
    r = client.get("/p/monitor/export.csv?status=new", headers=tok(core, proj, "op", "manager"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    assert "'=2+2" in r.text and ",=2+2" not in r.text
    assert client.get("/p/monitor/export.csv", headers=tok(core, proj, "v", "viewer")).status_code == 403
    assert client.get("/p/monitor/export.csv").status_code == 401


def test_monitor_actions_over_rest_follow_roles(client, core, proj):
    admin, mgr, view = tok(core, proj, "a", "admin"), tok(core, proj, "m", "manager"), tok(core, proj, "v", "viewer")
    rule = {"rule": {"name": "n", "phrases": ["x"]}}
    assert client.post("/p/actions/monitor.rules.save", headers=admin, json=rule).status_code == 200
    assert client.post("/p/actions/monitor.rules.save", headers=mgr, json=rule).status_code == 403
    assert client.post("/p/actions/monitor.rules.list", headers=view, json={}).status_code == 200
    assert client.post("/p/actions/monitor.sources.add", headers=admin, json={"chat_ref": "-1"}).status_code == 200
    assert client.post("/p/actions/monitor.sources.add", headers=admin, json={"chat_ref": "-1"}).status_code == 400   # duplicate
    assert client.post("/p/actions/monitor.report", headers=view, json={"days": 7}).json()["result"]["received"] == 0
    agent_h = {"Authorization": "Bearer " + core.projects._current_agent_token(proj)}
    assert client.post("/p/actions/monitor.leads.list", headers=agent_h, json={}).status_code == 403   # agent is kept out
