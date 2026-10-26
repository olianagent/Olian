"""Event inbox + rules. Scrapers, webhooks and external monitors all POST events; rules turn matching events into
leads, tasks or notifications. A lead from a signal is for a HUMAN to act on: nothing here contacts anyone."""
from __future__ import annotations

import json
import time
from typing import Any

from .hermes import HermesError
from .util import extract_json

QUALIFY_PROMPT = """Project «{name}», goal: {goal}.
Is this a real prospect or relevant signal for the project? Event: {event}
Reply with JSON only: {{"qualified": true|false, "reason": "<short>"}}"""


def dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
        if obj is None:
            return None
    return obj


def _text(payload: dict) -> str:
    return " ".join(str(payload.get(k, "")) for k in ("text", "title", "body", "message") if payload.get(k)).lower()


class Signals:
    def __init__(self, app):
        self.app, self.db = app, app.db

    def ingest(self, pid: str, source: str, kind: str, payload: dict) -> int:
        return self.db.add_event(pid, source, kind, payload)

    def events(self, pid: str, limit: int = 50, handled: bool | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE project_id=?", [pid]
        if handled is not None:
            sql += " AND handled=?"
            args.append(int(handled))
        rows = self.db.q(sql + " ORDER BY id DESC LIMIT ?", args + [limit])
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    @staticmethod
    def matches(rule: dict, payload: dict) -> bool:
        t = _text(payload)
        if any(x.lower() in t for x in rule.get("exclude", [])):
            return False
        if rule.get("all_of") and not all(x.lower() in t for x in rule["all_of"]):
            return False
        kws = rule.get("keywords", [])
        return bool(not kws or any(k.lower() in t for k in kws)) and bool(kws or rule.get("all_of"))

    def process(self, pid: str, limit: int = 100) -> dict:
        project = self.app.projects.get(pid)
        rules = project["cfg"].get("signals", {}).get("rules", [])
        stats = {"events": 0, "leads": 0, "tasks": 0, "notified": 0}
        for ev in self.db.q("SELECT * FROM events WHERE project_id=? AND handled=0 ORDER BY id LIMIT ?", (pid, limit)):
            payload = json.loads(ev["payload"])
            retry = False
            for rule in rules:
                if rule.get("source") and rule["source"] not in ev["source"]:
                    continue
                if not self.matches(rule, payload):
                    continue
                if rule.get("ai_qualify"):
                    try:
                        v = extract_json(self.app.agent_chat(pid, QUALIFY_PROMPT.format(
                            name=project["name"], goal=project["goal"] or "-", event=json.dumps(payload, ensure_ascii=False)[:1500]),
                            session="qualify"))
                        if not v.get("qualified"):
                            continue
                    except HermesError:
                        retry = True  # agent down: try this event again next tick
                        break
                    except (ValueError, AttributeError):
                        continue
                self._act(pid, rule, ev, payload, stats)
            if retry:
                break
            self.db.x("UPDATE events SET handled=1 WHERE id=?", (ev["id"],))
            stats["events"] += 1
        return stats

    def _act(self, pid: str, rule: dict, ev: dict, payload: dict, stats: dict) -> None:
        action = rule.get("action", "notify")
        title = rule.get("name", "signal")
        if action == "lead":
            addr = dig(payload, rule.get("address_field", "user.username")) or dig(payload, "user.id")
            if not addr:
                return
            lead = self.app.outreach.add_lead(
                pid, rule.get("channel", "telegram"), str(addr), name=str(dig(payload, rule.get("name_field", "user.name")) or ""),
                source=f"signal:{title}", data={"signal": _text(payload)[:500], "source": ev["source"]},
                detected_at=float(payload.get("ts") or ev["created_at"]))
            stats["leads"] += 1
            if lead["status"] == "new":  # a human decides how (and whether) to engage; see Outreach.contact_block
                try:
                    if rule.get("auto_draft", False):
                        self.app.outreach.draft(pid, lead["id"], brief=f"Reacting to their message: {_text(payload)[:300]}")
                except (HermesError, ValueError):
                    pass
                self.app.notifier.send(pid, f"🔔 Сигнал «{title}» → лід #{lead['id']} ({lead['channel']}:{lead['address']}); "
                                            f"автоконтакту немає, рішення за оператором.\n{_text(payload)[:300]}")
        elif action == "task":
            self.app.tasks.add(pid, f"{title}: {_text(payload)[:80]}", detail=json.dumps(payload, ensure_ascii=False)[:800],
                               executor=rule.get("executor", "human"), assignee=rule.get("assignee", ""))
            stats["tasks"] += 1
        else:
            stats["notified"] += self.app.notifier.send(pid, f"🔔 {title}\n{_text(payload)[:500]}")
