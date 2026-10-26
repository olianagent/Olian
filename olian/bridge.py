"""Telegram long-poll bridge: one poller per project bot, routed through Chat (RBAC + audit)."""
from __future__ import annotations

import logging
import threading

import httpx

from .channels import ChannelError, Telegram, TelegramError

log = logging.getLogger("olian.telegram")


class TelegramBridge:
    def __init__(self, app, http=httpx):
        self.app, self.http = app, http

    def start(self) -> list[threading.Thread]:
        threads = []
        for p in self.app.projects.list():
            tg = p["cfg"].get("channels", {}).get("telegram", {})
            if tg.get("bot_token") and tg.get("via", "olian") == "olian":
                t = threading.Thread(target=self._loop, args=(p["id"], tg["bot_token"]), name=f"tg-{p['id']}", daemon=True)
                t.start()
                threads.append(t)
        return threads

    def _loop(self, pid: str, token: str) -> None:
        api, offset = Telegram(token, self.http), 0
        while not self.app._stop.is_set():
            try:
                for u in api.updates(offset):
                    offset = u["update_id"] + 1
                    self.handle_update(pid, api, u)
            except TelegramError as e:
                log.warning("telegram %s: %s", pid, e)
                if e.is_auth:  # a rejected token will not become valid by retrying: stop this bot's loop
                    return
                self.app._stop.wait(max(e.retry_after, 1) if e.is_rate_limit else 30 if e.is_conflict else 5)
            except (ChannelError, httpx.HTTPError) as e:
                log.warning("telegram %s: %s", pid, e)
                self.app._stop.wait(5)

    def handle_update(self, pid: str, api: Telegram, u: dict) -> None:
        msg = u.get("message") or {}
        text = msg.get("text")
        if not text:
            return
        user = msg.get("from", {})
        reply = self.app.chat.handle("telegram", str(msg["chat"]["id"]), str(user.get("id", "")), text,
                                     name=user.get("username") or user.get("first_name", ""))
        if reply:
            api.send(str(msg["chat"]["id"]), reply)
