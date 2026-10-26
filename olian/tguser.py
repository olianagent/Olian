"""Telegram USER accounts: the senders of private messages and the listeners for replies.

How an account is connected (no physical phone is kept around):
  1. you give a phone number + api_id/api_hash (from my.telegram.org, issued to the account owner),
  2. Telegram sends a login code to that number (SMS or an already-logged-in Telegram app) and you enter it once,
  3. the resulting session string is stored ENCRYPTED (OLIAN_SESSION_KEY) and reused: no phone, no code, no browser.
A session string is a password to the account. It never leaves the server unencrypted, never goes into logs or audit.

What is deliberately NOT here: proxies, device/fingerprint spoofing, account "warm-up" simulation, buying sessions,
moving a person to another account when one is limited. Telegram can still restrict any account that messages
strangers; the controls live in outreach.py (caps, ramp, breaker, stop-list).
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Any, Callable

from .channels import ChannelError

log = logging.getLogger("olian.tguser")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")
PHONE_RE = re.compile(r"^\+\d{8,15}$")
API_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
LOGIN_TTL = 600


class DeliveryError(ChannelError):
    """kind: peer_flood | flood_wait | privacy | blocked | unreachable | auth | other"""

    def __init__(self, kind: str, message: str, wait: int = 0):
        super().__init__(f"{kind}: {message}")
        self.kind, self.wait = kind, wait


# --------------------------------------------------------------------------- Telethon adapter
class _Loop:
    """One asyncio loop in a thread shared by all accounts; the rest of Olian stays synchronous."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="olian-tguser-loop", daemon=True).start()

    def run(self, coro, timeout: float = 90):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


_LOOP: _Loop | None = None


def shared_loop() -> _Loop:
    global _LOOP
    if _LOOP is None:
        _LOOP = _Loop()
    return _LOOP


def map_error(e: Exception) -> Exception:
    """Telethon exception -> our DeliveryError kinds. Unknown errors are returned unchanged."""
    from telethon import errors as E
    names = lambda *n: tuple(getattr(E, x) for x in n if hasattr(E, x))  # noqa: E731
    if isinstance(e, E.FloodWaitError):
        return DeliveryError("flood_wait", f"Telegram asks to wait {e.seconds}s", wait=int(e.seconds))
    if isinstance(e, E.PeerFloodError):
        return DeliveryError("peer_flood", "PEER_FLOOD: the account is limited for messaging non-contacts")
    if isinstance(e, names("UserPrivacyRestrictedError", "ChatWriteForbiddenError")):
        return DeliveryError("privacy", "the user does not accept messages from this account")
    if isinstance(e, names("UserIsBlockedError", "YouBlockedUserError")):
        return DeliveryError("blocked", "the user blocked this account (or vice versa)")
    if isinstance(e, names("PeerIdInvalidError", "UsernameNotOccupiedError", "UsernameInvalidError",
                           "InputUserDeactivatedError", "UserDeactivatedError")):
        return DeliveryError("unreachable", "the recipient cannot be resolved or is deactivated")
    if isinstance(e, names("AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError", "UserDeactivatedBanError",
                           "AuthKeyDuplicatedError")):
        return DeliveryError("auth", f"{type(e).__name__}: the session is unusable")
    return e


class TelethonUser:
    """Sync facade over a Telethon client. Default Telethon connection parameters; nothing spoofed."""

    def __init__(self, api_id: int, api_hash: str, session: str):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        self.loop = shared_loop()

        async def make():
            return TelegramClient(StringSession(session), api_id, api_hash)
        self.client = self.loop.run(make())
        self._listening = False

    def connect(self) -> None:
        try:
            self.loop.run(self.client.connect())
            if not self.loop.run(self.client.is_user_authorized()):
                raise DeliveryError("auth", "session is not authorised")
        except DeliveryError:
            raise
        except Exception as e:  # noqa: BLE001
            raise map_error(e) from None

    def me(self) -> dict:
        u = self.loop.run(self.client.get_me())
        return {"id": u.id, "username": u.username or "", "first_name": u.first_name or ""}

    def send(self, target: str, text: str) -> tuple[str, str | None]:
        ent: Any = int(target) if re.fullmatch(r"-?\d+", target) else target
        try:
            msg = self.loop.run(self.client.send_message(ent, text))
        except Exception as e:  # noqa: BLE001
            raise map_error(e) from None
        peer = getattr(getattr(msg, "peer_id", None), "user_id", None)
        return str(msg.id), (str(peer) if peer else None)

    def listen(self, cb: Callable[[int, str, str, float], None]) -> None:
        if self._listening:
            return
        from telethon import events

        async def handler(event):
            sender = await event.get_sender()
            await asyncio.get_running_loop().run_in_executor(
                None, cb, getattr(sender, "id", 0), getattr(sender, "username", "") or "", event.raw_text or "", event.date.timestamp())
        self.client.add_event_handler(handler, events.NewMessage(incoming=True, func=lambda e: e.is_private))
        self._listening = True

    def log_out(self) -> None:
        try:
            self.loop.run(self.client.log_out())
        except Exception:  # noqa: BLE001  best effort: also revokes the session on Telegram's side
            pass

    def disconnect(self) -> None:
        try:
            self.loop.run(self.client.disconnect())
        except Exception:  # noqa: BLE001
            pass


