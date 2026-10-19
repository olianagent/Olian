"""Goal/Task controller.

* A goal is broken down into tasks, each with an executor: agent (does it itself), scraper, or human.
* When the agent cannot do something, it becomes a HUMAN task with a step-by-step guide and ready-to-paste
  text, so the person only clicks and pastes.
* Tasks tied to goals (and to nearer deadlines) float to the top.
"""
from __future__ import annotations

import json
import time

from .hermes import HermesError
from .util import extract_json

EXECUTORS = ("agent", "scraper", "human")

PLAN_PROMPT = """You are the task controller of project «{name}». Goal: {goal}
Break it into 3-8 concrete tasks. For each decide the executor:
- "agent": you can do it yourself with your tools,
- "scraper": data collection a scraper can do (name the target),
- "human": needs a person (accounts, payments, calls, decisions, physical actions).
For every human task give "steps": a short list of {{"text": "...", "link": "optional url", "copy": "optional text to paste"}}
so the person only clicks and pastes. Reply with JSON only:
{{"tasks": [{{"title": "...", "detail": "...", "executor": "agent|scraper|human", "assignee": "optional name", "steps": []}}]}}
Extra context: {context}"""

DO_PROMPT = """Do this task for project «{name}» yourself, using your tools.
Task: {title}
Details: {detail}
If you fully completed it, reply: DONE: <short result>.
If you CANNOT (no access, needs a person), reply with JSON only:
{{"needs_human": true, "reason": "...", "steps": [{{"text": "...", "link": "", "copy": ""}}]}}"""


def _now() -> float:
    return time.time()


