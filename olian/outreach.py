"""Outreach engine: lead funnel, drafts with human approval, sender pool with hard limits, reply handling.

Safety rails are structural, not advisory: the agent only DRAFTS; this module decides whether, when and from
which account a message goes out (approval, working-hours window, per-sender daily caps, global pacing,
opt-out, lead freshness).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .channels import ChannelError, WhatsAppCloud
from .hermes import HermesError
from .util import digits, extract_json

DEFAULT_POLICY = {
    "require_approval": True,
    "window": {"start": 9, "end": 20, "utc_offset": 0},
    "min_interval_seconds": 45,
    "max_lead_age_minutes": 0,  # speed-to-lead: a 'new' lead older than this is stale (0 = off)
    "stop_words": ["stop", "unsubscribe", "отписаться", "відписатися", "не пишіть", "не пишите", "стоп", "odhlásit"],
    # --- built-in safeguards (docs/TELEGRAM-LIMITS.md): stop early, before a platform restricts anything
    "max_per_lead": 3,            # at most this many messages to one person unless they reply
    "min_gap_hours": 72,          # minimum gap between messages to one person unless they reply
    "daily_cap_total": 50,        # all senders together, per project per day
    "footer": "Щоб відмовитись від повідомлень, відповідайте STOP.",  # appended to the FIRST message to a person
    "complaint_words": ["spam", "спам", "report", "скарг", "жалоб", "заблок", "blocked", "nahlásit"],
    "breaker": {"consecutive_failures": 3, "complaints": 3, "optout_rate": 0.25, "min_sent_for_rate": 10, "window_days": 7},
}
LIMIT_MARKERS = ("peer_flood", "flood", "too many", "spam", "restricted", "banned", "blocked", "rate limit", "429",
                 "131048", "131056", "130429", "368")
POSITIVE = ("yes", "interested", "tell me more", "call me", "price", "how much", "да", "так", "цікаво", "интересно",
            "сколько", "скільки", "давайте", "ок")
NEGATIVE = ("no thanks", "not interested", "не цікаво", "не интересно", "нет", "ні")

DRAFT_PROMPT = """Write ONE short outreach message ({kind}) for project «{name}».
Goal of the project: {goal}
Lead: {lead}
Extra brief: {brief}
Rules: same language the lead uses (or the project's default), no fake claims, no pressure, one clear question,
max 60 words, no emojis spam. Reply with the message text only."""

CLASSIFY_PROMPT = """A lead replied to our outreach for project «{name}».
Reply text: {text}
Classify. Reply with JSON only:
{{"intent": "positive|negative|question|other", "summary": "<one line>", "reply": "<suggested short answer or empty>"}}"""


def _now() -> float:
    return time.time()


class Outreach:
    def __init__(self, app, http: Any = httpx):
        self.app, self.db, self.http = app, app.db, http

    # ---- policy
    def policy(self, pid: str) -> dict:
        cfg = self.app.projects.get(pid)["cfg"].get("outreach", {})
        pol = {**DEFAULT_POLICY, **cfg}
        pol["window"] = {**DEFAULT_POLICY["window"], **cfg.get("window", {})}
        pol["breaker"] = {**DEFAULT_POLICY["breaker"], **cfg.get("breaker", {})}
        return pol

    # ---- leads
    def add_lead(self, pid: str, channel: str, address: str, name: str = "", source: str = "manual",
                 data: dict | None = None, warmth: int = 0, next_touch: float = 0, detected_at: float = 0) -> dict:
        address = digits(address) if channel == "whatsapp" else address.strip()
        if not address:
            raise ValueError("empty address")
        self.db.x("INSERT OR IGNORE INTO leads(project_id,channel,address,name,source,data,warmth,next_touch,detected_at,"
                  "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (pid, channel, address, name, source, json.dumps(data or {}, ensure_ascii=False), warmth,
                   next_touch, detected_at or _now(), _now()))
        lead = self.lead_by_addr(pid, channel, address)
        if lead["status"] == "new" and self.stoplisted(pid, address, lead["id"]):
            self.db.x("UPDATE leads SET status='opted_out' WHERE id=?", (lead["id"],))
            lead = self.lead_by_addr(pid, channel, address)
        return lead

    def lead_by_addr(self, pid: str, channel: str, address: str) -> dict | None:
        address = digits(address) if channel == "whatsapp" else address.strip()
        return self.db.one("SELECT * FROM leads WHERE project_id=? AND channel=? AND address=?", (pid, channel, address))

    def lead(self, pid: str, lid: int) -> dict:
        row = self.db.one("SELECT * FROM leads WHERE id=? AND project_id=?", (lid, pid))
        if not row:
            raise ValueError(f"lead {lid} not found in project {pid}")
        return row

    def leads(self, pid: str, status: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM leads WHERE project_id=?", [pid]
        if status:
            sql += " AND status=?"
            args.append(status)
        return self.db.q(sql + " ORDER BY id DESC LIMIT ?", args + [limit])

    def set_lead_status(self, pid: str, lid: int, status: str) -> dict:
        self.lead(pid, lid)
        self.db.x("UPDATE leads SET status=? WHERE id=? AND project_id=?", (status, lid, pid))
        return self.lead(pid, lid)

    def funnel(self, pid: str) -> dict:
        rows = self.db.q("SELECT status, COUNT(*) n FROM leads WHERE project_id=? GROUP BY status", (pid,))
        sent = self.db.one("SELECT COUNT(*) n FROM messages WHERE project_id=? AND direction='out' AND status='sent'", (pid,))
        pend = self.db.one("SELECT COUNT(*) n FROM messages WHERE project_id=? AND status IN ('draft','approved')", (pid,))
        return {"leads": {r["status"]: r["n"] for r in rows}, "messages_sent": sent["n"], "messages_pending": pend["n"]}

    # ---- senders
    def add_sender(self, pid: str, label: str, channel: str, config: dict | None = None, daily_cap: int = 20) -> None:
        if channel not in ("whatsapp_cloud", "webhook", "dryrun"):
            raise ValueError("sender channel: whatsapp_cloud | webhook | dryrun")
        self.db.x("INSERT OR REPLACE INTO senders(project_id,label,channel,config,daily_cap) VALUES(?,?,?,?,?)",
                  (pid, label, channel, json.dumps(config or {}), daily_cap))

    def senders(self, pid: str) -> list[dict]:
        rows = self.db.q("SELECT * FROM senders WHERE project_id=?", (pid,))
        for r in rows:
            r["config"] = json.loads(r["config"] or "{}")
        return rows

    def senders_public(self, pid: str) -> list[dict]:
        out = []
        for r in self.senders(pid):
            r["config"] = {k: ("***" if any(w in k.lower() for w in ("token", "secret", "key", "authorization")) else v)
                           for k, v in r["config"].items() if k != "headers"}
            out.append(r)
        return out

    def reset_sender(self, pid: str, label: str) -> dict:
        if not self.db.one("SELECT 1 FROM senders WHERE project_id=? AND label=?", (pid, label)):
            raise ValueError(f"sender {label!r} not found")
        self.db.x("UPDATE senders SET status='active', status_reason='', fail_streak=0 WHERE project_id=? AND label=?", (pid, label))
        return next(x for x in self.senders_public(pid) if x["label"] == label)

    def trip(self, pid: str, reason: str, label: str | None = None) -> None:
        """Circuit breaker (TZ CON-6): stop sending. Only an admin may re-enable; no failover to another account."""
        sql = "UPDATE senders SET status='tripped', status_reason=? WHERE project_id=? AND status='active'"
        args: list = [reason[:300], pid]
        if label:
            sql += " AND label=?"
            args.append(label)
        self.db.x(sql, args)
        self.db.add_event(pid, "outreach", "breaker", {"reason": reason[:300], "sender": label or "all"})
        self.app.notifier.send(pid, f"⛔ Аутріч зупинено ({label or 'усі відправники'}): {reason[:200]}\n"
                                    "Причину треба з'ясувати; увімкнути може лише admin (outreach.reset_sender).", min_role="admin")

    def check_health(self, pid: str, now: float | None = None) -> str | None:
        """Trip all senders when complaints or opt-outs pile up (complaints are what actually get accounts restricted)."""
        now, br = now or _now(), self.policy(pid)["breaker"]
        since = now - br["window_days"] * 86400
        complaints = self.db.one("SELECT COUNT(*) n FROM events WHERE project_id=? AND kind='complaint' AND created_at>=?", (pid, since))["n"]
        sent = self.db.one("SELECT COUNT(*) n FROM messages WHERE project_id=? AND direction='out' AND status='sent' AND sent_at>=?", (pid, since))["n"]
        optouts = self.db.one("SELECT COUNT(*) n FROM events WHERE project_id=? AND kind='optout' AND created_at>=?", (pid, since))["n"]
        reason = None
        if complaints >= br["complaints"]:
            reason = f"{complaints} complaints in {br['window_days']} days"
        elif sent >= br["min_sent_for_rate"] and optouts / sent >= br["optout_rate"]:
            reason = f"opt-out rate {optouts}/{sent} in {br['window_days']} days"
        if reason and any(x["status"] == "active" for x in self.senders(pid)):
            self.trip(pid, reason)
        return reason

    @staticmethod
    def _serves(sender: dict, channel: str) -> bool:
        if sender["channel"] == "whatsapp_cloud":
            return channel == "whatsapp"
        if sender["channel"] == "webhook":
            return channel in sender["config"].get("serves", ["telegram", "whatsapp", "email"])
        return True  # dryrun

    # ---- contact guard (TZ CON-1/CON-2): authors of monitored posts are never contacted without a recorded basis
    COLD_SOURCES = ("signal:", "monitor:")

    def stoplisted(self, pid: str, address: str, exclude_id: int = 0) -> bool:
        return bool(self.db.one("SELECT 1 FROM leads WHERE project_id=? AND address=? AND status='opted_out' AND id!=?",
                                (pid, address, exclude_id)))

    def stop(self, pid: str, channel: str, address: str) -> dict:
        """Put a person on the project stop-list (they asked not to be contacted). Final; applies to every channel."""
        lead = self.add_lead(pid, channel, address, source="stoplist")
        self.db.x("UPDATE leads SET status='opted_out' WHERE id=?", (lead["id"],))
        self.db.x("UPDATE messages SET status='cancelled', error='stop-list' WHERE lead_id=? AND status IN ('draft','approved')",
                  (lead["id"],))
        return self.lead(pid, lead["id"])

    def contact_block(self, lead: dict) -> str | None:
        if lead.get("status") == "opted_out" or self.stoplisted(lead["project_id"], lead["address"], lead["id"]):
            return "the person is on the stop-list"
        if lead.get("consent"):
            return None
        if str(lead.get("source", "")).startswith(self.COLD_SOURCES):
            return ("contact with authors of monitored posts is disabled until a legal basis is recorded "
                    "(leads.set_consent); automatic cold private messages are not supported")
        return None

    def set_consent(self, pid: str, lid: int, basis: str) -> dict:
        basis = (basis or "").strip()
        if len(basis) < 5:
            raise ValueError("describe the basis for contact (e.g. 'person asked to be contacted in the group on 2026-10-26')")
        lead = self.lead(pid, lid)
        data = json.loads(lead["data"] or "{}")
        data["consent_basis"], data["consent_at"] = basis[:500], _now()
        self.db.x("UPDATE leads SET consent=1, data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), lid))
        return self.lead(pid, lid)

    # ---- messages
    def _insert_msg(self, pid: str, lead_id: int, direction: str, body: str, status: str) -> int:
        return self.db.x("INSERT INTO messages(project_id,lead_id,direction,body,status,created_at) VALUES(?,?,?,?,?,?)",
                         (pid, lead_id, direction, body.strip(), status, _now()))

    def message(self, pid: str, mid: int) -> dict:
        row = self.db.one("SELECT * FROM messages WHERE id=? AND project_id=?", (mid, pid))
        if not row:
            raise ValueError(f"message {mid} not found in project {pid}")
        return row

    def outbox(self, pid: str, status: str = "draft") -> list[dict]:
        return self.db.q("SELECT m.*, l.name lead_name, l.address, l.channel FROM messages m JOIN leads l ON l.id=m.lead_id "
                         "WHERE m.project_id=? AND m.status=? AND m.direction='out' ORDER BY m.id", (pid, status))

    def compose(self, pid: str, lead_id: int, body: str, approve: bool = False) -> dict:
        lead = self.lead(pid, lead_id)
        if lead["status"] == "opted_out":
            raise ValueError("lead opted out; will not message")
        if (why := self.contact_block(lead)):
            raise ValueError(why)
        mid = self._insert_msg(pid, lead_id, "out", body, "draft")
        if approve or not self.policy(pid)["require_approval"]:
            self.db.x("UPDATE messages SET status='approved' WHERE id=?", (mid,))
        return self.message(pid, mid)

    def draft(self, pid: str, lead_id: int, brief: str = "", kind: str = "first") -> dict:
        project, lead = self.app.projects.get(pid), self.lead(pid, lead_id)
        text = self.app.agent_chat(pid, DRAFT_PROMPT.format(
            kind=kind, name=project["name"], goal=project["goal"] or "-", brief=brief or "-",
            lead=json.dumps({"name": lead["name"], "channel": lead["channel"], **json.loads(lead["data"] or "{}")},
                            ensure_ascii=False)), session=f"draft-{lead_id}")
        msg = self.compose(pid, lead_id, text)
        if msg["status"] == "draft":
            self.app.notifier.send(pid, f"✍️ Чернетка #{msg['id']} для {lead['name'] or lead['address']}:\n\n{msg['body']}"
                                        f"\n\n/approve {msg['id']}  або  /reject {msg['id']}")
        return msg

    def approve(self, pid: str, mid: int) -> dict:
        m = self.message(pid, mid)
        if m["status"] != "draft":
            raise ValueError(f"message {mid} is {m['status']}, not a draft")
        self.db.x("UPDATE messages SET status='approved' WHERE id=?", (mid,))
        return self.message(pid, mid)

    def reject(self, pid: str, mid: int) -> dict:
        m = self.message(pid, mid)
        if m["status"] not in ("draft", "approved"):
            raise ValueError(f"message {mid} is {m['status']}")
        self.db.x("UPDATE messages SET status='rejected' WHERE id=?", (mid,))
        return self.message(pid, mid)

    # ---- sending (the only place where messages leave the building)
    def _local_hour(self, pol: dict, now: float) -> int:
        return (datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=pol["window"]["utc_offset"])).hour

    def send_due(self, pid: str, now: float | None = None, max_messages: int = 20) -> dict:
        now = now or _now()
        pol = self.policy(pid)
        res = {"sent": 0, "failed": 0, "cancelled": 0, "skipped": ""}
        if not (pol["window"]["start"] <= self._local_hour(pol, now) < pol["window"]["end"]):
            res["skipped"] = "outside sending window"
            return res
        if self.check_health(pid, now):
            res["skipped"] = "circuit breaker tripped"
            return res
        today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        day_start = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        for m in self.db.q("SELECT * FROM messages WHERE project_id=? AND direction='out' AND status='approved' "
                           "ORDER BY id LIMIT ?", (pid, max_messages)):
            lead = self.lead(pid, m["lead_id"])
            if lead["status"] in ("opted_out", "lost"):
                self._finish(m["id"], "cancelled", f"lead is {lead['status']}")
                res["cancelled"] += 1
                continue
            if (why := self.contact_block(lead)):
                self._finish(m["id"], "cancelled", why[:200])
                res["cancelled"] += 1
                continue
            age = pol["max_lead_age_minutes"]
            if age and lead["source"] != "warm" and lead["status"] == "new" and now - (lead["detected_at"] or now) > age * 60:
                self.set_lead_status(pid, lead["id"], "stale")
                self._finish(m["id"], "cancelled", "lead went stale (speed-to-lead window passed)")
                res["cancelled"] += 1
                continue
            # per-person limits: a conversation the person started is exempt, unsolicited follow-ups are not
            hist = self.db.one("SELECT COUNT(*) n, MAX(sent_at) t FROM messages WHERE lead_id=? AND direction='out' AND status='sent'", (lead["id"],))
            reply = self.db.one("SELECT MAX(created_at) t FROM messages WHERE lead_id=? AND direction='in'", (lead["id"],))
            replied = bool(reply and reply["t"] and reply["t"] > (hist["t"] or 0))
            if not replied and hist["n"] >= pol["max_per_lead"]:
                self._finish(m["id"], "cancelled", f"per-person limit reached ({pol['max_per_lead']} messages without a reply)")
                res["cancelled"] += 1
                continue
            if not replied and hist["t"] and now - hist["t"] < pol["min_gap_hours"] * 3600:
                res["skipped"] = "min gap between messages to one person"
                continue
            total = self.db.one("SELECT COUNT(*) n FROM messages WHERE project_id=? AND direction='out' AND status='sent' AND sent_at>=?", (pid, day_start))["n"]
            if total >= pol["daily_cap_total"]:
                res["skipped"] = "project daily cap reached"
                break
            last = self.db.one("SELECT MAX(sent_at) t FROM messages WHERE project_id=? AND status='sent'", (pid,))
            if last and last["t"] and now - last["t"] < pol["min_interval_seconds"]:
                res["skipped"] = "pacing"
                break
            sender = self._pick_sender(pid, lead["channel"], today)
            if not sender:
                res["skipped"] = "no sender with capacity for " + lead["channel"]
                continue
            text = m["body"] if hist["n"] else (m["body"] + ("\n\n" + pol["footer"] if pol["footer"] else ""))  # opt-out notice on first contact
            try:
                ext = self._deliver(sender, lead, text, now)
            except (ChannelError, httpx.HTTPError, ValueError) as e:
                self._finish(m["id"], "failed", str(e)[:300], sender["label"])
                res["failed"] += 1
                streak = sender["fail_streak"] + 1
                self.db.x("UPDATE senders SET fail_streak=? WHERE project_id=? AND label=?", (streak, pid, sender["label"]))
                if any(k in str(e).lower() for k in LIMIT_MARKERS):
                    self.trip(pid, f"platform limit/restriction signal: {str(e)[:150]}", sender["label"])
                    res["skipped"] = "circuit breaker tripped"
                    break
                if streak >= pol["breaker"]["consecutive_failures"]:
                    self.trip(pid, f"{streak} consecutive send failures", sender["label"])
                    res["skipped"] = "circuit breaker tripped"
                    break
                continue
            self.db.x("UPDATE messages SET status='sent', sent_at=?, sender=?, error=? WHERE id=?",
                      (now, sender["label"], ext or "", m["id"]))
            self.db.x("UPDATE senders SET sent_day=?, sent_today=?, fail_streak=0 WHERE project_id=? AND label=?",
                      (today, (sender["sent_today"] if sender["sent_day"] == today else 0) + 1, pid, sender["label"]))
            nxt = lead["next_touch"]
            if lead["source"] == "warm":
                nxt = now + (14 if lead["warmth"] >= 70 else 30 if lead["warmth"] >= 40 else 60) * 86400
            self.db.x("UPDATE leads SET last_contact=?, next_touch=?, status=CASE WHEN status IN ('new','stale') THEN "
                      "'contacted' ELSE status END WHERE id=?", (now, nxt, lead["id"]))
            res["sent"] += 1
        return res

    def _finish(self, mid: int, status: str, error: str, sender: str = "") -> None:
        self.db.x("UPDATE messages SET status=?, error=?, sender=? WHERE id=?", (status, error, sender, mid))

    def _pick_sender(self, pid: str, channel: str, today: str) -> dict | None:
        best = None
        pool = sorted(self.senders(pid), key=lambda x: x["label"])
        if channel == "telegram":  # one account per Telegram channel: no rotation/failover to route around a limit
            pool = [x for x in pool if self._serves(x, channel)][:1]
        for s in pool:
            used = s["sent_today"] if s["sent_day"] == today else 0
            if s["status"] != "active" or used >= s["daily_cap"] or not self._serves(s, channel):
                continue
            s["sent_today"], s["sent_day"] = used, today
            if best is None or used < best["sent_today"]:
                best = s
        return best

    def _deliver(self, sender: dict, lead: dict, text: str, now: float) -> str:
        cfg = sender["config"]
        if sender["channel"] == "dryrun":
            return "dryrun"
        if sender["channel"] == "webhook":
            r = self.http.post(cfg["url"], headers=cfg.get("headers", {}), timeout=30,
                               json={"channel": lead["channel"], "to": lead["address"], "text": text,
                                     "sender": sender["label"], "lead_id": lead["id"]})
            if r.status_code >= 300:
                raise ChannelError(f"webhook sender {r.status_code}: {r.text[:200]}")
            return "webhook"
        token = cfg.get("token") or self.app.s.wa_token
        wa = WhatsAppCloud(cfg["phone_number_id"], token, self.http)
        inbound = self.db.one("SELECT MAX(created_at) t FROM messages WHERE lead_id=? AND direction='in'", (lead["id"],))
        if inbound and inbound["t"] and now - inbound["t"] < 24 * 3600:  # inside Meta's customer-service window
            return wa.send_text(lead["address"], text)
        if cfg.get("template_name"):
            return wa.send_template(lead["address"], cfg["template_name"], cfg.get("template_lang", "en"), [text])
        raise ChannelError("outside WhatsApp 24h window and no approved template configured on this sender")

    # ---- inbound
    def handle_reply(self, pid: str, channel: str, address: str, text: str, name: str = "") -> dict:
        lead = self.lead_by_addr(pid, channel, address)
        if not lead:  # someone we never contacted wrote in: still a lead (inbound)
            lead = self.add_lead(pid, channel, address, name=name, source="inbound")
        self._insert_msg(pid, lead["id"], "in", text, "received")
        low = text.lower()
        pol = self.policy(pid)
        if any(w in low for w in pol["complaint_words"]):
            self.db.add_event(pid, "outreach", "complaint", {"lead_id": lead["id"]})
        if any(w in low for w in pol["stop_words"]):
            self.db.add_event(pid, "outreach", "optout", {"lead_id": lead["id"]})
            self.db.x("UPDATE leads SET status='opted_out' WHERE id=?", (lead["id"],))
            self.db.x("UPDATE messages SET status='cancelled', error='opt-out' WHERE lead_id=? AND status IN ('draft','approved')",
                      (lead["id"],))
            self.check_health(pid)
            return {"lead_id": lead["id"], "intent": "opt_out"}
        verdict = self._classify(pid, text)
        intent = verdict["intent"]
        if intent == "negative":
            self.db.x("UPDATE leads SET status='lost' WHERE id=?", (lead["id"],))
        else:
            self.db.x("UPDATE leads SET status=CASE WHEN ?='positive' THEN 'qualified' ELSE 'replied' END, last_contact=? "
                      "WHERE id=?", (intent, _now(), lead["id"]))
        who = lead["name"] or lead["address"]
        if intent == "positive":
            self.app.tasks.add(pid, f"Передати кваліфікованого ліда: {who}", detail=verdict["summary"], executor="human",
                               steps=[{"text": f"Відповісти {who} ({channel}: {lead['address']})",
                                       "copy": verdict.get("reply", "")}])
        elif intent in ("question", "other") and verdict.get("reply"):
            msg = self.compose(pid, lead["id"], verdict["reply"])
            if msg["status"] == "draft":
                self.app.notifier.send(pid, f"💬 {who}: «{text[:200]}»\nЧернетка відповіді #{msg['id']}:\n{msg['body']}"
                                            f"\n\n/approve {msg['id']}  або  /reject {msg['id']}")
        return {"lead_id": lead["id"], **verdict}

    def _classify(self, pid: str, text: str) -> dict:
        project = self.app.projects.get(pid)
        try:
            v = extract_json(self.app.agent_chat(pid, CLASSIFY_PROMPT.format(name=project["name"], text=text[:1500]),
                                                 session="classify"))
            if v.get("intent") in ("positive", "negative", "question", "other"):
                return {"intent": v["intent"], "summary": str(v.get("summary", ""))[:300], "reply": str(v.get("reply", ""))}
        except (HermesError, ValueError, AttributeError, TypeError):
            pass
        low = text.lower()  # agent down or unparsable: conservative keyword fallback, never auto-replies
        intent = "negative" if any(w in low for w in NEGATIVE) else "positive" if any(w in low for w in POSITIVE) else "other"
        return {"intent": intent, "summary": text[:200], "reply": ""}

    # ---- warm contacts / networking
    def touch_plan(self, pid: str, now: float | None = None, limit: int = 5) -> list[dict]:
        """Draft check-ins for warm contacts whose next_touch is due. Human approves before anything is sent."""
        now = now or _now()
        out = []
        for lead in self.db.q("SELECT * FROM leads WHERE project_id=? AND source='warm' AND next_touch>0 AND next_touch<=? "
                              "AND status NOT IN ('opted_out','lost') ORDER BY next_touch LIMIT ?", (pid, now, limit)):
            if self.db.one("SELECT 1 FROM messages WHERE lead_id=? AND status IN ('draft','approved')", (lead["id"],)):
                continue
            days = int((now - lead["last_contact"]) / 86400) if lead["last_contact"] else None
            brief = (f"Warm contact, last contact {days} days ago. " if days is not None else "Warm contact. ") + \
                    "Friendly check-in, mention shared context from the lead data, no selling."
            out.append(self.draft(pid, lead["id"], brief=brief, kind="check-in"))
        return out

    def sweep_stale(self, pid: str, now: float | None = None) -> int:
        age = self.policy(pid)["max_lead_age_minutes"]
        if not age:
            return 0
        now = now or _now()
        n = 0
        for lead in self.db.q("SELECT id,detected_at FROM leads WHERE project_id=? AND status='new' AND source!='warm'", (pid,)):
            if now - lead["detected_at"] > age * 60:
                self.set_lead_status(pid, lead["id"], "stale")
                n += 1
        return n
