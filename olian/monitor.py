"""Telegram monitoring + lead qualification (see docs/TZ-telegram-monitoring.md).

Flow:  collector -> normalise -> level-1 rules -> dedup -> queue (pending) -> level-2 qualifier -> lead / rejected.
Leads are for HUMAN review. Nothing here sends a message to anybody, and nothing here routes around a Telegram limit:
auth/access errors stop the affected source, rate limits are waited out.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import time
from typing import Any

import httpx

from .channels import Telegram, TelegramError
from .qualify import (QualifyError, Qualifier, find_phrases, hamming, jaccard, norm_hash, normalize, simhash)

LEAD_STATUSES = ("new", "review", "relevant", "irrelevant", "processed", "excluded")
SOURCE_STATUSES = ("active", "paused", "unavailable", "auth_error")
REVIEWED = ("relevant", "irrelevant", "processed", "excluded")
DEFAULTS: dict[str, Any] = {
    "enabled": False, "mode": "rules", "profile": "", "threshold": 0.5, "review_below": 0.65,
    "retention_days": 14, "lead_retention_days": 180, "author": "hash",
    "near_distance": 16, "near_jaccard": 0.8, "window_days": 30, "author_window_days": 14, "reopen_processed": False,
    "push_enabled": False, "max_attempts": 3, "notify": True, "notify_max_per_run": 5,
    "check_interval_seconds": 1800, "contact": {"allowed_ways": []},
    # Needed only if you want to write to authors privately: keep their @username (and raw id with author="raw").
    "store_username": False,
    "autopilot": {"enabled": False, "min_confidence": 0.7, "brief": ""},
}
MAX_TEXT = 4000
EXPORT_COLUMNS = ["id", "created_at", "status", "category", "service", "relevance_score", "confidence", "source",
                  "message_date", "message_url", "reason", "hit_count", "operator_note", "outcome", "message_text"]


def backoff(fail_count: int, base: int = 30, cap: int = 3600) -> int:
    return min(cap, base * (2 ** max(0, fail_count)))


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""


def csv_safe(v: Any) -> str:
    """Neutralise spreadsheet formula injection (a hostile post could start with = + - @)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


