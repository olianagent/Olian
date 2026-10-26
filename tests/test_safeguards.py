"""Built-in ban/limit protections: stop early, never route around a limit (docs/TELEGRAM-LIMITS.md)."""
import json

import pytest

from olian.facade import Actor
from olian.rbac import Forbidden, Role


def sender(core, proj, label="s1", channel="webhook", cap=50, cfg=None):
    core.outreach.add_sender(proj, label, channel, cfg if cfg is not None else {"url": "https://farm.local/send"}, cap)


def lead(core, proj, addr, channel="telegram", **kw):
    return core.outreach.add_lead(proj, channel, addr, **kw)


def queue(core, proj, addr, body="hi", **kw):
    return core.outreach.compose(proj, lead(core, proj, addr, **kw)["id"], body, approve=True)


def sent_bodies(http):
    return [p["json"]["text"] for p in http.sent_to("farm.local")]


def test_platform_limit_signal_trips_only_that_sender_and_never_fails_over(core, proj, http, noon):
    sender(core, proj, "a", cfg={"url": "https://farm.local/send", "serves": ["whatsapp"]})
    sender(core, proj, "b", cfg={"url": "https://farm.local/send", "serves": ["whatsapp"]})
    for i in range(3):
        queue(core, proj, f"4201000{i}", channel="whatsapp")
    http.fail, http.fail_status, http.fail_text = True, 400, "PEER_FLOOD: too many requests"
    r = core.outreach.send_due(proj, noon)
    assert r["failed"] == 1 and r["skipped"] == "circuit breaker tripped"
    st = {x["label"]: x for x in core.outreach.senders(proj)}
    assert st["a"]["status"] == "tripped" and "PEER_FLOOD" in st["a"]["status_reason"] and st["b"]["status"] == "active"
    assert core.db.one("SELECT 1 FROM events WHERE kind='breaker'")
    assert len(http.sent_to("farm.local")) == 1                                    # exactly one attempt, nothing hammered
    http.fail = False
    core.outreach.send_due(proj, noon + 1)                                          # 'b' is still allowed to work
    assert [x["sender"] for x in core.db.q("SELECT sender FROM messages WHERE status='sent'")] == ["b", "b"]


def test_repeated_plain_failures_also_trip_the_breaker(core, proj, http, noon):
    sender(core, proj, "a")
    for i in range(5):
        queue(core, proj, f"@u{i}")
    http.fail, http.fail_status, http.fail_text = True, 502, "bad gateway"
    r = core.outreach.send_due(proj, noon)
    assert r["failed"] == 3 and r["skipped"] == "circuit breaker tripped"
    assert core.outreach.senders(proj)[0]["status"] == "tripped" and "3 consecutive" in core.outreach.senders(proj)[0]["status_reason"]
    assert core.outreach.send_due(proj, noon + 60)["sent"] == 0


def test_tripped_sender_needs_an_admin_to_come_back(core, proj, http, noon):
    sender(core, proj, "a")
    core.outreach.trip(proj, "manual test", "a")
    queue(core, proj, "@x")
    assert core.outreach.send_due(proj, noon)["sent"] == 0
    with pytest.raises(Forbidden):
        core.facade.call(proj, Actor("mia", Role.MANAGER), "outreach.reset_sender", {"label": "a"})
    core.facade.call(proj, Actor("boss", Role.ADMIN), "outreach.reset_sender", {"label": "a"})
    assert core.outreach.send_due(proj, noon + 60)["sent"] == 1


def test_complaints_trip_everything(core, proj, agent, http, noon):
    sender(core, proj, "a"); sender(core, proj, "b", cfg={"url": "https://farm.local/2"})
    for i in range(3):
        lead(core, proj, f"@c{i}")
        agent.script.append(json.dumps({"intent": "negative", "summary": "", "reply": ""}))
        core.outreach.handle_reply(proj, "telegram", f"@c{i}", "це спам, я поскаржусь")
    assert core.outreach.check_health(proj) and {x["status"] for x in core.outreach.senders(proj)} == {"tripped"}
    queue(core, proj, "@new")
    assert core.outreach.send_due(proj, noon)["skipped"] == "circuit breaker tripped"


def test_high_optout_rate_trips(core, proj, http, noon):
    sender(core, proj, "a")
    core.projects.update_config(proj, {"outreach": {"breaker": {"min_sent_for_rate": 4, "optout_rate": 0.5}}})
    for i in range(4):
        queue(core, proj, f"@r{i}")
    core.outreach.send_due(proj, noon)
    core.outreach.send_due(proj, noon + 60)
    core.outreach.send_due(proj, noon + 120)
    core.outreach.send_due(proj, noon + 180)
    for i in range(2):
        core.outreach.handle_reply(proj, "telegram", f"@r{i}", "stop")
    assert core.outreach.senders(proj)[0]["status"] == "tripped" and "opt-out rate" in core.outreach.senders(proj)[0]["status_reason"]


