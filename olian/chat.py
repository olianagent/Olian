"""Human-facing chat router. Same entry for Telegram, WhatsApp, CLI: (platform, chat, user, text) -> reply.

Chat -> project by binding; user -> role by membership. Slash commands map 1:1 onto facade actions, so RBAC and
audit are identical for humans and agents. Free text goes to the project's agent, scoped to this human's rights.
"""
from __future__ import annotations

import re

from .facade import ActionError, Actor
from .rbac import Forbidden, Role
from .tasks import format_task

HELP = """Команди:
/status — огляд проєкту      /goals · /goal назва | метрика
/tasks — відкриті завдання   /task назва (агенту) · /todo назва (мені)
/done id [результат]         /plan [текст цілі] — розбити ціль на задачі
/sweep — агент робить, що може  /leads [статус] · /funnel
/outbox — чернетки · /approve id · /reject id · /send
/scrape назва                /audit (admin)
Будь-який інший текст — запитання або доручення агенту проєкту.
У приватному чаті з кількома проєктами: #проєкт текст"""


class Chat:
    def __init__(self, app):
        self.app = app

    def handle(self, platform: str, chat_id: str, user_id: str, text: str, name: str = "") -> str:
        text = (text or "").strip()
        if not text:
            return ""
        forced = None
        m = re.match(r"^#([a-z0-9-]{2,39})\s+(.*)$", text, re.S)
        if m:
            forced, text = m.group(1), m.group(2).strip()
        pid, role, scopes = (self.app.projects.resolve(platform, chat_id, user_id) if not forced else
                             (forced, *self.app.projects.role_of(forced, platform, user_id)))
        if not pid or role is None:
            return f"Немає доступу. Попросіть адміністратора проєкту додати вас (ваш id: {user_id})."
        actor = Actor(f"{platform}:{user_id}", role, scopes)  # stable id, not a changeable nickname
        try:
            return self._dispatch(pid, actor, text, chat_id)
        except Forbidden as e:
            return f"⛔ {e}"
        except (ActionError, ValueError) as e:
            return f"⚠️ {e}"

    def _call(self, pid: str, actor: Actor, action: str, **args):
        return self.app.facade.call(pid, actor, action, args)

    def _dispatch(self, pid: str, actor: Actor, text: str, chat_id: str) -> str:
        if not text.startswith("/"):
            return self._call(pid, actor, "chat.ask", text=text)["reply"] or "(порожня відповідь)"
        cmd, _, rest = text[1:].partition(" ")
        cmd, rest = cmd.lower().split("@")[0], rest.strip()
        if cmd in ("help", "start"):
            return HELP
        if cmd == "status":
            s = self._call(pid, actor, "project.status")
            t = ", ".join(f"{k}: {v}" for k, v in s["tasks"].items()) or "немає"
            return (f"📁 {s['name']}\nМета: {s['goal'] or '—'}\nЦілі: {len(s['goals'])}\nЗавдання: {t}\n"
                    f"Ліди: {s['funnel']['leads'] or '—'} · відправлено: {s['funnel']['messages_sent']} · "
                    f"чекають: {s['funnel']['messages_pending']}")
        if cmd == "goals":
            gs = self._call(pid, actor, "goals.list")
            return "\n".join(f"🎯 #{g['id']} {g['title']} {g['metric']}" for g in gs) or "Цілей ще немає."
        if cmd == "goal":
            title, _, metric = rest.partition("|")
            g = self._call(pid, actor, "goals.add", title=title.strip(), metric=metric.strip())
            return f"🎯 Ціль #{g['id']} створено."
        if cmd == "tasks":
            ts = [t for t in self._call(pid, actor, "tasks.list") if t["status"] in ("todo", "doing", "blocked")]
            return "\n\n".join(format_task(t) for t in ts[:15]) or "Відкритих завдань немає."
        if cmd in ("task", "todo"):
            t = self._call(pid, actor, "tasks.add", title=rest, executor="agent" if cmd == "task" else "human",
                           assignee="" if cmd == "task" else actor.label.split(":", 1)[-1])
            return f"✅ Завдання #{t['id']} створено ({t['executor']})."
        if cmd == "done":
            tid, _, res = rest.partition(" ")
            t = self._call(pid, actor, "tasks.update", id=int(tid), status="done", result=res)
            return f"✔️ #{t['id']} виконано."
        if cmd == "plan":
            ts = self._call(pid, actor, "tasks.plan", text=rest)
            return "📋 План:\n" + "\n".join(f"#{t['id']} [{t['executor']}] {t['title']}" for t in ts)
        if cmd == "sweep":
            ts = self._call(pid, actor, "tasks.sweep")
            return "\n".join(f"#{t['id']} {t['status']} [{t['executor']}] {t['title']}" for t in ts) or "Агентських завдань немає."
        if cmd == "leads":
            ls = self._call(pid, actor, "leads.list", status=rest or None, limit=20)
            return "\n".join(f"#{l['id']} {l['status']:<10} {l['channel']}:{l['address']} {l['name']}" for l in ls) or "Лідів немає."
        if cmd == "funnel":
            return str(self._call(pid, actor, "outreach.funnel"))
        if cmd == "outbox":
            ms = self._call(pid, actor, "outreach.outbox", status=rest or "draft")
            return "\n\n".join(f"#{m['id']} → {m['lead_name'] or m['address']}\n{m['body']}" for m in ms) or "Порожньо."
        if cmd == "approve":
            m = self._call(pid, actor, "outreach.approve", id=int(rest))
            return f"👍 #{m['id']} схвалено, піде в найближче вікно відправки."
        if cmd == "reject":
            m = self._call(pid, actor, "outreach.reject", id=int(rest))
            return f"🗑 #{m['id']} відхилено."
        if cmd == "send":
            return str(self._call(pid, actor, "outreach.send_due"))
        if cmd == "scrape":
            return str(self._call(pid, actor, "scrapers.run", name=rest))
        if cmd == "audit":
            rows = self._call(pid, actor, "audit.list", limit=15)
            return "\n".join(f"{'✓' if r['ok'] else '✗'} {r['actor']} {r['action']}" for r in rows) or "Порожньо."
        return "Невідома команда. /help"
