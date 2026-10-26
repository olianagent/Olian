"""Thin clients for external messaging APIs. `http` is injectable so everything is testable offline."""
from __future__ import annotations

import os
from typing import Any

import httpx


class ChannelError(RuntimeError):
    pass


class TelegramError(ChannelError):
    """Telegram Bot API error. `status` is the HTTP/API code, `retry_after` seconds when rate limited (429)."""

    def __init__(self, status: int, description: str, retry_after: int = 0):
        super().__init__(f"telegram {status}: {description}")
        self.status, self.description, self.retry_after = status, description, retry_after

    @property
    def is_auth(self) -> bool:
        return self.status == 401

    @property
    def is_rate_limit(self) -> bool:
        return self.status == 429

    @property
    def is_access(self) -> bool:  # bot removed / chat gone / forbidden: stop for this source, never route around it
        return self.status in (400, 403, 404)

    @property
    def is_conflict(self) -> bool:  # another consumer of getUpdates or an active webhook
        return self.status == 409

    @property
    def is_transient(self) -> bool:
        return self.status >= 500 or self.status == 0


class Telegram:
    def __init__(self, token: str, http: Any = httpx):
        self.token, self.http = token, http

    def _url(self, method: str) -> str:
        # OLIAN_TELEGRAM_API lets you point at a self-hosted Bot API server (or a test double)
        return f"{os.environ.get('OLIAN_TELEGRAM_API', 'https://api.telegram.org').rstrip('/')}/bot{self.token}/{method}"

    def _call(self, method: str, body: dict, timeout: int = 20) -> Any:
        try:
            r = self.http.post(self._url(method), json=body, timeout=timeout)
        except httpx.HTTPError as e:  # never include the URL: it carries the token
            raise TelegramError(0, f"network error ({type(e).__name__})") from None
        if r.status_code != 200:
            desc, retry = r.text[:200], 0
            try:
                j = r.json()
                desc = str(j.get("description", desc))
                retry = int((j.get("parameters") or {}).get("retry_after", 0))
            except (ValueError, AttributeError, TypeError):
                pass
            raise TelegramError(r.status_code, desc, retry)
        return r.json().get("result")

    def send(self, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        body: dict = {"chat_id": chat_id, "text": text[:4096]}
        if reply_markup:
            body["reply_markup"] = reply_markup
        self._call("sendMessage", body)

    def updates(self, offset: int, timeout: int = 25, allowed: list[str] | None = None) -> list[dict]:
        body: dict = {"offset": offset, "timeout": timeout}
        if allowed:
            body["allowed_updates"] = allowed
        return self._call("getUpdates", body, timeout + 10) or []

    def get_me(self) -> dict:
        return self._call("getMe", {})

    def get_chat(self, chat_id: str) -> dict:
        return self._call("getChat", {"chat_id": chat_id})


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