def test_per_person_limits_unless_they_reply(core, proj, http, agent, noon):
    core.projects.update_config(proj, {"outreach": {"min_gap_hours": 24, "max_per_lead": 2}})
    sender(core, proj, "a")
    l = lead(core, proj, "@p")
    t = noon
    for n in range(4):
        m = core.outreach.compose(proj, l["id"], f"msg{n}", approve=True)
        r = core.outreach.send_due(proj, t)
        if n == 0:
            assert r["sent"] == 1
        elif n == 1:
            assert r["sent"] == 0 and r["skipped"] == "min gap between messages to one person"      # too soon: held, not sent
            t += 25 * 3600
            assert core.outreach.send_due(proj, t)["sent"] == 1
        else:
            assert r["cancelled"] == 1 and "per-person limit" in core.outreach.message(proj, m["id"])["error"]
    # the person answering reopens the conversation: limits no longer apply to replies
    agent.script = [json.dumps({"intent": "question", "summary": "", "reply": ""})]
    core.outreach.handle_reply(proj, "telegram", "@p", "who is this?")
    m = core.outreach.compose(proj, l["id"], "we are Alpha", approve=True)
    assert core.outreach.send_due(proj, t + 60)["sent"] == 1


def test_project_daily_cap(core, proj, http, noon):
    core.projects.update_config(proj, {"outreach": {"daily_cap_total": 2}})
    sender(core, proj, "a")
    for i in range(4):
        queue(core, proj, f"@d{i}")
    r = core.outreach.send_due(proj, noon)
    assert r["sent"] == 2 and r["skipped"] == "project daily cap reached"
    assert core.outreach.send_due(proj, noon + 86400)["sent"] == 2


def test_optout_footer_only_on_first_contact(core, proj, http, noon):
    sender(core, proj, "a")
    core.projects.update_config(proj, {"outreach": {"min_gap_hours": 0}})
    l = lead(core, proj, "@f")
    core.outreach.compose(proj, l["id"], "first", approve=True); core.outreach.send_due(proj, noon)
    core.outreach.compose(proj, l["id"], "second", approve=True); core.outreach.send_due(proj, noon + 60)
    first, second = sent_bodies(http)
    assert first.startswith("first") and "відповідайте STOP" in first
    assert second == "second"
    stored = core.db.q("SELECT body FROM messages ORDER BY id")
    assert stored[0]["body"] == "first"                                            # footer is added at send time only


def test_stoplist_is_cross_channel_and_cancels_queued(core, proj, http, noon):
    sender(core, proj, "a")
    queued = queue(core, proj, "@same")
    core.outreach.stop(proj, "viber", "@same")                                  # same address on another channel
    assert core.outreach.send_due(proj, noon)["cancelled"] == 1
    assert "stop-list" in core.outreach.message(proj, queued["id"])["error"]
    again = core.outreach.add_lead(proj, "email", "@same")
    assert again["status"] == "opted_out"
    with pytest.raises(ValueError):
        core.outreach.compose(proj, again["id"], "hello again")
    assert core.outreach.stop(proj, "telegram", "@brandnew")["status"] == "opted_out"


def test_sender_secrets_are_masked(core, proj):
    core.outreach.add_sender(proj, "wa", "whatsapp_cloud", {"phone_number_id": "1", "token": "EAAsecret"}, 5)
    out = core.facade.call(proj, Actor("boss", Role.ADMIN), "outreach.senders")
    assert out[0]["config"]["token"] == "***" and "EAAsecret" not in json.dumps(out)
    with pytest.raises(Forbidden):
        core.facade.call(proj, Actor("mia", Role.MANAGER), "outreach.senders")


def test_no_evasion_features_exist_in_the_code_base():
    """Guard rail for future edits: the product must not grow account-rotation / fingerprint / proxy / warm-up logic."""
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[1] / "olian"
    banned = re.compile(r"(?i)(proxy_pool|rotate_proxy|residential|mobile_proxy|warm[_ ]?up|prогрів|initConnection|device_model|"
                        r"fingerprint_spoof|buy_session|session_marketplace|account_farm|rotate_account|account_rotation)")
    hits = [f"{f.name}: {m.group(0)}" for f in root.rglob("*.py") for m in banned.finditer(f.read_text())]
    assert not hits, hits