class Tasks:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    # ---- goals
    def add_goal(self, pid: str, title: str, metric: str = "", deadline: float = 0) -> dict:
        gid = self.db.x("INSERT INTO goals(project_id,title,metric,deadline,created_at) VALUES(?,?,?,?,?)",
                        (pid, title, metric, deadline, _now()))
        return self.db.one("SELECT * FROM goals WHERE id=?", (gid,))

    def goals(self, pid: str, status: str = "active") -> list[dict]:
        return self.db.q("SELECT * FROM goals WHERE project_id=? AND status=? ORDER BY deadline=0, deadline", (pid, status))

    # ---- tasks
    def add(self, pid: str, title: str, detail: str = "", executor: str = "agent", assignee: str = "",
            goal_id: int | None = None, steps: list | None = None, due: float = 0, parent_id: int | None = None,
            notify: bool = True) -> dict:
        if executor not in EXECUTORS:
            raise ValueError(f"executor must be one of {EXECUTORS}")
        if goal_id and not self.db.one("SELECT 1 FROM goals WHERE id=? AND project_id=?", (goal_id, pid)):
            raise ValueError("goal not in this project")
        t = _now()
        tid = self.db.x("INSERT INTO tasks(project_id,goal_id,parent_id,title,detail,executor,assignee,steps,due,"
                        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, goal_id, parent_id, title, detail, executor, assignee,
                         json.dumps(steps or [], ensure_ascii=False), due, t, t))
        task = self.get(pid, tid)
        if executor == "human" and notify:
            self.app.notifier.send(pid, "🧑 Нове завдання для людини\n" + format_task(task),
                                   min_role="manager", only=[assignee] if assignee else None)
        return task

    def get(self, pid: str, tid: int) -> dict:
        row = self.db.one("SELECT * FROM tasks WHERE id=? AND project_id=?", (tid, pid))
        if not row:
            raise ValueError(f"task {tid} not found in project {pid}")
        row["steps"] = json.loads(row["steps"] or "[]")
        return row

    def list(self, pid: str, status: str | None = None, executor: str | None = None, assignee: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM tasks WHERE project_id=?", [pid]
        for col, val in (("status", status), ("executor", executor), ("assignee", assignee)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        rows = self.db.q(sql, args)
        goals = {g["id"]: g for g in self.db.q("SELECT * FROM goals WHERE project_id=?", (pid,))}

        def prio(r):  # goal-linked first, then nearest goal deadline / due date, then oldest
            g = goals.get(r["goal_id"])
            dl = (g["deadline"] if g and g["deadline"] else 0) or r["due"] or 1e18
            return (0 if g else 1, dl, r["id"])
        rows.sort(key=prio)
        for r in rows:
            r["steps"] = json.loads(r["steps"] or "[]")
        return rows

    def set_status(self, pid: str, tid: int, status: str, result: str = "", blocked_reason: str = "") -> dict:
        if status not in ("todo", "doing", "blocked", "done", "cancelled"):
            raise ValueError("bad status")
        self.get(pid, tid)
        self.db.x("UPDATE tasks SET status=?, result=?, blocked_reason=?, updated_at=? WHERE id=? AND project_id=?",
                  (status, result, blocked_reason, _now(), tid, pid))
        return self.get(pid, tid)

    # ---- controller (uses the project's Hermes agent)
    def plan(self, pid: str, goal_id: int | None = None, text: str = "", context: str = "") -> list[dict]:
        project = self.app.projects.get(pid)
        goal = text
        if goal_id:
            g = self.db.one("SELECT * FROM goals WHERE id=? AND project_id=?", (goal_id, pid))
            if not g:
                raise ValueError("goal not found")
            goal = g["title"] + (f" (metric: {g['metric']})" if g["metric"] else "")
        goal = goal or project["goal"]
        if not goal:
            raise ValueError("no goal given and the project has no goal")
        reply = self.app.agent_chat(pid, PLAN_PROMPT.format(name=project["name"], goal=goal, context=context or "-"),
                                    session=f"plan-{goal_id or 'adhoc'}")
        try:
            items = extract_json(reply)["tasks"]
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(f"controller could not parse the plan: {e}") from e
        out = []
        for it in items[:12]:
            ex = it.get("executor", "agent")
            out.append(self.add(pid, str(it.get("title", "untitled"))[:200], str(it.get("detail", "")),
                                ex if ex in EXECUTORS else "agent", str(it.get("assignee", "")), goal_id,
                                it.get("steps") or []))
        return out

    def sweep(self, pid: str, limit: int = 5) -> list[dict]:
        """«Go through the tasks, do what you can yourself»: the agent works its own queue; what it can't
        becomes a human task with a guide."""
        project = self.app.projects.get(pid)
        done = []
        for t in [x for x in self.list(pid, status="todo", executor="agent")][:limit]:
            self.set_status(pid, t["id"], "doing")
            try:
                reply = self.app.agent_chat(pid, DO_PROMPT.format(name=project["name"], title=t["title"], detail=t["detail"]),
                                            session=f"task-{t['id']}")
            except HermesError as e:
                self.set_status(pid, t["id"], "todo", result=f"agent unavailable: {e}")
                break
            if reply.strip().upper().startswith("DONE"):
                done.append(self.set_status(pid, t["id"], "done", result=reply.split(":", 1)[-1].strip()))
                continue
            try:
                need = extract_json(reply)
                assert need.get("needs_human")
            except (ValueError, AssertionError, AttributeError):
                done.append(self.set_status(pid, t["id"], "done", result=reply.strip()[:1000]))
                continue
            self.db.x("UPDATE tasks SET executor='human', steps=?, status='todo', blocked_reason=?, updated_at=? "
                      "WHERE id=? AND project_id=?",
                      (json.dumps(need.get("steps") or [], ensure_ascii=False), str(need.get("reason", ""))[:500],
                       _now(), t["id"], pid))
            task = self.get(pid, t["id"])
            self.app.notifier.send(pid, "🧑 Агент не може сам — потрібна людина\n" + format_task(task))
            done.append(task)
        return done


def format_task(t: dict) -> str:
    lines = [f"#{t['id']} [{t['executor']}] {t['title']}"]
    if t.get("detail"):
        lines.append(t["detail"])
    if t.get("blocked_reason"):
        lines.append(f"Чому не агент: {t['blocked_reason']}")
    for i, st in enumerate(t.get("steps") or [], 1):
        row = f"  {i}. {st.get('text', '')}"
        if st.get("link"):
            row += f"\n     🔗 {st['link']}"
        if st.get("copy"):
            row += f"\n     📋 {st['copy']}"
        lines.append(row)
    return "\n".join(lines)
