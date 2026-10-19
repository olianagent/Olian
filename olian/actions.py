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


__all__ = ["register_all", "format_task"]
