import json

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Looking for glamping in Bali</title><link>http://x/1</link><description>need a place for 5</description></item>
<item><title>Cooking tips</title><link>http://x/2</link><description>pasta</description></item></channel></rss>"""


def test_rss_scraper_dedupes_and_emits_events(core, proj, http):
    http.pages["http://feed"] = RSS
    core.scrapers.register(proj, "news", "rss", {"url": "http://feed"}, 10)
    assert core.scrapers.run(proj, "news")["new_items"] == 2
    assert core.scrapers.run(proj, "news")["new_items"] == 0                # nothing new the second time
    ev = core.signals.events(proj)
    assert len(ev) == 2 and ev[0]["source"] == "scraper:news"


def test_scraper_dedupe_memory_is_per_project(core, proj, http):
    core.projects.create("beta", "Beta")
    http.pages["http://feed"] = RSS
    for p in (proj, "beta"):
        core.scrapers.register(p, "news", "rss", {"url": "http://feed"})
        assert core.scrapers.run(p, "news")["new_items"] == 2


def test_failing_scraper_reports_an_error_event_and_does_not_raise(core, proj, http):
    core.scrapers.register(proj, "dead", "http", {"url": "http://nope"})
    assert "error" in core.scrapers.run(proj, "dead")
    assert core.signals.events(proj)[0]["kind"] == "error"


def test_command_scraper_runs_in_docker_by_default(core, proj):
    calls = []
    core.scrapers.runner = lambda argv, timeout, cwd: (calls.append(argv) or
                                                        '{"text":"a lead"}\nnoise\n{"text":"b"}\n')
    core.scrapers.register(proj, "tg", "command", {"image": "scraper:1", "cmd": ["python", "run.py"]})
    assert core.scrapers.run(proj, "tg")["new_items"] == 2
    argv = calls[0]
    assert argv[:3] == ["docker", "run", "--rm"] and "--memory" in argv and "scraper:1" in argv and "--network" not in argv


def test_command_scraper_requires_sandbox_unless_explicit(core, proj):
    import pytest
    with pytest.raises(ValueError):
        core.scrapers.register(proj, "x", "command", {"cmd": ["rm"]})


def test_due_scrapers_only(core, proj, http):
    http.pages["http://feed"] = RSS
    core.scrapers.register(proj, "n", "rss", {"url": "http://feed"}, 10)
    assert len(core.scrapers.run_due(proj, now=10**10)) == 1
    assert core.scrapers.run_due(proj, now=10**10 + 5) == []


def test_rule_turns_message_into_lead_but_never_auto_contacts(core, proj, agent, http):
    core.projects.update_config(proj, {"signals": {"rules": [
        {"name": "bali-stay", "keywords": ["glamping", "villa"], "exclude": ["sell"], "action": "lead",
         "channel": "telegram", "address_field": "user.username", "name_field": "user.name", "ai_qualify": True,
         "auto_draft": True}]}})
    core.signals.ingest(proj, "monitor:tg", "message", {"text": "Any glamping in Bali for 5?", "user": {"username": "@ann", "name": "Ann"}})
    core.signals.ingest(proj, "monitor:tg", "message", {"text": "I want to sell my villa", "user": {"username": "@bob"}})
    core.signals.ingest(proj, "monitor:tg", "message", {"text": "weather is nice", "user": {"username": "@cat"}})
    agent.script = [json.dumps({"qualified": True, "reason": "asks for stay"})]
    stats = core.signals.process(proj)
    assert stats["events"] == 3 and stats["leads"] == 1
    ann = core.outreach.lead_by_addr(proj, "telegram", "@ann")
    assert ann["name"] == "Ann" and ann["source"] == "signal:bali-stay" and ann["status"] == "new"
    assert core.outreach.outbox(proj) == []                                  # even with auto_draft: no draft, no DM (CON-1)
    assert http.sent_to("api.telegram.org")                                  # but the operators are told
    assert core.outreach.lead_by_addr(proj, "telegram", "@bob") is None     # excluded
    assert core.signals.process(proj)["events"] == 0                        # processed once only
    with __import__("pytest").raises(ValueError, match="legal basis"):
        core.outreach.compose(proj, ann["id"], "hello")
    core.outreach.set_consent(proj, ann["id"], "Ann asked in the group to be contacted by DM")
    assert core.outreach.compose(proj, ann["id"], "hello Ann")["status"] == "draft"


def test_ai_qualify_rejection_and_agent_outage_retries(core, proj, agent):
    from olian.hermes import HermesError
    core.projects.update_config(proj, {"signals": {"rules": [
        {"name": "r", "keywords": ["villa"], "action": "lead", "ai_qualify": True, "address_field": "user.username"}]}})
    core.signals.ingest(proj, "m", "message", {"text": "villa?", "user": {"username": "@q"}})
    agent.script = [lambda m: (_ for _ in ()).throw(HermesError("down"))]
    assert core.signals.process(proj)["events"] == 0                       # left for the next tick
    agent.script = [json.dumps({"qualified": False, "reason": "spam"})]
    assert core.signals.process(proj) == {"events": 1, "leads": 0, "tasks": 0, "notified": 0}


def test_task_and_notify_rules(core, proj, http):
    core.projects.update_config(proj, {"signals": {"rules": [
        {"name": "complaint", "keywords": ["broken"], "action": "task", "assignee": "mia"},
        {"name": "vip", "keywords": ["vip"], "action": "notify"}]}})
    core.signals.ingest(proj, "chat", "message", {"text": "the sauna is broken"})
    core.signals.ingest(proj, "chat", "message", {"text": "VIP guest arriving"})
    s = core.signals.process(proj)
    assert s["tasks"] == 1 and s["notified"] >= 1
    assert core.tasks.list(proj, executor="human")[0]["assignee"] == "mia"
