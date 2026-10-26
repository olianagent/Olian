"""Every capability of a project, as a named, permissioned, audited action. This is the contract that the
agent (via MCP), humans (via chat/CLI), scrapers and scripts (via REST) all share."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .facade import Actor, Facade
from .rbac import Role
from .tasks import format_task

V, M, A = Role.VIEWER, Role.MANAGER, Role.ADMIN
INT = {"type": "integer"}
STR = {"type": "string"}


def _ts(v) -> float:
    """Accept epoch seconds or YYYY-MM-DD."""
    if not v:
        return 0
    if isinstance(v, (int, float)):
        return float(v)
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def register_all(f: Facade) -> None:
    # ----------------------------------------------------------------- project
    @f.register("project.status", "Project overview: goal, active goals, task counts, outreach funnel.", V)
    def project_status(app, pid, actor):
        p = app.projects.get(pid)
        counts = {r["status"]: r["n"] for r in app.db.q(
            "SELECT status, COUNT(*) n FROM tasks WHERE project_id=? GROUP BY status", (pid,))}
        return {"id": pid, "name": p["name"], "goal": p["goal"], "goals": app.tasks.goals(pid), "tasks": counts,
                "funnel": app.outreach.funnel(pid)}

    # ----------------------------------------------------------------- goals & tasks
    @f.register("goals.list", "List active goals.", V)
    def goals_list(app, pid, actor):
        return app.tasks.goals(pid)

    @f.register("goals.add", "Create a goal (optional metric, deadline YYYY-MM-DD).", M,
                {"title": STR, "metric": STR, "deadline": STR}, ["title"])
    def goals_add(app, pid, actor, title, metric="", deadline=""):
        return app.tasks.add_goal(pid, title, metric, _ts(deadline))

    @f.register("tasks.list", "List tasks, goal-linked and most urgent first. Filter by status/executor/assignee.", V,
                {"status": STR, "executor": STR, "assignee": STR})
    def tasks_list(app, pid, actor, status=None, executor=None, assignee=None):
        return app.tasks.list(pid, status, executor, assignee)

    @f.register("tasks.add", "Create a task. executor: agent|scraper|human. For human tasks give steps "
                "[{text, link?, copy?}] so the person only clicks and pastes.", M,
                {"title": STR, "detail": STR, "executor": STR, "assignee": STR, "goal_id": INT,
                 "steps": {"type": "array", "items": {"type": "object"}}, "due": STR}, ["title"])
    def tasks_add(app, pid, actor, title, detail="", executor="agent", assignee="", goal_id=None, steps=None, due=""):
        return app.tasks.add(pid, title, detail, executor, assignee, goal_id, steps, _ts(due))

    @f.register("tasks.update", "Change a task's status (todo|doing|blocked|done|cancelled) and optionally its result.", M,
                {"id": INT, "status": STR, "result": STR, "blocked_reason": STR}, ["id", "status"])
    def tasks_update(app, pid, actor, id, status, result="", blocked_reason=""):
        return app.tasks.set_status(pid, id, status, result, blocked_reason)

    @f.register("tasks.plan", "Controller: break a goal into tasks (agent / scraper / human).", M,
                {"goal_id": INT, "text": STR, "context": STR}, agent_ok=False)
    def tasks_plan(app, pid, actor, goal_id=None, text="", context=""):
        return app.tasks.plan(pid, goal_id, text, context)

    @f.register("tasks.sweep", "Let the agent work through its own queue; what it can't do becomes a human task.", M,
                {"limit": INT}, agent_ok=False)
    def tasks_sweep(app, pid, actor, limit=5):
        return app.tasks.sweep(pid, limit)

    # ----------------------------------------------------------------- leads & outreach
    @f.register("leads.list", "List leads (filter by status).", V, {"status": STR, "limit": INT})
    def leads_list(app, pid, actor, status=None, limit=100):
        return app.outreach.leads(pid, status, limit)

    @f.register("leads.add", "Add a lead/contact. channel: whatsapp|telegram|email. source 'warm' = nurtured contact "
                "(use warmth 0-100).", M,
                {"channel": STR, "address": STR, "name": STR, "source": STR, "data": {"type": "object"},
                 "warmth": INT, "next_touch": STR}, ["channel", "address"])
    def leads_add(app, pid, actor, channel, address, name="", source="manual", data=None, warmth=0, next_touch=""):
        return app.outreach.add_lead(pid, channel, address, name, source, data, warmth, _ts(next_touch))

    @f.register("leads.set_status", "Set a lead's status (new|contacted|replied|qualified|handed_off|lost|opted_out).", M,
                {"id": INT, "status": STR}, ["id", "status"])
    def leads_set_status(app, pid, actor, id, status):
        return app.outreach.set_lead_status(pid, id, status)

    @f.register("outreach.compose", "Write an outreach message yourself for a lead. It stays a DRAFT until a human "
                "approves it (unless the project disables approval).", M, {"lead_id": INT, "body": STR}, ["lead_id", "body"])
    def outreach_compose(app, pid, actor, lead_id, body):
        return app.outreach.compose(pid, lead_id, body)

    @f.register("outreach.draft", "Ask the agent to draft a message for a lead.", M, {"lead_id": INT, "brief": STR},
                ["lead_id"], agent_ok=False)
    def outreach_draft(app, pid, actor, lead_id, brief=""):
        return app.outreach.draft(pid, lead_id, brief)

    @f.register("outreach.outbox", "List messages by status (draft|approved|sent|failed|rejected).", V, {"status": STR})
    def outreach_outbox(app, pid, actor, status="draft"):
        return app.outreach.outbox(pid, status)

    @f.register("outreach.approve", "Approve a draft for sending.", M, {"id": INT}, ["id"], agent_ok=False)
    def outreach_approve(app, pid, actor, id):
        return app.outreach.approve(pid, id)

    @f.register("outreach.reject", "Reject a draft.", M, {"id": INT}, ["id"], agent_ok=False)
    def outreach_reject(app, pid, actor, id):
        return app.outreach.reject(pid, id)

    @f.register("outreach.send_due", "Send approved messages now (respecting window, caps, pacing).", M, agent_ok=False)
    def outreach_send_due(app, pid, actor):
        return app.outreach.send_due(pid)

    @f.register("outreach.funnel", "Lead funnel counts.", V)
    def outreach_funnel(app, pid, actor):
        return app.outreach.funnel(pid)

    @f.register("outreach.add_sender", "Register a sending account (whatsapp_cloud|webhook|dryrun) with a daily cap.", A,
                {"label": STR, "channel": STR, "config": {"type": "object"}, "daily_cap": INT}, ["label", "channel"],
                agent_ok=False)
    def outreach_add_sender(app, pid, actor, label, channel, config=None, daily_cap=20):
        app.outreach.add_sender(pid, label, channel, config, daily_cap)
        return {"ok": True}

    @f.register("outreach.senders", "List senders with status (active|tripped), caps and usage; secrets are masked.", A,
                agent_ok=False)
    def outreach_senders(app, pid, actor):
        return app.outreach.senders_public(pid)

    @f.register("outreach.reset_sender", "Re-enable a sender stopped by the circuit breaker, after the cause was investigated.",
                A, {"label": STR}, ["label"], agent_ok=False)
    def outreach_reset(app, pid, actor, label):
        return app.outreach.reset_sender(pid, label)

    @f.register("outreach.stop", "Put a person on the project stop-list (final, applies to every channel).", M,
                {"channel": STR, "address": STR}, ["channel", "address"], agent_ok=False)
    def outreach_stop(app, pid, actor, channel, address):
        return app.outreach.stop(pid, channel, address)

    @f.register("outreach.reply", "Record an inbound reply from a lead (used by channel bridges).", M,
                {"channel": STR, "address": STR, "text": STR, "name": STR}, ["channel", "address", "text"], agent_ok=False)
    def outreach_reply(app, pid, actor, channel, address, text, name=""):
        return app.outreach.handle_reply(pid, channel, address, text, name)

    # ----------------------------------------------------------------- scrapers & events
    @f.register("scrapers.list", "List the project's scrapers.", V)
    def scrapers_list(app, pid, actor):
        return app.scrapers.list(pid)

    @f.register("scrapers.add", "Register a scraper (http|rss|command|external) with an optional interval in minutes.", A,
                {"name": STR, "kind": STR, "spec": {"type": "object"}, "interval_minutes": INT}, ["name", "kind"],
                agent_ok=False)
    def scrapers_add(app, pid, actor, name, kind, spec=None, interval_minutes=0):
        app.scrapers.register(pid, name, kind, spec, interval_minutes)
        return {"ok": True}

    @f.register("scrapers.run", "Run a scraper now; new items become events.", M, {"name": STR}, ["name"])
    def scrapers_run(app, pid, actor, name):
        return app.scrapers.run(pid, name)

    @f.register("events.ingest", "Push an event (from a scraper/monitor) into the project inbox.", M,
                {"source": STR, "kind": STR, "payload": {"type": "object"}}, ["source", "payload"])
    def events_ingest(app, pid, actor, source, payload, kind="item"):
        return {"event_id": app.signals.ingest(pid, source, kind, payload)}

    @f.register("events.list", "Recent events.", V, {"limit": INT})
    def events_list(app, pid, actor, limit=50):
        return app.signals.events(pid, limit)

    @f.register("notify.send", "Notify project members (min_role: viewer|manager|admin).", M,
                {"text": STR, "min_role": STR}, ["text"])
    def notify_send(app, pid, actor, text, min_role="manager"):
        return {"delivered": app.notifier.send(pid, text, min_role)}

    @f.register("audit.list", "Recent audit log entries.", A, {"limit": INT}, agent_ok=False)
    def audit_list(app, pid, actor, limit=50):
        return app.db.q("SELECT * FROM audit WHERE project_id=? ORDER BY id DESC LIMIT ?", (pid, limit))

    @f.register("chat.ask", "Ask the project's agent a question or give it an instruction.", V, {"text": STR}, ["text"],
                agent_ok=False)
    def chat_ask(app, pid, actor, text):
        return {"reply": app.agent_chat(pid, text, session=f"chat-{actor.label}", actor=actor)}


    # ----------------------------------------------------------------- consent (contact module guard)
    @f.register("leads.set_consent", "Record the legal basis for contacting a lead (required before any message to the "
                "author of a monitored post). Describe it, e.g. 'asked to be contacted in the group'.", M,
                {"id": INT, "basis": STR}, ["id", "basis"], agent_ok=False)
    def leads_set_consent(app, pid, actor, id, basis):
        return app.outreach.set_consent(pid, id, f"{basis} (recorded by {actor.label})")

    _register_monitor(f)


def _register_monitor(f: Facade) -> None:
    """Telegram monitoring (docs/TZ-telegram-monitoring.md). Closed to the agent: the data is untrusted text (SEC-9)."""
    OBJ = {"type": "object"}

    @f.register("monitor.sources.list", "List monitored sources with status, last success, counters.", V, agent_ok=False)
    def src_list(app, pid, actor):
        return app.monitor.sources(pid)

    @f.register("monitor.sources.candidates", "Chats the bot is in but that are not approved sources.", A, agent_ok=False)
    def src_cand(app, pid, actor):
        return app.monitor.candidates(pid)

    @f.register("monitor.sources.add", "Add an approved source (Telegram chat id). ingest: bot (default) | push.", A,
                {"chat_ref": STR, "title": STR, "username": STR, "kind": STR, "ingest": STR}, ["chat_ref"], agent_ok=False)
    def src_add(app, pid, actor, chat_ref, title="", username="", kind="", ingest="bot"):
        return app.monitor.add_source(pid, chat_ref, title, username, kind, ingest)

    @f.register("monitor.sources.update", "Pause/resume a source (status: active|paused) or rename it.", A,
                {"id": INT, "status": STR, "title": STR}, ["id"], agent_ok=False)
    def src_update(app, pid, actor, id, status=None, title=None):
        return app.monitor.update_source(pid, id, status, title)

    @f.register("monitor.sources.remove", "Remove a source and its retained messages (leads are kept).", A, {"id": INT}, ["id"],
                agent_ok=False)
    def src_remove(app, pid, actor, id):
        app.monitor.remove_source(pid, id)
        return {"ok": True}

    @f.register("monitor.source_status", "Report a source as unavailable/auth_error with a reason (for push clients).", M,
                {"id": INT, "status": STR, "reason": STR}, ["id", "status"], agent_ok=False)
    def src_status(app, pid, actor, id, status, reason=""):
        return app.monitor.set_source_status(pid, id, status, reason)

    @f.register("monitor.rules.list", "List keyword rules.", V, agent_ok=False)
    def rules_list(app, pid, actor):
        return app.monitor.rules(pid)

    @f.register("monitor.rules.save", "Create or update a rule: {id?, name, category, phrases[], all_of[], exclude[], "
                "min_hits, weight, enabled}. Phrases support prefix wildcard 'куп*'.", A, {"rule": OBJ}, ["rule"], agent_ok=False)
    def rules_save(app, pid, actor, rule):
        return app.monitor.save_rule(pid, rule)

    @f.register("monitor.rules.delete", "Delete a rule.", A, {"id": INT}, ["id"], agent_ok=False)
    def rules_delete(app, pid, actor, id):
        app.monitor.delete_rule(pid, id)
        return {"ok": True}

    @f.register("monitor.rules.test", "Dry-run a rule over retained messages (nothing is written).", M,
                {"rule": OBJ, "limit": INT}, ["rule"], agent_ok=False)
    def rules_test(app, pid, actor, rule, limit=20):
        return app.monitor.test_rule(pid, rule, limit)

    @f.register("monitor.ingest", "Push one message from an authorised operator client (channel B; disabled by default).", M,
                {"chat_ref": STR, "message_id": INT, "text": STR, "date": {"type": "number"}, "author_id": STR},
                ["chat_ref", "message_id", "text"], agent_ok=False)
    def ingest(app, pid, actor, chat_ref, message_id, text, date=None, author_id=None):
        return app.monitor.ingest_push(pid, chat_ref, message_id, text, date, author_id)

    @f.register("monitor.collect", "Poll the Bot API once now.", M, agent_ok=False)
    def collect(app, pid, actor):
        return app.monitor.collect(pid)

    @f.register("monitor.process", "Run level-2 qualification on queued messages now.", M, {"limit": INT}, agent_ok=False)
    def process(app, pid, actor, limit=50):
        return app.monitor.process_pending(pid, limit)

    @f.register("monitor.leads.list", "List leads. Filters: status, category, source_id, q (text), since (epoch).", V,
                {"status": STR, "category": STR, "source_id": INT, "q": STR, "since": {"type": "number"}, "limit": INT,
                 "offset": INT}, agent_ok=False)
    def leads_list_(app, pid, actor, status=None, category=None, source_id=None, q=None, since=None, limit=100, offset=0):
        return app.monitor.leads(pid, status, category, source_id, q, since, limit, offset)

    @f.register("monitor.leads.get", "Lead with hit history, status history and the allowed contact ways.", V, {"id": INT},
                ["id"], agent_ok=False)
    def leads_get(app, pid, actor, id):
        return app.monitor.lead_detail(pid, id)

    @f.register("monitor.leads.set_status", "Review a lead: new|review|relevant|irrelevant|processed|excluded, with a note "
                "and optional outcome of the manual handling.", M, {"id": INT, "status": STR, "note": STR, "outcome": STR},
                ["id", "status"], agent_ok=False)
    def leads_status(app, pid, actor, id, status, note="", outcome=None):
        return app.monitor.set_status(pid, id, status, actor.label, note, outcome)

    @f.register("monitor.leads.note", "Add an operator note to a lead.", M, {"id": INT, "note": STR}, ["id", "note"],
                agent_ok=False)
    def leads_note(app, pid, actor, id, note):
        return app.monitor.add_note(pid, id, note, actor.label)

    @f.register("monitor.leads.delete", "Delete a lead and its history (data erasure).", A, {"id": INT}, ["id"], agent_ok=False)
    def leads_delete(app, pid, actor, id):
        app.monitor.delete_lead(pid, id)
        return {"ok": True}

    @f.register("monitor.report", "Funnel report for the last N days (received, matched, qualified, duplicates, errors, "
                "confirmed %, top sources/rules, daily series).", V, {"days": INT}, agent_ok=False)
    def report(app, pid, actor, days=30):
        return app.monitor.report(pid, days)

    @f.register("monitor.export", "Export leads as CSV text (formula-safe).", M, {"status": STR, "category": STR, "since": {"type": "number"}},
                agent_ok=False)
    def export(app, pid, actor, status=None, category=None, since=None):
        return {"csv": app.monitor.export_csv(pid, status, category, since)}

    @f.register("monitor.evaluate", "Precision/recall on a labelled sample [{text, expected: relevant|irrelevant}].", M,
                {"samples": {"type": "array", "items": OBJ}}, ["samples"], agent_ok=False)
    def evaluate(app, pid, actor, samples):
        return app.monitor.evaluate(pid, samples)

    @f.register("monitor.purge", "Apply retention now (delete expired messages and leads).", A, agent_ok=False)
    def purge(app, pid, actor):
        return app.monitor.purge(pid)

    @f.register("monitor.erase_author", "Erase everything stored about a Telegram author id (GDPR erasure).", A,
                {"author_id": STR}, ["author_id"], agent_ok=False)
    def erase(app, pid, actor, author_id):
        return app.monitor.erase_author(pid, author_id)


__all__ = ["register_all", "format_task"]
