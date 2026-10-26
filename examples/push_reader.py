#!/usr/bin/env python3
"""READ-ONLY Telegram reader for ingestion channel B (docs/TZ-telegram-monitoring.md, ADR-1).

Use ONLY after you have a documented decision that this use is permitted for your groups and legal basis. It:
  * listens ONLY to chats that are approved sources in Olian (it fetches the list; it cannot add its own),
  * never sends, joins, forwards, reacts, or edits anything (there is no write call in this file; a test enforces it),
  * uses ONE session (your own account's, or a dedicated one you own), never rotates accounts, never uses proxies,
  * waits out FloodWait exactly as long as Telegram says, and on lost access REPORTS the source as unavailable /
    auth_error to Olian and stops that source instead of working around it,
  * refuses to start if the session file is readable by other users.

Setup:  pip install telethon httpx
  export TG_API_ID=... TG_API_HASH=...            # from my.telegram.org, issued to YOU
  export OLIAN_URL=https://olian.example.com OLIAN_TOKEN=olp_...   # scoped: monitor.ingest,monitor.sources.list,monitor.source_status
  python examples/push_reader.py                  # first run asks for the login code interactively
Server side: project config monitor.push_enabled=true and sources added with ingest="push".
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
import time

SESSION = os.environ.get("TG_SESSION", "olian_reader")


def to_payload(chat_id, message_id: int, text: str, date_ts: float, sender_id) -> dict:
    """Map a Telegram message to the Olian monitor.ingest arguments (pure; unit-tested)."""
    return {"chat_ref": str(chat_id), "message_id": int(message_id), "text": text or "",
            "date": float(date_ts), "author_id": str(sender_id) if sender_id is not None else None}


def session_is_private(path: str) -> bool:
    try:
        return os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    except FileNotFoundError:
        return True  # created fresh on first login (Telethon creates it 0600-ish; we re-check after login)


class Olian:
    def __init__(self, url: str, token: str):
        import httpx
        self.c = httpx.AsyncClient(base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=30)

    async def call(self, action: str, args: dict | None = None):
        r = await self.c.post(f"/p/actions/{action}", json=args or {})
        r.raise_for_status()
        return r.json()["result"]


async def main() -> int:
    from telethon import TelegramClient, events
    from telethon.errors import (AuthKeyError, ChannelPrivateError, ChatForbiddenError, FloodWaitError,
                                 SessionRevokedError, UserDeactivatedBanError)

    olian = Olian(os.environ["OLIAN_URL"], os.environ["OLIAN_TOKEN"])
    session_file = SESSION + ".session"
    if not session_is_private(session_file):
        print(f"{session_file} is readable by other users: chmod 600 it (a session file IS the account). Refusing to start.")
        return 2
    client = TelegramClient(SESSION, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.start()
    os.chmod(session_file, 0o600)

    def approved():
        return {s["chat_ref"]: s for s in sources if s["ingest"] == "push" and s["status"] == "active"}

    sources = await olian.call("monitor.sources.list")
    if not approved():
        print("no approved push sources in Olian; nothing to read. Approve sources in the panel first.")
        return 0

    async def report(ref: str, status: str, reason: str):
        s = next((x for x in sources if x["chat_ref"] == ref), None)
        if s:
            await olian.call("monitor.source_status", {"id": s["id"], "status": status, "reason": reason})

    @client.on(events.NewMessage(chats=[int(r) for r in approved()]))
    async def on_message(event):
        m = event.message
        if not m.message or (m.sender and getattr(m.sender, "bot", False)):
            return
        try:
            await olian.call("monitor.ingest", to_payload(event.chat_id, m.id, m.message, m.date.timestamp(), m.sender_id))
        except Exception as e:  # noqa: BLE001  Olian down: Telegram redelivers nothing, so log; ingestion is idempotent
            print("ingest failed:", type(e).__name__, file=sys.stderr)

    print(f"reading {len(approved())} approved chat(s); read-only. Ctrl+C to stop.")
    while True:
        try:
            await client.run_until_disconnected()
            return 0
        except FloodWaitError as e:  # obey exactly, no retry storm, no second account
            print(f"FloodWait {e.seconds}s: waiting")
            await asyncio.sleep(e.seconds + 1)
        except (ChannelPrivateError, ChatForbiddenError):
            for ref in list(approved()):
                await report(ref, "unavailable", "Telegram reports no access to the chat")
            print("lost access: reported to Olian; not trying to regain it by other means")
            return 3
        except (AuthKeyError, SessionRevokedError, UserDeactivatedBanError) as e:
            for ref in list(approved()):
                await report(ref, "auth_error", f"{type(e).__name__}: session unusable")
            print("session unusable: reported to Olian and stopping (no replacement account will be used)")
            return 4


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
