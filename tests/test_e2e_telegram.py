"""End to end over REAL HTTP against a local Bot API double: real httpx, real JSON, real 429 bodies."""
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from olian.app import App
from olian.config import Settings

TOKEN = "999999:E2E_MONITOR_TOKEN_abcdefghijklmnopqrstuv"
CHAT = -1001234567890


class FakeBotApi:
    def __init__(self):
        self.updates, self.sent, self.calls, self.fail_next = [], [], [], []
        self.app = FastAPI()
        a = self.app

        @a.post("/bot{token}/{method}")
        async def call(token: str, method: str, request: Request):
            body = await request.json()
            self.calls.append((token, method, body))
            if token != TOKEN and method == "getUpdates":
                return JSONResponse({"ok": False, "description": "Unauthorized"}, 401)
            if self.fail_next:
                code, payload = self.fail_next.pop(0)
                return JSONResponse(payload, code)
            if method == "getUpdates":
                off = body.get("offset", 0)
                return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= off]}
            if method == "getChat":
                return {"ok": True, "result": {"id": body["chat_id"], "type": "supergroup", "title": "Bali Travel", "username": "balitravel"}}
            if method == "sendMessage":
                self.sent.append(body)
                return {"ok": True, "result": {"message_id": 1}}
            return {"ok": True, "result": True}


@pytest.fixture
def botapi(monkeypatch):
    fake = FakeBotApi()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(fake.app, port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    monkeypatch.setenv("OLIAN_TELEGRAM_API", f"http://127.0.0.1:{port}")
    yield fake
    server.should_exit = True


@pytest.fixture
def live(tmp_path, botapi):
    core = App(Settings(data_dir=tmp_path / "d", api_key="k", hermes_mode="external", telegram_notify_token=TOKEN))  # real httpx
    core.projects.create("bali", "Bali")
    core.projects.update_config("bali", {"monitor": {"enabled": True, "bot_token": TOKEN}})
    core.projects.add_member("bali", "telegram", "424242", "manager", "mia")
    core.monitor.save_rule("bali", {"name": "stay", "category": "stay", "phrases": ["вілл*"], "require_any": ["шукаю", "підкажіть"]})
    core.monitor.add_source("bali", str(CHAT))
    return core


def msg(uid, text, user=7):
    return {"update_id": uid, "message": {"message_id": uid, "date": int(time.time()), "chat": {"id": CHAT, "type": "supergroup", "title": "Bali Travel", "username": "balitravel"},
                                           "from": {"id": user, "is_bot": False}, "text": text}}


def test_message_to_lead_to_operator_notification(live, botapi):
    botapi.updates = [msg(1, "Шукаю віллу на Балі на 5 осіб в листопаді, підкажіть будь ласка, скільки коштує?"),
                      msg(2, "Всім привіт!"), msg(3, "Пропонуємо оренду вілл, знижка 20%! Звертайтесь")]
    live.monitor.poll("bali")
    leads = live.monitor.leads("bali")
    assert len(leads) == 1 and leads[0]["message_url"] == "https://t.me/balitravel/1"      # public username learned from the update
    note = [b for b in botapi.sent if b["chat_id"] == "424242"]
    assert note and "Новий лід" in note[0]["text"] and "https://t.me/balitravel/1" in note[0]["text"]
    live.monitor.poll("bali")                                                          # offset was confirmed: no re-delivery
    assert len(live.monitor.leads("bali")) == 1
    getu = [c for c in botapi.calls if c[1] == "getUpdates"]
    assert getu[-1][2]["offset"] == 4 and getu[0][2]["timeout"] == 0


def test_real_429_body_is_honoured(live, botapi):
    botapi.fail_next = [(429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 7", "parameters": {"retry_after": 7}})]
    t0 = time.time()
    res = live.monitor.collect("bali", t0)
    assert "7s" in res["skipped"] and abs(float(live.monitor._state("bali", "poll_next")) - (t0 + 7)) < 1
    n = len([c for c in botapi.calls if c[1] == "getUpdates"])
    live.monitor.collect("bali", t0 + 3)
    assert len([c for c in botapi.calls if c[1] == "getUpdates"]) == n                # no request during the wait


def test_rejected_token_stops_everything_and_never_leaks(live, botapi, caplog):
    live.projects.update_config("bali", {"monitor": {"bot_token": "111:WRONG_TOKEN_abcdefghijklmnopqrstuvwxyz"}})
    res = live.monitor.collect("bali")
    assert res["skipped"] == "auth_error" and live.monitor.sources("bali")[0]["status"] == "auth_error"
    assert "WRONG_TOKEN" not in str(res) and "WRONG_TOKEN" not in caplog.text


def test_source_health_check_over_http(live, botapi):
    res = live.monitor.check_sources("bali", time.time() + 4000)
    s = live.monitor.sources("bali")[0]
    assert res["checked"] == 1 and s["username"] == "balitravel" and s["title"] == "Bali Travel"
    botapi.fail_next = [(403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked from the supergroup chat"})]
    live.monitor.check_sources("bali", time.time() + 9000)
    assert live.monitor.sources("bali")[0]["status"] == "unavailable"
