import json
import time
from datetime import datetime, timezone

import pytest

from olian.app import App
from olian.config import Settings


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data = status, data if data is not None else {}
        self.text = text or json.dumps(self._data)

    def json(self):
        return self._data


class FakeHTTP:
    """Stands in for httpx: records POSTs (Telegram/WhatsApp/webhook senders), serves canned GETs (scrapers)."""

    def __init__(self):
        self.posts, self.pages, self.fail = [], {}, False
        self.fail_status, self.fail_text = 500, 'boom'
        self.tg = {}  # Telegram method -> queue of Resp | Exception (consumed in order); default: ok/empty

    def post(self, url, **kw):
        self.posts.append({"url": url, **kw})
        method = url.rsplit("/", 1)[-1]
        if "api.telegram.org" in url and self.tg.get(method):
            item = self.tg[method].pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if self.fail:
            return Resp(self.fail_status, text=self.fail_text)
        return Resp(200, {"messages": [{"id": "wamid.1"}], "result": []})

    def get(self, url, **kw):
        if url not in self.pages:
            return Resp(404, text="nf")
        return Resp(200, text=self.pages[url])

    def sent_to(self, needle):
        return [p for p in self.posts if needle in p["url"]]


class FakeAgent:
    def __init__(self):
        self.script, self.calls = [], []  # script: list of replies or callables(message)->str

    def chat(self, message, session=None, system=None):
        self.calls.append((session, message))
        if not self.script:
            return "ok"
        r = self.script.pop(0)
        return r(message) if callable(r) else r

    def healthy(self):
        return True


@pytest.fixture
def http():
    return FakeHTTP()


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
def core(tmp_path, http, agent):
    s = Settings(data_dir=tmp_path / "data", api_key="admin-key", webhook_secret="whsec", wa_verify_token="vt",
                 wa_app_secret="appsecret", wa_token="watoken", telegram_notify_token="tgtoken", hermes_mode="external")
    app = App(s, http=http)
    app.client_factory = lambda project: agent
    return app


@pytest.fixture
def noon():
    return datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def proj(core):
    """Project 'alpha' with an admin, a manager and a viewer on Telegram."""
    core.projects.create("alpha", "Alpha Villas", "Fill 40 villas", {"channels": {"telegram": {"bot_token": "T"}},
                                                                    "outreach": {"min_interval_seconds": 0}})
    core.projects.add_member("alpha", "telegram", "1", "admin", "boss")
    core.projects.add_member("alpha", "telegram", "2", "manager", "mia")
    core.projects.add_member("alpha", "telegram", "3", "viewer", "vic")
    return "alpha"


def tg_ok(result):
    return Resp(200, {"ok": True, "result": result})


def tg_err(status, description="err", retry_after=0):
    body = {"ok": False, "description": description}
    if retry_after:
        body["parameters"] = {"retry_after": retry_after}
    return Resp(status, body)


def tg_msg(update_id, chat_id, text, msg_id=None, user=7, username=None, chat_type="supergroup", is_bot=False, key="message"):
    chat = {"id": chat_id, "type": chat_type, "title": "Bali Travel"}
    if username:
        chat["username"] = username
    m = {"message_id": msg_id or update_id, "date": 1_790_000_000 + update_id, "chat": chat, "text": text}
    if user is not None:
        m["from"] = {"id": user, "is_bot": is_bot}
    return {"update_id": update_id, key: m}


MON_RULES = [
    {"name": "stay", "category": "accommodation", "phrases": ["вілл*", "вилл*", "глемпінг*", "villa*", "glamping", "жиль*"],
     "exclude": ["продам", "for sale"]},
]


@pytest.fixture
def mon(core, proj):
    """Project 'alpha' with monitoring configured (own bot), one approved supergroup and one rule."""
    core.projects.update_config(proj, {"monitor": {"enabled": True, "bot_token": "999999:MONITORBOTTOKEN_abcdefghijklmnop",
                                                    "profile": "Travellers looking for stays in Bali"}})
    for r in MON_RULES:
        core.monitor.save_rule(proj, r)
    core.monitor.add_source(proj, "-1001234567890", title="Bali Travel")
    return core.monitor
