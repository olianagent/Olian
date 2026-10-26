"""`olian` command line: manage projects, people, senders, scrapers; talk to agents; run the server."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

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
    t.add_argument("--replace", action="store_true", help="invalidate older tokens with the same label (rotation)")
    tr = sub.add_parser("token-revoke", help="invalidate all tokens with a label"); tr.add_argument("project"); tr.add_argument("label")
    tl = sub.add_parser("tokens", help="list token labels (never the secrets)"); tl.add_argument("project")
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
    sub.add_parser("setup", help="generate .env with fresh secrets (admin key, webhook secret, PII salt, backup key)")
    mo = sub.add_parser("monitor", help="Telegram monitoring helpers")
    mos = mo.add_subparsers(dest="msub", required=True)
    mi = mos.add_parser("init", help="configure monitoring for a project and create a starter rule")
    mi.add_argument("project"); mi.add_argument("--bot-token", default="", help="token of a DEDICATED monitoring bot (not the chat bot)")
    mi.add_argument("--bot-token-env", default="", help="read the token from this environment variable instead")
    mi.add_argument("--profile", default="", help="who counts as a good lead, in a sentence")
    mi.add_argument("--topics", required=True, help="comma-separated topic phrases, e.g. 'вілл*,глемпінг*,villa*'")
    mi.add_argument("--category", default="general"); mi.add_argument("--langs", default="uk,ru,en")
    mi.add_argument("--mode", default="rules", choices=["rules", "hybrid", "llm"])
    mi.add_argument("--author", default="hash", choices=["hash", "raw", "none"])
    me_ = mos.add_parser("evaluate", help="precision/recall on a labelled JSON sample [{text, expected}]")
    me_.add_argument("project"); me_.add_argument("samples")
    tg = sub.add_parser("tg", help="Telegram user accounts that send private messages")
    tgs = tg.add_subparsers(dest="tgsub", required=True)
    tl_ = tgs.add_parser("login", help="connect an account: phone number + code once, then no phone is needed")
    tl_.add_argument("project"); tl_.add_argument("label"); tl_.add_argument("--phone", required=True, help="+380XXXXXXXXX")
    tl_.add_argument("--api-id", type=int, required=True, help="from my.telegram.org (for this account)")
    tl_.add_argument("--api-hash", default="", help="or set TG_API_HASH / be prompted (keeps it out of shell history)")
    tl_.add_argument("--cap", type=int, default=10, help="messages per day once the account is past its ramp-up")
    tgs.add_parser("list").add_argument("project")
    tr_ = tgs.add_parser("remove"); tr_.add_argument("project"); tr_.add_argument("label")
    bk = sub.add_parser("backup", help="consistent, encrypted snapshot of the database")
    bk.add_argument("--out", default=""); bk.add_argument("--plain", action="store_true", help="allow an UNENCRYPTED backup")
    rs = sub.add_parser("restore", help="restore a backup into a (new) data directory")
    rs.add_argument("file"); rs.add_argument("--to", required=True)

    args = ap.parse_args(argv)
    if args.cmd == "setup":
        return setup_env()
    if args.cmd == "restore":
        return restore_backup(args.file, args.to)
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
        print(core.projects.issue_token(args.project, args.label, args.role, args.scopes, replace=args.replace))
    elif args.cmd == "token-revoke":
        print(f"revoked {core.projects.revoke_token(args.project, args.label)} token(s)")
    elif args.cmd == "tokens":
        for t in core.projects.list_tokens(args.project):
            print(f"{t['label']:<20} {t['role']:<8} scopes={t['scopes'] or '*'}")
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
    elif args.cmd == "monitor":
        if args.msub == "init":
            return monitor_init(core, args)
        res = core.monitor.evaluate(args.project, json.load(open(args.samples)))
        print(_j({k: v for k, v in res.items() if k != "mismatches"}))
        for m in res["mismatches"][:20]:
            print(f"  {'FN' if m['expected'] else 'FP'} {m['text'][:90]!r} -> {m['why'][:90]}")
    elif args.cmd == "tg":
        return tg_command(core, args)
    elif args.cmd == "backup":
        return backup(core, args.out, args.plain)
    elif args.cmd == "serve":
        import uvicorn
        from .api import create_app
        if args.start_agents:
            for p in core.projects.list():
                core.projects.start(p["id"])
        uvicorn.run(create_app(core), host=args.host or core.s.host, port=args.port or core.s.port)
    return 0


def tg_command(core: App, a) -> int:
    import getpass
    if a.tgsub == "list":
        for r in core.tguser.list(a.project):
            print(f"{r['label']:<14} {r['status']:<10} {r['phone_hint']:<16} @{r['me'].get('username', '') or '—'} {r['status_reason']}")
        return 0
    if a.tgsub == "remove":
        core.tguser.remove(a.project, a.label)
        print("account logged out at Telegram and deleted here")
        return 0
    api_hash = a.api_hash or os.environ.get("TG_API_HASH") or getpass.getpass("api_hash (hidden): ")
    try:
        r = core.tguser.login_start(a.project, a.label, a.phone, a.api_id, api_hash)
    except (ValueError, RuntimeError) as e:
        print(f"cannot start login: {e}")
        return 1
    print(f"Telegram sent a login code to {r['phone']} (SMS or the Telegram app that is already logged in).")
    password = None
    for _ in range(3):
        code = getpass.getpass("code (hidden): ")
        try:
            out = core.tguser.login_code(a.project, a.label, code, password, a.cap)
            break
        except ValueError as e:
            if "two-step" in str(e) and not password:
                password = getpass.getpass("two-step verification password (hidden): ")
                try:
                    out = core.tguser.login_code(a.project, a.label, code, password, a.cap)
                    break
                except ValueError as e2:
                    print(e2)
                    continue
            print(e)
    else:
        print("login not completed")
        return 1
    print(f"connected as @{out['me'].get('username') or out['me'].get('first_name')}; sender '{out['sender']}' created "
          f"(new accounts start with a low daily cap and ramp up; see outreach.ramp)")
    return 0


def setup_env() -> int:
    import secrets
    from cryptography.fernet import Fernet
    path = Path(".env")
    if path.exists():
        print(".env already exists; refusing to overwrite. Delete it first if you really want new secrets.")
        return 1
    lines = [f"OLIAN_API_KEY={secrets.token_urlsafe(32)}", f"OLIAN_WEBHOOK_SECRET={secrets.token_urlsafe(32)}",
             f"OLIAN_PII_SALT={secrets.token_hex(32)}", f"OLIAN_BACKUP_KEY={Fernet.generate_key().decode()}",
             f"OLIAN_SESSION_KEY={Fernet.generate_key().decode()}",
             "OLIAN_DATA_DIR=./data", "OLIAN_PORT_BASE=8700", "OLIAN_DEFAULT_PROVIDER=", "OLIAN_DEFAULT_MODEL=",
             "WHATSAPP_CLOUD_TOKEN=", "WHATSAPP_VERIFY_TOKEN=", "WHATSAPP_APP_SECRET=", "OLIAN_NOTIFY_TELEGRAM_TOKEN="]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    print("wrote .env (0600) with fresh secrets. KEEP A COPY OF OLIAN_BACKUP_KEY, OLIAN_SESSION_KEY AND OLIAN_PII_SALT somewhere safe:\n"
          "without the backup key encrypted backups cannot be opened; without the session key stored Telegram sessions are "
          "unreadable (accounts must be logged in again); losing the PII salt makes stored pseudonyms unlinkable.")
    return 0


def monitor_init(core: App, a) -> int:
    from .presets import EXCLUDE, intent_phrases
    mon = {"enabled": True, "mode": a.mode, "author": a.author, "profile": a.profile}
    if a.bot_token_env:
        mon["bot_token_env"] = a.bot_token_env
    elif a.bot_token:
        mon["bot_token"] = a.bot_token
    core.projects.update_config(a.project, {"monitor": mon})
    topics = [t.strip() for t in a.topics.split(",") if t.strip()]
    rule = core.monitor.save_rule(a.project, {"name": f"{a.category}: topic + intent", "category": a.category, "phrases": topics,
                                              "require_any": intent_phrases(a.langs.split(",")), "exclude": EXCLUDE})
    print(f"monitoring enabled for {a.project}; starter rule #{rule['id']} created ({len(topics)} topic phrases, "
          f"{len(rule['require_any'])} intent phrases). Next: add the bot to approved chats, then approve them as sources "
          f"(panel /admin -> Джерела, or `olian do {a.project} monitor.sources.add '{{\"chat_ref\":\"-100…\"}}'`).")
    return 0


def backup(core: App, out: str, plain: bool) -> int:
    import io, sqlite3, tarfile, tempfile, time as _t
    key = os.environ.get("OLIAN_BACKUP_KEY", "")
    if not key and not plain:
        print("OLIAN_BACKUP_KEY is not set; refusing to write an unencrypted backup (use `olian setup`, or --plain).")
        return 1
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "olian.db"
        dst = sqlite3.connect(snap)
        core.db._conn.backup(dst)  # consistent even while the service is writing
        dst.close()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(snap, arcname="olian.db")
            salt = core.s.data_dir / ".pii_salt"
            if salt.exists():
                tar.add(salt, arcname=".pii_salt")
        data = buf.getvalue()
    if key:
        from cryptography.fernet import Fernet
        data = Fernet(key.encode()).encrypt(data)
    name = out or f"backup-{_t.strftime('%Y%m%d-%H%M%S')}.tar.gz" + (".enc" if key else "")
    Path(name).write_bytes(data)
    Path(name).chmod(0o600)
    print(f"wrote {name} ({len(data)} bytes, {'encrypted' if key else 'PLAIN'})")
    return 0


def restore_backup(file: str, to: str) -> int:
    import io, tarfile
    data = Path(file).read_bytes()
    if file.endswith(".enc"):
        key = os.environ.get("OLIAN_BACKUP_KEY", "")
        if not key:
            print("OLIAN_BACKUP_KEY is required to open an encrypted backup")
            return 1
        from cryptography.fernet import Fernet, InvalidToken
        try:
            data = Fernet(key.encode()).decrypt(data)
        except InvalidToken:
            print("wrong key or corrupted backup")
            return 1
    dest = Path(to)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.name not in ("olian.db", ".pii_salt"):
                print(f"unexpected member {m.name!r}; aborting")
                return 1
        tar.extractall(dest, filter="data")
    (dest / ".pii_salt").exists() and (dest / ".pii_salt").chmod(0o600)
    print(f"restored into {dest} (set OLIAN_DATA_DIR={dest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
