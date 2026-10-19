"""Composition root + background worker."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import hermes
from .actions import register_all
from .config import Settings
from .db import DB
from .facade import Actor, Facade
from .notify import Notifier
from .outreach import Outreach
from .projects import Projects
from .rbac import Role
from .scrapers import Scrapers
from .signals import Signals
from .tasks import Tasks

log = logging.getLogger("olian")
SYSTEM = Actor("system", Role.MANAGER)


class App:
    def __init__(self, settings: Settings | None = None, http=None):
        self.s = settings or Settings()
        self.s.ensure_dirs()
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
        self.signals = Signals(self)
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

    # ----- background
    def tick(self) -> None:
        """One pass of autonomous work for every project. Errors in one step never stop the others."""
        for p in self.projects.list():
            pid = p["id"]
            for name, step in (("scrapers", lambda: self.scrapers.run_due(pid)),
                               ("signals", lambda: self.signals.process(pid)),
                               ("stale", lambda: self.outreach.sweep_stale(pid)),
                               ("touches", lambda: self.outreach.touch_plan(pid)),
                               ("send", lambda: self.outreach.send_due(pid))):
                try:
                    step()
                except Exception:  # noqa: BLE001
                    log.exception("tick %s/%s failed", pid, name)

    def start_workers(self, interval: float = 30) -> None:
        def loop():
            while not self._stop.wait(interval):
                self.tick()
        t = threading.Thread(target=loop, name="olian-worker", daemon=True)
        t.start()
        self._threads.append(t)
        from .bridge import TelegramBridge
        self.telegram = TelegramBridge(self)
        self._threads += self.telegram.start()

    def shutdown(self) -> None:
        self._stop.set()
        self.runtime.stop_all()
