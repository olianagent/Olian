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

    def post(self, url, **kw):
        self.posts.append({"url": url, **kw})
        if self.fail:
            return Resp(500, text="boom")
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
