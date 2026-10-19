"""Thin clients for external messaging APIs. `http` is injectable so everything is testable offline."""
from __future__ import annotations

from typing import Any

import httpx


class ChannelError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, http: Any = httpx):
        self.token, self.http = token, http

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        body: dict = {"chat_id": chat_id, "text": text[:4096]}
        if reply_markup:
            body["reply_markup"] = reply_markup
        r = self.http.post(self._url("sendMessage"), json=body, timeout=20)
        if r.status_code != 200:
            raise ChannelError(f"telegram {r.status_code}: {r.text[:200]}")

    def updates(self, offset: int, timeout: int = 25) -> list[dict]:
        r = self.http.post(self._url("getUpdates"), json={"offset": offset, "timeout": timeout}, timeout=timeout + 10)
        if r.status_code != 200:
            raise ChannelError(f"telegram {r.status_code}: {r.text[:200]}")
        return r.json().get("result", [])


class WhatsAppCloud:
    """Meta's official Business Cloud API. Cold outreach MUST use this (not an unofficial bridge)."""

    def __init__(self, phone_number_id: str, token: str, http: Any = httpx, version: str = "v21.0"):
        self.pnid, self.token, self.http, self.version = phone_number_id, token, http, version

    def _post(self, payload: dict) -> str:
        r = self.http.post(f"https://graph.facebook.com/{self.version}/{self.pnid}/messages",
                           headers={"Authorization": f"Bearer {self.token}"}, json=payload, timeout=30)
        if r.status_code >= 300:
            raise ChannelError(f"whatsapp {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["messages"][0]["id"]
        except Exception:
            return ""

    def send_text(self, to: str, text: str) -> str:
        return self._post({"messaging_product": "whatsapp", "to": to, "type": "text",
                           "text": {"body": text[:4096], "preview_url": False}})

    def send_template(self, to: str, name: str, lang: str, params: list[str]) -> str:
        comp = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}] if params else []
        return self._post({"messaging_product": "whatsapp", "to": to, "type": "template",
                           "template": {"name": name, "language": {"code": lang}, "components": comp}})
