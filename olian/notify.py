"""Tell humans things (new task, draft to approve, qualified lead) in the channels they actually use."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .channels import ChannelError, Telegram, WhatsAppCloud
from .rbac import Role

log = logging.getLogger("olian.notify")
_RANK = {"viewer": 1, "manager": 2, "admin": 3}


class Notifier:
    def __init__(self, app, http: Any = httpx):
        self.app, self.http = app, http

    def telegram_for(self, project: dict) -> Telegram | None:
        tok = project["cfg"].get("channels", {}).get("telegram", {}).get("bot_token") or self.app.s.telegram_notify_token
        return Telegram(tok, self.http) if tok else None

    def whatsapp_for(self, project: dict) -> WhatsAppCloud | None:
        w = project["cfg"].get("channels", {}).get("whatsapp_cloud", {})
        tok = w.get("token") or self.app.s.wa_token
        return WhatsAppCloud(w["phone_number_id"], tok, self.http) if w.get("phone_number_id") and tok else None

    def send(self, pid: str, text: str, min_role: str = "manager", only: list[str] | None = None) -> int:
        """Deliver to project members with at least `min_role` (or the named user ids). Returns delivered count."""
        project = self.app.projects.get(pid)
        sent = 0
        for m in self.app.projects.members(pid):
            if _RANK[m["role"]] < _RANK[min_role] or (only and m["user_id"] not in only and m["name"] not in only):
                continue
            try:
                if m["platform"] == "telegram" and (tg := self.telegram_for(project)):
                    tg.send(m["user_id"], text)
                    sent += 1
                elif m["platform"] == "whatsapp" and (wa := self.whatsapp_for(project)):
                    wa.send_text(m["user_id"], text)
                    sent += 1
            except (ChannelError, httpx.HTTPError) as e:
                log.warning("notify %s/%s failed: %s", pid, m["user_id"], e)
        return sent