class Monitor:
    def __init__(self, app, http: Any = httpx):
        self.app, self.db, self.http = app, app.db, http

    # ------------------------------------------------------------------ config & state
    def cfg(self, pid: str) -> dict:
        raw = self.app.projects.get(pid)["cfg"].get("monitor", {})
        return {**DEFAULTS, **raw, "contact": {**DEFAULTS["contact"], **raw.get("contact", {})}}

    def bot_token(self, pid: str) -> str:
        raw = self.app.projects.get(pid)["cfg"].get("monitor", {})
        env = raw.get("bot_token_env")
        return (os.environ.get(env, "") if env else "") or raw.get("bot_token", "")

    def _state(self, pid: str, key: str, default: str = "") -> str:
        r = self.db.one("SELECT value FROM tg_state WHERE project_id=? AND key=?", (pid, key))
        return r["value"] if r else default

    def _set_state(self, pid: str, key: str, value: Any) -> None:
        self.db.x("INSERT OR REPLACE INTO tg_state(project_id,key,value) VALUES(?,?,?)", (pid, key, str(value)))

    def author_ref(self, pid: str, author_id: Any) -> str | None:
        mode = self.cfg(pid)["author"]
        if author_id in (None, "") or mode == "none":
            return None
        if mode == "raw":
            return str(author_id)
        return hmac.new(self.app.s.pii_salt(), f"{pid}:{author_id}".encode(), hashlib.sha256).hexdigest()[:32]

    # ------------------------------------------------------------------ sources
    def add_source(self, pid: str, chat_ref: str, title: str = "", username: str = "", kind: str = "",
                   ingest: str = "bot") -> dict:
        chat_ref = str(chat_ref).strip()
        if not chat_ref or len(chat_ref) > 64:
            raise ValueError("chat_ref (Telegram chat id) is required")
        if ingest not in ("bot", "push"):
            raise ValueError("ingest must be bot|push")
        if ingest == "push" and not self.cfg(pid)["push_enabled"]:
            raise ValueError("push ingestion is disabled: it needs a documented decision (monitor.push_enabled), see TZ §10")
        if self.db.one("SELECT 1 FROM tg_sources WHERE project_id=? AND chat_ref=?", (pid, chat_ref)):
            raise ValueError(f"source {chat_ref} already exists")
        sid = self.db.x("INSERT INTO tg_sources(project_id,chat_ref,kind,title,username,ingest,created_at) VALUES(?,?,?,?,?,?,?)",
                        (pid, chat_ref, kind, title[:200], username.lstrip("@")[:64], ingest, time.time()))
        return self.source(pid, sid)

    def source(self, pid: str, sid: int) -> dict:
        r = self.db.one("SELECT * FROM tg_sources WHERE id=? AND project_id=?", (sid, pid))
        if not r:
            raise ValueError(f"source {sid} not found in project {pid}")
        return r

    def sources(self, pid: str) -> list[dict]:
        return self.db.q("SELECT * FROM tg_sources WHERE project_id=? ORDER BY id", (pid,))

    def _source_by_ref(self, pid: str, ref: str) -> dict | None:
        return self.db.one("SELECT * FROM tg_sources WHERE project_id=? AND chat_ref=?", (pid, str(ref)))

    def update_source(self, pid: str, sid: int, status: str | None = None, title: str | None = None) -> dict:
        self.source(pid, sid)
        if status is not None:
            if status not in ("active", "paused"):
                raise ValueError("operators may set only active|paused (unavailable/auth_error are set by the system)")
            self.db.x("UPDATE tg_sources SET status=?, status_reason='', fail_count=0, next_attempt_at=0 WHERE id=?", (status, sid))
        if title is not None:
            self.db.x("UPDATE tg_sources SET title=? WHERE id=?", (title[:200], sid))
        return self.source(pid, sid)

    def remove_source(self, pid: str, sid: int) -> None:
        self.source(pid, sid)
        self.db.x("DELETE FROM tg_sources WHERE id=?", (sid,))

    def set_source_status(self, pid: str, sid: int, status: str, reason: str = "") -> dict:
        """System / external-client path: records unavailable or auth_error with a reason."""
        if status not in SOURCE_STATUSES:
            raise ValueError("bad source status")
        self.source(pid, sid)
        self.db.x("UPDATE tg_sources SET status=?, status_reason=?, error_count=error_count+? WHERE id=?",
                  (status, reason[:300], 1 if status in ("unavailable", "auth_error") else 0, sid))
        return self.source(pid, sid)

    def candidates(self, pid: str) -> list[dict]:
        try:
            return list(json.loads(self._state(pid, "candidates", "{}")).values())
        except ValueError:
            return []

    # ------------------------------------------------------------------ rules (level 1)
    @staticmethod
    def _clean_list(v: Any, label: str) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            raise ValueError(f"{label} must be a list of strings")
        out = [x.strip() for x in v if x.strip()]
        if len(out) > 500 or any(len(x) > 100 for x in out):
            raise ValueError(f"{label}: at most 500 items of up to 100 characters")
        return out

    def save_rule(self, pid: str, rule: dict) -> dict:
        name = str(rule.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValueError("rule name is required (max 80 chars)")
        phrases, all_of = self._clean_list(rule.get("phrases"), "phrases"), self._clean_list(rule.get("all_of"), "all_of")
        exclude, require_any = self._clean_list(rule.get("exclude"), "exclude"), self._clean_list(rule.get("require_any"), "require_any")
        if not phrases and not all_of:
            raise ValueError("a rule needs phrases or all_of")
        if len(require_any) > 500:
            raise ValueError("require_any: at most 500 items")
        min_hits = int(rule.get("min_hits", 1))
        weight = float(rule.get("weight", 1.0))
        if not 1 <= min_hits <= 10 or not 0.1 <= weight <= 2.0:
            raise ValueError("min_hits must be 1..10 and weight 0.1..2")
        vals = (name, str(rule.get("category", "general"))[:60] or "general", json.dumps(phrases, ensure_ascii=False),
                json.dumps(all_of, ensure_ascii=False), json.dumps(exclude, ensure_ascii=False), min_hits, weight,
                1 if rule.get("enabled", True) else 0, json.dumps(require_any, ensure_ascii=False))
        now = time.time()
        if rule.get("id"):
            self.rule(pid, int(rule["id"]))
            self.db.x("UPDATE tg_rules SET name=?,category=?,phrases=?,all_of=?,exclude=?,min_hits=?,weight=?,enabled=?,"
                      "require_any=?,updated_at=? WHERE id=? AND project_id=?", (*vals, now, int(rule["id"]), pid))
            return self.rule(pid, int(rule["id"]))
        rid = self.db.x("INSERT INTO tg_rules(project_id,name,category,phrases,all_of,exclude,min_hits,weight,enabled,"
                        "require_any,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (pid, *vals, now, now))
        return self.rule(pid, rid)

    def rule(self, pid: str, rid: int) -> dict:
        r = self.db.one("SELECT * FROM tg_rules WHERE id=? AND project_id=?", (rid, pid))
        if not r:
            raise ValueError(f"rule {rid} not found in project {pid}")
        return self._decode_rule(r)

    @staticmethod
    def _decode_rule(r: dict) -> dict:
        for k in ("phrases", "all_of", "exclude", "require_any"):
            r[k] = json.loads(r.get(k) or "[]")
        r["enabled"] = bool(r["enabled"])
        return r

    def rules(self, pid: str, only_enabled: bool = False) -> list[dict]:
        rows = self.db.q("SELECT * FROM tg_rules WHERE project_id=?" + (" AND enabled=1" if only_enabled else "") + " ORDER BY id", (pid,))
        return [self._decode_rule(r) for r in rows]

    def delete_rule(self, pid: str, rid: int) -> None:
        self.rule(pid, rid)
        self.db.x("DELETE FROM tg_rules WHERE id=?", (rid,))

    @staticmethod
    def match(rules: list[dict], norm: str) -> list[dict]:
        hits = []
        for r in rules:
            if find_phrases(norm, r["exclude"]):
                continue
            matched = find_phrases(norm, r["phrases"])
            req = [t for t in r["all_of"] if find_phrases(norm, [t])]
            if r["all_of"] and len(req) < len(r["all_of"]):
                continue
            if r.get("require_any") and not find_phrases(norm, r["require_any"]):
                continue
            if r["phrases"] and len(matched) < r["min_hits"]:
                continue
            n = len(matched) + len(req)
            if n < 1:
                continue
            strength = min(1.0, (0.5 + 0.25 * (n - 1)) * r["weight"])
            hits.append({"rule_id": r["id"], "name": r["name"], "category": r["category"],
                         "matched": matched + req, "strength": round(strength, 3)})
        return hits

    def test_rule(self, pid: str, rule: dict, limit: int = 20, scan: int = 5000) -> dict:
        """Dry-run a (possibly unsaved) rule over messages we still retain. Nothing is written."""
        draft = {"id": 0, "name": rule.get("name", "test"), "category": rule.get("category", "general"),
                 "phrases": self._clean_list(rule.get("phrases"), "phrases"), "all_of": self._clean_list(rule.get("all_of"), "all_of"),
                 "exclude": self._clean_list(rule.get("exclude"), "exclude"), "min_hits": int(rule.get("min_hits", 1)),
                 "require_any": self._clean_list(rule.get("require_any"), "require_any"),
                 "weight": float(rule.get("weight", 1.0)), "enabled": True}
        rows = self.db.q("SELECT id, text, message_date FROM tg_messages WHERE project_id=? ORDER BY id DESC LIMIT ?", (pid, scan))
        total, samples = 0, []
        for r in rows:
            if self.match([draft], normalize(r["text"])):
                total += 1
                if len(samples) < limit:
                    samples.append({"message_id": r["id"], "text": r["text"][:200]})
        return {"scanned": len(rows), "matches": total, "samples": samples,
                "note": "only messages within the retention period are available for testing"}

    # ------------------------------------------------------------------ ingestion (both channels)
    def message_url(self, src: dict, tg_message_id: int) -> str:
        if src["username"]:
            return f"https://t.me/{src['username']}/{tg_message_id}"
        if src["chat_ref"].startswith("-100"):
            return f"https://t.me/c/{src['chat_ref'][4:]}/{tg_message_id}"
        return ""

    def ingest_message(self, pid: str, chat_ref: str, tg_message_id: int, text: str, date: float | None = None,
                       author_id: Any = None, author_username: str | None = None) -> dict:
        cfg, now = self.cfg(pid), time.time()
        src = self._source_by_ref(pid, chat_ref)
        if not src:
            return {"result": "ignored_unknown_source"}
        if src["status"] != "active":
            return {"result": f"ignored_source_{src['status']}"}
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            return {"result": "ignored_empty"}
        if self.db.one("SELECT 1 FROM tg_messages WHERE source_id=? AND tg_message_id=?", (src["id"], int(tg_message_id))):
            self.db.x("UPDATE tg_sources SET dup_count=dup_count+1 WHERE id=?", (src["id"],))
            return {"result": "duplicate_delivery"}
        norm = normalize(text)
        nh, sh = norm_hash(norm), simhash(norm)
        hits = self.match(self.rules(pid, only_enabled=True), norm)
        mid = self.db.x(
            "INSERT INTO tg_messages(project_id,source_id,tg_message_id,message_date,text,norm_hash,simhash,author_ref,matched,"
            "rule_hits,state,created_at,author_username) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, src["id"], int(tg_message_id), float(date or now), text, nh, sh, self.author_ref(pid, author_id),
             1 if hits else 0, json.dumps(hits, ensure_ascii=False), "received", now,
             (author_username or "").lstrip("@")[:64] or None if cfg["store_username"] else None))
        self.db.x("UPDATE tg_sources SET msg_count=msg_count+1, last_ok_at=? WHERE id=?", (now, src["id"]))
        if not hits:
            return {"result": "no_match", "message_row_id": mid}
        dup = self._find_duplicate(pid, mid, nh, sh, norm, cfg)
        if dup:
            row, kind = dup
            self.db.x("UPDATE tg_messages SET state='duplicate', dup_of=?, lead_id=?, verdict=? WHERE id=?",
                      (row["id"], row["lead_id"], json.dumps({"dup_kind": kind}), mid))
            if row["lead_id"]:
                self._add_hit(row["lead_id"], mid, kind)
            return {"result": "duplicate", "kind": kind, "of": row["id"], "lead_id": row["lead_id"]}
        self.db.x("UPDATE tg_messages SET state='pending' WHERE id=?", (mid,))
        return {"result": "queued", "message_row_id": mid}

    def ingest_push(self, pid: str, chat_ref: str, tg_message_id: int, text: str, date: float | None = None,
                    author_id: Any = None, author_username: str | None = None) -> dict:
        """Channel B: messages pushed by an operator-run authorised client. Off unless explicitly enabled (TZ §10, ADR-1)."""
        if not self.cfg(pid)["push_enabled"]:
            raise ValueError("push ingestion is disabled (monitor.push_enabled); it needs a documented decision first")
        src = self._source_by_ref(pid, chat_ref)
        if src and src["ingest"] != "push":
            raise ValueError("this source is collected through the Bot API, not pushed")
        return self.ingest_message(pid, chat_ref, tg_message_id, text, date, author_id, author_username)

    def _find_duplicate(self, pid: str, mid: int, nh: str, sh: str, norm: str, cfg: dict):
        cutoff = time.time() - cfg["window_days"] * 86400
        row = self.db.one("SELECT id, lead_id FROM tg_messages WHERE project_id=? AND norm_hash=? AND created_at>=? AND id!=? "
                          "AND matched=1 AND state!='duplicate' ORDER BY id LIMIT 1", (pid, nh, cutoff, mid))
        if row:
            return row, "repost"
        if len(norm.split()) < 4:  # too short for fuzzy matching: exact only
            return None
        # SimHash is a cheap prefilter (generous: short texts move many bits per edited word); token Jaccard confirms.
        for c in self.db.q("SELECT id, lead_id, simhash, text FROM tg_messages WHERE project_id=? AND matched=1 AND created_at>=? "
                           "AND id!=? AND state!='duplicate' ORDER BY id DESC LIMIT 2000", (pid, cutoff, mid)):
            if hamming(c["simhash"], sh) <= cfg["near_distance"] and jaccard(norm, normalize(c["text"])) >= cfg["near_jaccard"]:
                return c, "near_duplicate"
        return None

    def _add_hit(self, lead_id: int, mid: int | None, kind: str) -> None:
        now = time.time()
        self.db.x("INSERT INTO tg_lead_hits(lead_id,message_row_id,kind,created_at) VALUES(?,?,?,?)", (lead_id, mid, kind, now))
        self.db.x("UPDATE tg_leads SET hit_count=hit_count+1, last_seen_at=? WHERE id=?", (now, lead_id))

    # ------------------------------------------------------------------ level 2 (queue worker)
    def process_pending(self, pid: str, limit: int = 50, http: Any = None) -> dict:
        cfg = self.cfg(pid)
        q = Qualifier(cfg, http or self.http)
        stats = {"processed": 0, "leads": 0, "rejected": 0, "linked": 0, "retry": 0}
        new_leads: list[int] = []
        for row in self.db.q("SELECT * FROM tg_messages WHERE project_id=? AND state='pending' ORDER BY id LIMIT ?", (pid, limit)):
            hits = json.loads(row["rule_hits"])
            try:
                v = q.qualify(row["text"], hits)
            except QualifyError as e:
                attempts = row["attempts"] + 1
                if attempts < cfg["max_attempts"]:
                    self.db.x("UPDATE tg_messages SET attempts=?, verdict=? WHERE id=?",
                              (attempts, json.dumps({"error": str(e)[:200]}), row["id"]))
                    stats["retry"] += 1
                    continue
                v = q.rules_only(row["text"], hits)  # model kept failing: never lose the message, flag for a human
            lead_id, outcome = self._decide(pid, row, v, cfg)
            stats["processed"] += 1
            stats[outcome] = stats.get(outcome, 0) + 1
            if outcome == "leads":
                new_leads.append(lead_id)
        if new_leads and cfg["notify"]:
            self._notify(pid, new_leads, cfg["notify_max_per_run"])
        ap = cfg["autopilot"]
        if ap.get("enabled"):
            for lid in new_leads:
                l = self.lead(pid, lid)
                if l["status"] == "new" and l["confidence"] >= ap.get("min_confidence", 0.7):
                    try:
                        self.start_conversation(pid, lid, "autopilot", ap.get("brief", ""))
                        stats["conversations"] = stats.get("conversations", 0) + 1
                    except Exception as e:  # noqa: BLE001  one bad lead must not stop the queue
                        self.add_note(pid, lid, f"автопілот не розпочав діалог: {str(e)[:200]}", "autopilot")
        return stats

    def _decide(self, pid: str, row: dict, v: dict, cfg: dict) -> tuple[int | None, str]:
        verdict = json.dumps(v, ensure_ascii=False)
        if v["spam"] or not v["is_request"] or v["score"] < cfg["threshold"]:
            self.db.x("UPDATE tg_messages SET state='rejected', verdict=? WHERE id=?", (verdict, row["id"]))
            return None, "rejected"
        now = time.time()
        if row["author_ref"]:
            cutoff = now - cfg["author_window_days"] * 86400
            ex = self.db.one("SELECT * FROM tg_leads WHERE project_id=? AND author_ref=? AND category=? AND created_at>=? "
                             "ORDER BY id DESC LIMIT 1", (pid, row["author_ref"], v["category"], cutoff))
            if ex:
                self._add_hit(ex["id"], row["id"], "same_author")
                self.db.x("UPDATE tg_messages SET state='linked', lead_id=?, verdict=? WHERE id=?", (ex["id"], verdict, row["id"]))
                self._attach_queued_duplicates(row["id"], ex["id"])
                if ex["status"] in ("processed", "excluded", "irrelevant") and cfg["reopen_processed"]:
                    self.set_status(pid, ex["id"], "review", "system", "повторний збіг від того самого автора")
                return ex["id"], "linked"
        src = self.db.one("SELECT * FROM tg_sources WHERE id=?", (row["source_id"],)) or {"username": "", "chat_ref": ""}
        status = "review" if (v["confidence"] < cfg["review_below"] or v.get("needs_review")) else "new"
        lid = self.db.x(
            "INSERT INTO tg_leads(project_id,source_id,message_row_id,tg_message_id,message_date,message_text,message_url,"
            "author_ref,category,service,relevance_score,confidence,qualification_reason,qualifier,status,last_seen_at,created_at,"
            "author_username,author_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, row["source_id"], row["id"], row["tg_message_id"], row["message_date"], row["text"],
             self.message_url(src, row["tg_message_id"]) if src.get("chat_ref") else "", row["author_ref"], v["category"],
             v["service"], v["score"], v["confidence"], v["reason"], v["qualifier"], status, now, now,
             row.get("author_username"), row["author_ref"] if cfg["author"] == "raw" else None))
        self.db.x("INSERT INTO tg_lead_history(lead_id,actor,from_status,to_status,note,created_at) VALUES(?,?,?,?,?,?)",
                  (lid, "system", "", status, v["reason"][:300], now))
        self.db.x("UPDATE tg_messages SET state='qualified', lead_id=?, verdict=? WHERE id=?", (lid, verdict, row["id"]))
        self._attach_queued_duplicates(row["id"], lid)
        return lid, "leads"

    def _attach_queued_duplicates(self, orig_id: int, lead_id: int) -> None:
        """Reposts that arrived while the original was still waiting in the queue join its lead once it exists."""
        for d in self.db.q("SELECT id, verdict FROM tg_messages WHERE dup_of=? AND lead_id IS NULL", (orig_id,)):
            kind = json.loads(d["verdict"] or "{}").get("dup_kind", "repost")
            self.db.x("UPDATE tg_messages SET lead_id=? WHERE id=?", (lead_id, d["id"]))
            self._add_hit(lead_id, d["id"], kind)

    def _notify(self, pid: str, lead_ids: list[int], cap: int) -> None:
        for lid in lead_ids[:cap]:
            l = self.lead(pid, lid)
            self.app.notifier.send(pid, f"🔎 Новий лід #{lid} · {l['category']} · оцінка {l['relevance_score']:.2f} "
                                        f"({'на перевірку' if l['status'] == 'review' else 'новий'})\n{l['message_text'][:300]}\n"
                                        f"{l['message_url']}\nЧому: {l['qualification_reason'][:200]}")
        if len(lead_ids) > cap:
            self.app.notifier.send(pid, f"🔎 Ще {len(lead_ids) - cap} нових лідів чекають у панелі.")

    # ------------------------------------------------------------------ leads
    def lead(self, pid: str, lid: int) -> dict:
        r = self.db.one("SELECT * FROM tg_leads WHERE id=? AND project_id=?", (lid, pid))
        if not r:
            raise ValueError(f"lead {lid} not found in project {pid}")
        return r

    def lead_detail(self, pid: str, lid: int) -> dict:
        l = self.lead(pid, lid)
        l["hits"] = self.db.q("SELECT kind, created_at FROM tg_lead_hits WHERE lead_id=? ORDER BY id", (lid,))
        l["history"] = self.db.q("SELECT actor, from_status, to_status, note, created_at FROM tg_lead_history WHERE lead_id=? ORDER BY id", (lid,))
        l["contact_policy"] = self.contact_policy(pid)
        return l

    def contact_policy(self, pid: str) -> dict:
        """What the operator may do. Never includes automatic private messaging (CON-1)."""
        ways = self.cfg(pid)["contact"].get("allowed_ways") or []
        return {"automatic_private_messages": False, "allowed_ways": ways,
                "note": "Контакт лише за задокументованою підставою; див. CON-2…CON-9 у ТЗ."}

    def leads(self, pid: str, status: str | None = None, category: str | None = None, source_id: int | None = None,
              q: str | None = None, since: float | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        sql, args = "SELECT * FROM tg_leads WHERE project_id=?", [pid]
        for col, val in (("status", status), ("category", category), ("source_id", source_id)):
            if val not in (None, ""):
                sql += f" AND {col}=?"
                args.append(val)
        if q:
            sql += " AND message_text LIKE ? ESCAPE '\\'"
            args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if since:
            sql += " AND created_at>=?"
            args.append(since)
        return self.db.q(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", args + [min(int(limit), 500), max(int(offset), 0)])

    def set_status(self, pid: str, lid: int, status: str, actor: str, note: str = "", outcome: str | None = None) -> dict:
        if status not in LEAD_STATUSES:
            raise ValueError(f"status must be one of {LEAD_STATUSES}")
        l = self.lead(pid, lid)
        now = time.time()
        self.db.x("UPDATE tg_leads SET status=?, reviewed_by=CASE WHEN ? IN ('relevant','irrelevant','processed','excluded') "
                  "THEN ? ELSE reviewed_by END, reviewed_at=CASE WHEN ? IN ('relevant','irrelevant','processed','excluded') "
                  "THEN ? ELSE reviewed_at END WHERE id=?", (status, status, actor, status, now, lid))
        if outcome is not None:
            self.db.x("UPDATE tg_leads SET outcome=? WHERE id=?", (outcome[:300], lid))
        self.db.x("INSERT INTO tg_lead_history(lead_id,actor,from_status,to_status,note,created_at) VALUES(?,?,?,?,?,?)",
                  (lid, actor, l["status"], status, note[:500], now))
        return self.lead(pid, lid)

    def add_note(self, pid: str, lid: int, note: str, actor: str) -> dict:
        l = self.lead(pid, lid)
        merged = (l["operator_note"] + "\n" if l["operator_note"] else "") + f"[{_iso(time.time())} {actor}] {note[:1000]}"
        self.db.x("UPDATE tg_leads SET operator_note=? WHERE id=?", (merged[-4000:], lid))
        self.db.x("INSERT INTO tg_lead_history(lead_id,actor,from_status,to_status,note,created_at) VALUES(?,?,?,?,?,?)",
                  (lid, actor, l["status"], l["status"], "note: " + note[:300], time.time()))
        return self.lead(pid, lid)

    def start_conversation(self, pid: str, lid: int, actor: str, brief: str = "") -> dict:
        """Turn a monitored lead into a private conversation: the agent writes the first message, the contact policy decides
        whether it may go out (cold_contact / consent), the outreach limits decide when. Returns the drafted message."""
        l = self.lead(pid, lid)
        if l["status"] in ("irrelevant", "excluded"):
            raise ValueError(f"lead is {l['status']}")
        addr = f"@{l['author_username']}" if l["author_username"] else (l["author_id"] or "")
        if not addr:
            raise ValueError("no way to address the author: enable monitor.store_username (and author: raw for ids)")
        ol = self.app.outreach.add_lead(pid, "telegram", addr, source=f"monitor:{lid}", detected_at=l["message_date"],
                                        data={"post": l["message_text"][:600], "category": l["category"], "service": l["service"],
                                              "post_url": l["message_url"], "monitor_lead": lid})
        if ol["status"] == "opted_out":
            raise ValueError("the person is on the stop-list")
        extra = (brief + " " if brief else "") + f"Their post asked about: {l['service'] or l['category']}."
        msg = self.app.outreach.draft(pid, ol["id"], brief=extra)
        self.set_status(pid, lid, "processed" if l["status"] == "new" else l["status"], actor,
                        f"діалог розпочато (чернетка #{msg['id']}, {msg['status']})")
        return msg

    def delete_lead(self, pid: str, lid: int) -> None:
        self.lead(pid, lid)
        self.db.x("DELETE FROM tg_leads WHERE id=?", (lid,))

    # ------------------------------------------------------------------ collector (official Bot API)
    def collect(self, pid: str, now: float | None = None) -> dict:
        now = now or time.time()
        res = {"received": 0, "ingested": 0, "ignored": 0, "skipped": ""}
        token = self.bot_token(pid)
        if not token:
            res["skipped"] = "no bot token configured (monitor.bot_token / bot_token_env)"
            return res
        if not self.cfg(pid)["enabled"]:
            res["skipped"] = "monitoring disabled"
            return res
        fp = hashlib.sha1(token.encode()).hexdigest()[:10]
        if self._state(pid, "token_fp") != fp:  # credentials changed: forget old auth failure and offset
            self._set_state(pid, "token_fp", fp)
            for k in ("auth_failed", "poll_fail", "poll_next", "poll_offset"):
                self._set_state(pid, k, "")
            self.db.x("UPDATE tg_sources SET status='active', status_reason='' WHERE project_id=? AND status='auth_error'", (pid,))
        if self._state(pid, "auth_failed"):
            res["skipped"] = "authorisation failed: update the bot token"
            return res
        if now < float(self._state(pid, "poll_next", "0") or 0):
            res["skipped"] = "backing off"
            return res
        api = Telegram(token, self.http)
        offset = int(self._state(pid, "poll_offset", "0") or 0)
        try:
            updates = api.updates(offset, timeout=0, allowed=["message", "channel_post", "my_chat_member"])
        except TelegramError as e:
            return self._poll_error(pid, e, now, res)
        self._set_state(pid, "poll_fail", 0)
        for u in updates:
            res["received"] += 1
            self._handle_update(pid, u, res)
            offset = max(offset, u["update_id"] + 1)
        if updates:
            self._set_state(pid, "poll_offset", offset)
        return res

    def _poll_error(self, pid: str, e: TelegramError, now: float, res: dict) -> dict:
        fails = int(self._state(pid, "poll_fail", "0") or 0) + 1
        self._set_state(pid, "poll_fail", fails)
        res["error"] = e.description[:120]
        if e.is_auth:  # stop everything for this bot; never try another credential
            self._set_state(pid, "auth_failed", "1")
            self.db.x("UPDATE tg_sources SET status='auth_error', status_reason=?, error_count=error_count+1 "
                      "WHERE project_id=? AND ingest='bot' AND status='active'", ("bot token rejected by Telegram (401)", pid))
            res["skipped"] = "auth_error"
        elif e.is_rate_limit:
            self._set_state(pid, "poll_next", now + max(e.retry_after, 1))
            res["skipped"] = f"rate limited, waiting {e.retry_after}s"
        elif e.is_conflict:
            self._set_state(pid, "poll_next", now + 60)
            res["skipped"] = "another consumer of getUpdates/webhook is active; not fighting it"
        else:
            self._set_state(pid, "poll_next", now + backoff(fails))
            res["skipped"] = f"temporary error, retry in {backoff(fails)}s"
        return res

    def _handle_update(self, pid: str, u: dict, res: dict) -> None:
        if "my_chat_member" in u:
            m = u["my_chat_member"]
            src = self._source_by_ref(pid, str(m.get("chat", {}).get("id")))
            status = (m.get("new_chat_member") or {}).get("status")
            if src and status in ("left", "kicked"):
                self.set_source_status(pid, src["id"], "unavailable", f"bot removed from chat ({status})")
            elif src and status in ("member", "administrator") and src["status"] == "unavailable" \
                    and src["status_reason"].startswith("bot removed"):
                self.db.x("UPDATE tg_sources SET status='active', status_reason='' WHERE id=?", (src["id"],))
            return
        msg = u.get("message") or u.get("channel_post")
        if not msg:
            return
        chat = msg.get("chat") or {}
        ref = str(chat.get("id"))
        src = self._source_by_ref(pid, ref)
        if not src:  # not an approved source: store nothing but a minimal candidate hint for the admin
            self._note_candidate(pid, chat)
            res["ignored"] += 1
            return
        frm = msg.get("from") or {}
        if frm.get("is_bot"):
            res["ignored"] += 1
            return
        self.db.x("UPDATE tg_sources SET title=?, username=?, kind=? WHERE id=?",
                  ((chat.get("title") or src["title"])[:200], (chat.get("username") or src["username"])[:64],
                   chat.get("type", src["kind"]), src["id"]))
        out = self.ingest_message(pid, ref, msg.get("message_id", 0), msg.get("text") or msg.get("caption") or "",
                                  msg.get("date"), frm.get("id"), frm.get("username"))
        res["ingested" if out["result"] in ("queued", "no_match", "duplicate") else "ignored"] += 1

    def _note_candidate(self, pid: str, chat: dict) -> None:
        try:
            cand = json.loads(self._state(pid, "candidates", "{}"))
        except ValueError:
            cand = {}
        ref = str(chat.get("id"))
        if ref in cand or len(cand) < 50:
            cand[ref] = {"chat_ref": ref, "title": (chat.get("title") or "")[:100], "type": chat.get("type", ""),
                         "username": chat.get("username", ""), "last_seen": time.time()}
            self._set_state(pid, "candidates", json.dumps(cand, ensure_ascii=False))

    def check_sources(self, pid: str, now: float | None = None, max_checks: int = 5) -> dict:
        """Verify that approved sources are still reachable (getChat). Access loss stops that source; no workaround."""
        now = now or time.time()
        token, res = self.bot_token(pid), {"checked": 0, "unavailable": 0}
        if not token or self._state(pid, "auth_failed") or not self.cfg(pid)["enabled"]:
            return res
        api, interval = Telegram(token, self.http), self.cfg(pid)["check_interval_seconds"]
        for s in self.db.q("SELECT * FROM tg_sources WHERE project_id=? AND ingest='bot' AND status='active' AND checked_at<=? "
                           "AND next_attempt_at<=? ORDER BY checked_at LIMIT ?", (pid, now - interval, now, max_checks)):
            res["checked"] += 1
            try:
                chat = api.get_chat(s["chat_ref"]) or {}
                self.db.x("UPDATE tg_sources SET checked_at=?, last_ok_at=?, fail_count=0, last_error='', title=?, username=?, kind=? "
                          "WHERE id=?", (now, now, (chat.get("title") or s["title"])[:200],
                                         (chat.get("username") or s["username"])[:64], chat.get("type", s["kind"]), s["id"]))
            except TelegramError as e:
                if e.is_auth:
                    self._set_state(pid, "auth_failed", "1")
                    self.db.x("UPDATE tg_sources SET status='auth_error', status_reason='bot token rejected (401)' "
                              "WHERE project_id=? AND ingest='bot' AND status='active'", (pid,))
                    break
                if e.is_access:
                    self.set_source_status(pid, s["id"], "unavailable", f"no access: {e.description[:150]}")
                    res["unavailable"] += 1
                    continue
                fails = s["fail_count"] + 1
                wait = e.retry_after if e.is_rate_limit else backoff(fails)
                self.db.x("UPDATE tg_sources SET checked_at=?, fail_count=?, next_attempt_at=?, last_error=?, "
                          "error_count=error_count+1 WHERE id=?", (now, fails, now + wait, e.description[:200], s["id"]))
        return res

    # ------------------------------------------------------------------ reports, export, quality
    def report(self, pid: str, days: int = 30) -> dict:
        since = time.time() - days * 86400
        def n(sql: str, *a) -> int:
            return self.db.one(sql, (pid, since, *a))["n"]
        received = n("SELECT COUNT(*) n FROM tg_messages WHERE project_id=? AND created_at>=?")
        matched = n("SELECT COUNT(*) n FROM tg_messages WHERE project_id=? AND created_at>=? AND matched=1")
        rejected = n("SELECT COUNT(*) n FROM tg_messages WHERE project_id=? AND created_at>=? AND state='rejected'")
        dups = n("SELECT COUNT(*) n FROM tg_messages WHERE project_id=? AND created_at>=? AND state IN ('duplicate','linked')")
        leads = n("SELECT COUNT(*) n FROM tg_leads WHERE project_id=? AND created_at>=?")
        by_status = {r["status"]: r["n"] for r in self.db.q(
            "SELECT status, COUNT(*) n FROM tg_leads WHERE project_id=? AND created_at>=? GROUP BY status", (pid, since))}
        conf = by_status.get("relevant", 0) + by_status.get("processed", 0)
        den = conf + by_status.get("irrelevant", 0)
        srcs = []
        for s in self.sources(pid):
            r = self.db.one("SELECT COUNT(*) leads, SUM(status IN ('relevant','processed')) ok, SUM(status='irrelevant') bad "
                            "FROM tg_leads WHERE source_id=? AND created_at>=?", (s["id"], since))
            srcs.append({"id": s["id"], "title": s["title"] or s["chat_ref"], "status": s["status"], "messages": s["msg_count"],
                         "errors": s["error_count"], "duplicates": s["dup_count"], "leads": r["leads"] or 0,
                         "confirmed": r["ok"] or 0, "rejected_by_operator": r["bad"] or 0, "last_ok_at": s["last_ok_at"]})
        rules: dict[str, dict] = {}
        for m in self.db.q("SELECT rule_hits, state, lead_id FROM tg_messages WHERE project_id=? AND created_at>=? AND matched=1 "
                           "ORDER BY id DESC LIMIT 20000", (pid, since)):
            hits = json.loads(m["rule_hits"])
            if not hits:
                continue
            best = max(hits, key=lambda h: h["strength"])["name"]
            r = rules.setdefault(best, {"rule": best, "matches": 0, "leads": 0})
            r["matches"] += 1
            r["leads"] += 1 if m["state"] == "qualified" else 0
        daily = self.db.q("SELECT strftime('%Y-%m-%d', created_at, 'unixepoch') day, COUNT(*) received, SUM(matched) matched "
                          "FROM tg_messages WHERE project_id=? AND created_at>=? GROUP BY day ORDER BY day", (pid, since))
        errors = sum(s["errors"] for s in srcs)
        return {"days": days, "received": received, "level1_matches": matched, "rejected_by_qualifier": rejected,
                "duplicates": dups, "leads_created": leads, "leads_by_status": by_status,
                "confirmed_relevant_pct": round(100 * conf / den, 1) if den else None, "reviewed": den,
                "sources": sorted(srcs, key=lambda x: -x["confirmed"]), "rules": sorted(rules.values(), key=lambda x: -x["leads"]),
                "daily": daily, "source_errors": errors,
                "sources_unhealthy": sum(1 for s in srcs if s["status"] in ("unavailable", "auth_error"))}

    def export_csv(self, pid: str, status: str | None = None, category: str | None = None, since: float | None = None) -> str:
        names = {s["id"]: (s["title"] or s["chat_ref"]) for s in self.sources(pid)}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(EXPORT_COLUMNS)
        for l in self.leads(pid, status=status, category=category, since=since, limit=500):
            w.writerow([csv_safe(x) for x in (
                l["id"], _iso(l["created_at"]), l["status"], l["category"], l["service"], l["relevance_score"], l["confidence"],
                names.get(l["source_id"], ""), _iso(l["message_date"]), l["message_url"], l["qualification_reason"],
                l["hit_count"], l["operator_note"], l["outcome"], l["message_text"])])
        return buf.getvalue()

    def evaluate(self, pid: str, samples: list[dict], http: Any = None) -> dict:
        """Precision/recall on a labelled sample, through the same pipeline, without writing anything."""
        cfg, rules = self.cfg(pid), self.rules(pid, only_enabled=True)
        q = Qualifier(cfg, http or self.http)
        tp = fp = fn = tn = llm_errors = l1_missed = 0
        wrong = []
        for s in samples[:2000]:
            text, expected = str(s.get("text", "")), s.get("expected") in (True, "relevant", "yes", 1)
            hits = self.match(rules, normalize(text))
            if not hits:
                pred, why = False, "no level-1 match"
                l1_missed += 1 if expected else 0
            else:
                try:
                    v = q.qualify(text, hits)
                except QualifyError:
                    llm_errors += 1
                    v = q.rules_only(text, hits)
                pred = bool(v["is_request"] and not v["spam"] and v["score"] >= cfg["threshold"])
                why = v["reason"]
            tp += pred and expected
            fp += pred and not expected
            fn += (not pred) and expected
            tn += (not pred) and not expected
            if pred != expected:
                wrong.append({"text": text[:200], "expected": expected, "predicted": pred, "why": why[:200]})
        n = tp + fp + fn + tn
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        f1 = 2 * prec * rec / (prec + rec) if prec and rec else None
        return {"samples": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec, "f1": f1,
                "accuracy": (tp + tn) / n if n else None, "level1_missed_relevant": l1_missed, "model_errors": llm_errors,
                "mismatches": wrong[:50]}

    # ------------------------------------------------------------------ retention & erasure
    def purge(self, pid: str, now: float | None = None) -> dict:
        now, cfg = now or time.time(), self.cfg(pid)
        m = self.db.one("SELECT COUNT(*) n FROM tg_messages WHERE project_id=? AND created_at<?", (pid, now - cfg["retention_days"] * 86400))["n"]
        self.db.x("DELETE FROM tg_messages WHERE project_id=? AND created_at<?", (pid, now - cfg["retention_days"] * 86400))
        l = self.db.one("SELECT COUNT(*) n FROM tg_leads WHERE project_id=? AND created_at<?", (pid, now - cfg["lead_retention_days"] * 86400))["n"]
        self.db.x("DELETE FROM tg_leads WHERE project_id=? AND created_at<?", (pid, now - cfg["lead_retention_days"] * 86400))
        self._set_state(pid, "last_purge", now)
        return {"messages_deleted": m, "leads_deleted": l}

    def erase_author(self, pid: str, author_id: Any) -> dict:
        """GDPR erasure by Telegram id: removes the author's leads (with hits/history) and retained messages."""
        refs = {str(author_id), hmac.new(self.app.s.pii_salt(), f"{pid}:{author_id}".encode(), hashlib.sha256).hexdigest()[:32]}
        marks = ",".join("?" * len(refs))
        leads = self.db.one(f"SELECT COUNT(*) n FROM tg_leads WHERE project_id=? AND author_ref IN ({marks})", (pid, *refs))["n"]
        msgs = self.db.one(f"SELECT COUNT(*) n FROM tg_messages WHERE project_id=? AND author_ref IN ({marks})", (pid, *refs))["n"]
        self.db.x(f"DELETE FROM tg_leads WHERE project_id=? AND author_ref IN ({marks})", (pid, *refs))
        self.db.x(f"DELETE FROM tg_messages WHERE project_id=? AND author_ref IN ({marks})", (pid, *refs))
        return {"leads_deleted": leads, "messages_deleted": msgs}

    # ------------------------------------------------------------------ periodic work
    def poll(self, pid: str) -> None:
        """Fast path (every few seconds): fetch updates and qualify what is queued."""
        if self.cfg(pid)["enabled"]:
            self.collect(pid)
            self.process_pending(pid)

    def maintenance(self, pid: str) -> None:
        """Slow path: source health checks and retention."""
        if not self.cfg(pid)["enabled"]:
            return
        self.check_sources(pid)
        if time.time() - float(self._state(pid, "last_purge", "0") or 0) > 3600:
            self.purge(pid)

    def tick(self, pid: str) -> None:
        self.poll(pid)
        self.maintenance(pid)
