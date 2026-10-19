"""`olian` command line: manage projects, people, senders, scrapers; talk to agents; run the server."""
from __future__ import annotations

import argparse
import json
import sys

from .app import App
from .facade import Actor
from .rbac import Role

ADMIN = Actor("cli", Role.ADMIN)


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False, indent=2, default=str)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="olian", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the data directory")
    p = sub.add_parser("project", help="manage projects")
    ps = p.add_subparsers(dest="sub", required=True)
    c = ps.add_parser("create")
    c.add_argument("id"); c.add_argument("--name"); c.add_argument("--goal", default="")
    c.add_argument("--role", default="", help="agent role/persona, e.g. 'Outreach manager'")
    c.add_argument("--tone", default=""); c.add_argument("--language", default="")
    c.add_argument("--telegram-token", default=""); c.add_argument("--model", default=""); c.add_argument("--provider", default="")
    c.add_argument("--sandbox", default="docker", choices=["docker", "local", "ssh"])
    c.add_argument("--config", default="{}", help="extra JSON config merged in")
    ps.add_parser("list")
    for n in ("show", "start", "stop", "delete", "reprovision"):
        ps.add_parser(n).add_argument("id")
    cs = ps.add_parser("config"); cs.add_argument("id"); cs.add_argument("patch", help="JSON merged into the project config")

    m = sub.add_parser("member", help="add a person to a project")
    m.add_argument("project"); m.add_argument("--platform", required=True, choices=["telegram", "whatsapp", "cli", "api"])
    m.add_argument("--user", required=True); m.add_argument("--role", default="viewer", choices=["admin", "manager", "viewer"])
    m.add_argument("--name", default=""); m.add_argument("--scopes", default="", help="e.g. 'tasks.*,leads.list' to restrict")
    b = sub.add_parser("bind", help="bind a chat/group to a project")
    b.add_argument("project"); b.add_argument("--platform", required=True); b.add_argument("--chat", required=True)
    t = sub.add_parser("token", help="issue an API token (scraper, script, MCP client)")
    t.add_argument("project"); t.add_argument("--label", required=True); t.add_argument("--role", default="manager")
    t.add_argument("--scopes", default="")
    s = sub.add_parser("sender", help="register an outreach sender")
    s.add_argument("project"); s.add_argument("label"); s.add_argument("channel", choices=["whatsapp_cloud", "webhook", "dryrun"])
    s.add_argument("--config", default="{}"); s.add_argument("--cap", type=int, default=20)
    sc = sub.add_parser("scraper", help="register a scraper")
    sc.add_argument("project"); sc.add_argument("name"); sc.add_argument("kind", choices=["http", "rss", "command", "external"])
    sc.add_argument("--spec", default="{}"); sc.add_argument("--every", type=int, default=0, help="minutes")
    a = sub.add_parser("ask", help="talk to a project's agent as admin"); a.add_argument("project"); a.add_argument("text")
    d = sub.add_parser("do", help="call a facade action"); d.add_argument("project"); d.add_argument("action")
    d.add_argument("args", nargs="?", default="{}")
    r = sub.add_parser("serve", help="run the orchestrator API + workers")
    r.add_argument("--host"); r.add_argument("--port", type=int)
    r.add_argument("--start-agents", action="store_true", help="also start every project's Hermes gateway")
    sub.add_parser("tick", help="run one pass of background work")

    args = ap.parse_args(argv)
    core = App()

    if args.cmd == "init":
        print(f"data dir: {core.s.data_dir}")
        if not core.s.api_key:
            print("next: export OLIAN_API_KEY=<long random string>   (enables the admin API)")
        return 0
    if args.cmd == "project":
        pr = core.projects
        if args.sub == "create":
            cfg = json.loads(args.config)
            agent = {k: v for k, v in (("role", args.role), ("tone", args.tone), ("language", args.language)) if v}
            if agent:
                cfg.setdefault("agent", {}).update(agent)
            if args.telegram_token:
                cfg.setdefault("channels", {}).setdefault("telegram", {})["bot_token"] = args.telegram_token
            if args.model or args.provider:
                cfg.setdefault("model", {}).update({k: v for k, v in (("default", args.model), ("provider", args.provider)) if v})
            cfg.setdefault("sandbox", {})["backend"] = args.sandbox
            p = pr.create(args.id, args.name or args.id, args.goal, cfg)
            print(f"created {p['id']} (agent api port {p['api_port']}); hermes home: {core.s.hermes_root / p['id']}")
        elif args.sub == "list":
            for p in pr.list():
                print(f"{p['id']:<20} port={p['api_port']} {p['status']:<8} {p['name']}")
        elif args.sub == "show":
            p = pr.get(args.id); p.pop("api_key"); print(_j(p)); print(_j(pr.members(args.id)))
        elif args.sub == "start":
            print("started, pid", pr.start(args.id))
        elif args.sub == "stop":
            pr.stop(args.id)
        elif args.sub == "delete":
            pr.delete(args.id)
        elif args.sub == "reprovision":
            pr.provision(args.id)
        elif args.sub == "config":
            pr.update_config(args.id, json.loads(args.patch))
        return 0
    if args.cmd == "member":
        core.projects.add_member(args.project, args.platform, args.user, args.role, args.name, args.scopes)
    elif args.cmd == "bind":
        core.projects.bind(args.project, args.platform, args.chat)
    elif args.cmd == "token":
        print(core.projects.issue_token(args.project, args.label, args.role, args.scopes))
    elif args.cmd == "sender":
        core.outreach.add_sender(args.project, args.label, args.channel, json.loads(args.config), args.cap)
    elif args.cmd == "scraper":
        core.scrapers.register(args.project, args.name, args.kind, json.loads(args.spec), args.every)
    elif args.cmd == "ask":
        print(core.agent_chat(args.project, args.text, session="cli", actor=ADMIN))
    elif args.cmd == "do":
        print(_j(core.facade.call(args.project, ADMIN, args.action, json.loads(args.args))))
    elif args.cmd == "tick":
        core.tick()
    elif args.cmd == "serve":
        import uvicorn
        from .api import create_app
        if args.start_agents:
            for p in core.projects.list():
                core.projects.start(p["id"])
        uvicorn.run(create_app(core), host=args.host or core.s.host, port=args.port or core.s.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