class TelethonLogin:
    """Interactive login: send_code(phone) then sign_in(code[, 2FA password])."""

    def __init__(self, api_id: int, api_hash: str):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        self.loop = shared_loop()

        async def make():
            return TelegramClient(StringSession(), api_id, api_hash)
        self.client = self.loop.run(make())
        self.phone = self.code_hash = ""

    def send_code(self, phone: str) -> None:
        from telethon import errors as E
        try:
            self.loop.run(self.client.connect())
            r = self.loop.run(self.client.send_code_request(phone))
        except E.FloodWaitError as e:
            raise ValueError(f"Telegram asks to wait {e.seconds}s before another code request") from None
        except E.PhoneNumberInvalidError:
            raise ValueError("Telegram rejected the phone number") from None
        except E.ApiIdInvalidError:
            raise ValueError("api_id / api_hash are not valid") from None
        self.phone, self.code_hash = phone, r.phone_code_hash

    def sign_in(self, code: str, password: str | None = None) -> tuple[str, dict]:
        from telethon import errors as E
        try:
            try:
                self.loop.run(self.client.sign_in(self.phone, code, phone_code_hash=self.code_hash))
            except E.SessionPasswordNeededError:
                if not password:
                    raise ValueError("this account has two-step verification: send the password too") from None
                self.loop.run(self.client.sign_in(password=password))
        except E.PhoneCodeInvalidError:
            raise ValueError("wrong login code") from None
        except E.PhoneCodeExpiredError:
            raise ValueError("the code expired: start the login again") from None
        except E.PasswordHashInvalidError:
            raise ValueError("wrong two-step verification password") from None
        u = self.loop.run(self.client.get_me())
        session = self.client.session.save()
        self.loop.run(self.client.disconnect())
        return session, {"id": u.id, "username": u.username or "", "first_name": u.first_name or ""}

    def abort(self) -> None:
        try:
            self.loop.run(self.client.disconnect())
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- account registry
def mask_phone(phone: str) -> str:
    return phone[:4] + "•" * max(0, len(phone) - 6) + phone[-2:]


