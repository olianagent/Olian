"""Composition root + background worker."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

from . import hermes
from .actions import register_all
from .config import Settings
from .db import DB
from .facade import Actor, Facade
from .notify import Notifier
from .monitor import Monitor
from .outreach import Outreach
from .projects import Projects
from .rbac import Role
from .scrapers import Scrapers
from .signals import Signals
from .tasks import Tasks

log = logging.getLogger("olian")


_PATTERNS = [(re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"), "bot***"),
             (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer ***"),
             (re.compile(r"olp_[A-Za-z0-9_-]{16,}"), "olp_***")]


def redact(text: str) -> str:
    """Telegram bot tokens and bearer/API tokens must never reach the logs."""
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


def install_log_redaction() -> None:
    """Wrap the record factory so EVERY record (any logger, any handler) is redacted before formatting."""
    old = logging.getLogRecordFactory()
    if getattr(old, "_olian_redacting", False):
        return

    def factory(*a, **kw):
        rec = old(*a, **kw)
        try:
            rec.msg, rec.args = redact(rec.getMessage()), ()
        except Exception:  # noqa: BLE001  (never break logging)
            pass
        return rec
    factory._olian_redacting = True
    logging.setLogRecordFactory(factory)


SYSTEM = Actor("system", Role.MANAGER)
# Used whenever the prompt contains text written by strangers: the agent may not call any Olian tool during that turn.
NO_TOOLS = Actor("untrusted-text", Role.VIEWER, "-none-")


class App:
    def __init__(self, settings: Settings | None = None, http=None):
        self.s = settings or Settings()
        self.s.ensure_dirs()
        install_log_redaction()
        self.db = DB(self.s.db_path)
        self.runtime = hermes.Runtime(self.s)
        self.projects = Projects(self.db, self.s, self.runtime)
        self.facade = Facade(self)
        register_all(self.facade)
        kw = {"http": http} if http is not None else {}
        self.notifier = Notifier(self, **kw)
        self.tasks = Tasks(self)
        self.outreach = Outreach(self, **kw)
        self.scrapers = Scrapers(self, **kw)
        self.http = http
        self.signals = Signals(self)
        self.monitor = Monitor(self, **kw)
        from .tguser import Accounts
        self.tguser = Accounts(self)
        from .chat import Chat  # late: chat imports facade/actions
        self.chat = Chat(self)
        self.client_factory: Callable[[dict], hermes.Client] = lambda project: hermes.Client(
            project, base_url=project["cfg"].get("agent", {}).get("base_url"))
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # one entry point for "talk to this project's agent"; serialised per project and scoped to the acting human
    def agent_chat(self, pid: str, message: str, session: str | None = None, actor: Actor | None = None) -> str:
        project = self.projects.get(pid)
        with self.facade.acting(pid, actor or SYSTEM):
            return self.client_factory(project).chat(message, session=session)

    def complete(self, pid: str, prompt: str, system: str = "", session: str | None = None) -> str:
        """One text completion over UNTRUSTED input. Prefers a direct endpoint without tools (cfg.llm / monitor.llm); otherwise
        the project's agent with every Olian tool switched off. (Hermes' own built-in tools remain: configure `llm` to avoid that.)"""
        cfg = self.projects.get(pid)["cfg"]
        llm = cfg.get("llm") or cfg.get("monitor", {}).get("llm") or {}
        if llm.get("base_url") and llm.get("model"):
            from .qualify import chat_complete
            return chat_complete(llm, self.http or __import__("httpx"), system, prompt)
        return self.agent_chat(pid, (system + "\n\n" if system else "") + prompt, session=session, actor=NO_TOOLS)

    # ----- background
    def tick(self) -> None:
        """One pass of autonomous work for every project. Errors in one step never stop the others."""
        for p in self.projects.list():
            pid = p["id"]
            for name, step in (("scrapers", lambda: self.scrapers.run_due(pid)),
                               ("signals", lambda: self.signals.process(pid)),
                               ("stale", lambda: self.outreach.sweep_stale(pid)),
                               ("touches", lambda: self.outreach.touch_plan(pid)),
                               ("send", lambda: self.outreach.send_due(pid)),
                               ("monitor", lambda: self.monitor.maintenance(pid))):
                try:
                    step()
                except Exception:  # noqa: BLE001
                    log.exception("tick %s/%s failed", pid, name)

    def start_workers(self, interval: float = 30, poll_interval: float = 5) -> None:
        def loop():
            while not self._stop.wait(interval):
                self.tick()
        t = threading.Thread(target=loop, name="olian-worker", daemon=True)
        t.start()
        self._threads.append(t)

        def monitor_loop():  # near-real-time: short polls, errors never kill the loop
            while not self._stop.wait(poll_interval):
                for p in self.projects.list():
                    try:
                        self.monitor.poll(p["id"])
                    except Exception:  # noqa: BLE001
                        log.exception("monitor poll %s failed", p["id"])
        m = threading.Thread(target=monitor_loop, name="olian-monitor", daemon=True)
        m.start()
        self._threads.append(m)
        from .bridge import TelegramBridge
        self.tguser.start_all()  # connect connected accounts so replies are heard
        self.telegram = TelegramBridge(self)
        self._threads += self.telegram.start()

    def shutdown(self) -> None:
        self._stop.set()
        self.tguser.shutdown()
        self.runtime.stop_all()