class Accounts:
    def __init__(self, app, client_factory: Callable | None = None, login_factory: Callable | None = None):
        self.app, self.db = app, app.db
        self.client_factory = client_factory or (lambda api_id, api_hash, session: TelethonUser(api_id, api_hash, session))
        self.login_factory = login_factory or (lambda api_id, api_hash: TelethonLogin(api_id, api_hash))
        self._pending: dict[tuple[str, str], tuple[Any, float]] = {}
        self._clients: dict[tuple[str, str], Any] = {}
        self._lock = threading.RLock()

    # ---- login (code entered once)
    def login_start(self, pid: str, label: str, phone: str, api_id: int, api_hash: str) -> dict:
        if not LABEL_RE.match(label):
            raise ValueError("label: 2-31 chars, lowercase letters, digits, - or _")
        phone = phone.replace(" ", "")
        if not PHONE_RE.match(phone):
            raise ValueError("phone must look like +380XXXXXXXXX")
        if not API_HASH_RE.match(api_hash or ""):
            raise ValueError("api_hash must be the 32-character hex string from my.telegram.org")
        fernet = self.app.s.session_fernet()  # fail early, before Telegram sends a code
        row = self.db.one("SELECT status FROM tg_accounts WHERE project_id=? AND label=?", (pid, label))
        if row and row["status"] == "active":
            raise ValueError(f"account {label!r} is already connected; remove it first")
        login = self.login_factory(int(api_id), api_hash)
        login.send_code(phone)
        with self._lock:
            self._pending[(pid, label)] = (login, time.time())
        self.db.x("INSERT OR REPLACE INTO tg_accounts(project_id,label,phone_hint,api_id,api_hash_enc,status,created_at) "
                  "VALUES(?,?,?,?,?,'pending',?)", (pid, label, mask_phone(phone), int(api_id),
                                                    fernet.encrypt(api_hash.encode()).decode(), time.time()))
        return {"status": "code_sent", "phone": mask_phone(phone)}

    def login_code(self, pid: str, label: str, code: str, password: str | None = None, daily_cap: int = 10) -> dict:
        with self._lock:
            item = self._pending.get((pid, label))
        if not item or time.time() - item[1] > LOGIN_TTL:
            raise ValueError("no login in progress (or it expired): start again")
        login = item[0]
        session, me = login.sign_in(code.strip(), password)  # ValueError on wrong code / missing 2FA keeps the login open
        with self._lock:
            self._pending.pop((pid, label), None)
        fernet = self.app.s.session_fernet()
        import json
        self.db.x("UPDATE tg_accounts SET session_enc=?, status='active', status_reason='', me=? WHERE project_id=? AND label=?",
                  (fernet.encrypt(session.encode()).decode(), json.dumps(me), pid, label))
        if not self.db.one("SELECT 1 FROM senders WHERE project_id=? AND label=?", (pid, label)):
            self.app.outreach.add_sender(pid, label, "telegram_user", {"account": label}, daily_cap)
        return {"status": "connected", "me": me, "sender": label, "daily_cap": daily_cap}

    # ---- registry
    def list(self, pid: str) -> list[dict]:
        import json
        rows = self.db.q("SELECT label, phone_hint, status, status_reason, me, first_send_at, created_at FROM tg_accounts "
                         "WHERE project_id=? ORDER BY label", (pid,))
        for r in rows:
            r["me"] = json.loads(r["me"] or "{}")
        return rows

    def remove(self, pid: str, label: str) -> None:
        if not self.db.one("SELECT 1 FROM tg_accounts WHERE project_id=? AND label=?", (pid, label)):
            raise ValueError(f"account {label!r} not found")
        with self._lock:
            c = self._clients.pop((pid, label), None)
            p = self._pending.pop((pid, label), None)
        if c:
            c.log_out()  # revokes the session at Telegram, then forget it
        if p and hasattr(p[0], "abort"):
            p[0].abort()
        self.db.x("DELETE FROM tg_accounts WHERE project_id=? AND label=?", (pid, label))
        self.db.x("DELETE FROM senders WHERE project_id=? AND label=? AND channel='telegram_user'", (pid, label))

    def mark_auth_error(self, pid: str, label: str, reason: str) -> None:
        self.db.x("UPDATE tg_accounts SET status='auth_error', status_reason=? WHERE project_id=? AND label=?", (reason[:300], pid, label))
        with self._lock:
            self._clients.pop((pid, label), None)

    # ---- live clients
    def client(self, pid: str, label: str):
        with self._lock:
            if (pid, label) in self._clients:
                return self._clients[(pid, label)]
            row = self.db.one("SELECT * FROM tg_accounts WHERE project_id=? AND label=?", (pid, label))
            if not row or row["status"] != "active":
                raise DeliveryError("auth", f"account {label!r} is not connected (status: {row['status'] if row else 'missing'})")
            f = self.app.s.session_fernet()
            c = self.client_factory(row["api_id"], f.decrypt(row["api_hash_enc"].encode()).decode(),
                                    f.decrypt(row["session_enc"].encode()).decode())
            c.connect()
            c.listen(lambda uid, uname, text, ts, _p=pid, _l=label: self._on_dm(_p, _l, uid, uname, text, ts))
            self._clients[(pid, label)] = c
            return c

    def send(self, pid: str, label: str, target: str, text: str, now: float | None = None) -> tuple[str, str | None]:
        try:
            out = self.client(pid, label).send(target, text)
        except DeliveryError as e:
            if e.kind == "auth":
                self.mark_auth_error(pid, label, e.args[0][:200])
            raise
        if not self.db.one("SELECT first_send_at f FROM tg_accounts WHERE project_id=? AND label=?", (pid, label))["f"]:
            self.db.x("UPDATE tg_accounts SET first_send_at=? WHERE project_id=? AND label=?", (now or time.time(), pid, label))
        return out

    def start_all(self) -> None:
        for r in self.db.q("SELECT project_id, label FROM tg_accounts WHERE status='active'"):
            try:
                self.client(r["project_id"], r["label"])
            except Exception as e:  # noqa: BLE001
                log.warning("account %s/%s not started: %s", r["project_id"], r["label"], e)

    def _on_dm(self, pid: str, label: str, user_id: int, username: str, text: str, ts: float) -> None:
        try:
            self.app.outreach.handle_telegram_dm(pid, user_id, username, text)
        except Exception:  # noqa: BLE001
            log.exception("handling a reply for %s/%s failed", pid, label)

    def shutdown(self) -> None:
        with self._lock:
            for c in self._clients.values():
                try:
                    c.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self._clients.clear()
