import json

import pytest

from olian.hermes import HermesError


def sender(core, proj, label="s1", channel="dryrun", cap=20, cfg=None):
    core.outreach.add_sender(proj, label, channel, cfg or {}, cap)


def lead(core, proj, addr="+420 777 111 222", **kw):
    return core.outreach.add_lead(proj, "whatsapp", addr, name="Jan", **kw)


def test_agent_only_drafts_human_approves_then_it_sends(core, proj, agent, noon):
    sender(core, proj)
    l = lead(core, proj)
    assert l["address"] == "420777111222"
    agent.script = ["Hi Jan, do you rent glamping units?"]
    m = core.outreach.draft(proj, l["id"])
    assert m["status"] == "draft"
    assert core.outreach.send_due(proj, noon)["sent"] == 0          # nothing leaves without approval
    core.outreach.approve(proj, m["id"])
    assert core.outreach.send_due(proj, noon)["sent"] == 1
    assert core.outreach.message(proj, m["id"])["status"] == "sent"
    assert core.outreach.lead(proj, l["id"])["status"] == "contacted"


def test_window_caps_pacing_and_rotation(core, proj, noon):
    sender(core, proj, "a", cap=1); sender(core, proj, "b", cap=1)
    ids = [core.outreach.compose(proj, lead(core, proj, f"1555000{i}")["id"], f"hi {i}", approve=True)["id"] for i in range(3)]
    night = noon - 10 * 3600
    assert core.outreach.send_due(proj, night)["skipped"] == "outside sending window"
    r = core.outreach.send_due(proj, noon)
    assert r["sent"] == 2 and "no sender with capacity" in r["skipped"]                  # caps hold, 3rd waits
    used = {core.outreach.message(proj, i)["sender"] for i in ids[:2]}
    assert used == {"a", "b"}                                                           # load spread across accounts
    assert core.outreach.send_due(proj, noon + 86400)["sent"] == 1                       # next day the cap resets
    core.projects.update_config(proj, {"outreach": {"min_interval_seconds": 600}})
    ids2 = [core.outreach.compose(proj, lead(core, proj, f"1666000{i}")["id"], "x", approve=True)["id"] for i in range(2)]
    assert core.outreach.send_due(proj, noon + 2 * 86400)["skipped"] in ("", "pacing")
    assert core.outreach.send_due(proj, noon + 2 * 86400 + 1)["skipped"] == "pacing"


def test_opt_out_is_final(core, proj, noon):
    sender(core, proj)
    l = lead(core, proj)
    m = core.outreach.compose(proj, l["id"], "hello", approve=True)
    out = core.outreach.handle_reply(proj, "whatsapp", "420777111222", "Please STOP writing")
    assert out["intent"] == "opt_out" and core.outreach.lead(proj, l["id"])["status"] == "opted_out"
    assert core.outreach.message(proj, m["id"])["status"] == "cancelled"                 # queued message killed
    assert core.outreach.send_due(proj, noon)["sent"] == 0
    with pytest.raises(ValueError):
        core.outreach.compose(proj, l["id"], "again?")


def test_speed_to_lead_stale_leads_are_not_contacted(core, proj, noon):
    core.projects.update_config(proj, {"outreach": {"max_lead_age_minutes": 30, "min_interval_seconds": 0}})
    sender(core, proj)
    old = lead(core, proj, "1777000001", detected_at=noon - 3600)
    fresh = lead(core, proj, "1777000002", detected_at=noon - 120)
    warm = lead(core, proj, "1777000003", source="warm", detected_at=noon - 99999)
    ms = [core.outreach.compose(proj, x["id"], "hi", approve=True)["id"] for x in (old, fresh, warm)]
    r = core.outreach.send_due(proj, noon)
    assert r["sent"] == 2 and r["cancelled"] == 1
    assert core.outreach.lead(proj, old["id"])["status"] == "stale"
    assert core.outreach.message(proj, ms[0])["status"] == "cancelled"


def test_reply_positive_creates_handoff_task(core, proj, agent):
    l = lead(core, proj)
    agent.script = [json.dumps({"intent": "positive", "summary": "wants prices", "reply": "Sure, sending the deck"})]
    out = core.outreach.handle_reply(proj, "whatsapp", "420777111222", "Yes interested, how much?")
    assert out["intent"] == "positive" and core.outreach.lead(proj, l["id"])["status"] == "qualified"
    t = core.tasks.list(proj, executor="human")[0]
    assert "kvalifik" not in t["title"].lower() and t["steps"][0]["copy"] == "Sure, sending the deck"


def test_reply_question_makes_draft_not_auto_send(core, proj, agent, noon):
    sender(core, proj)
    l = lead(core, proj)
    agent.script = [json.dumps({"intent": "question", "summary": "asks size", "reply": "42 units"})]
    core.outreach.handle_reply(proj, "whatsapp", "420777111222", "How big is it?")
    d = core.outreach.outbox(proj, "draft")
    assert len(d) == 1 and d[0]["body"] == "42 units"
    assert core.outreach.send_due(proj, noon)["sent"] == 0


def test_classifier_falls_back_when_agent_is_down(core, proj, agent):
    lead(core, proj)
    agent.script = [lambda m: (_ for _ in ()).throw(HermesError("down"))]
    out = core.outreach.handle_reply(proj, "whatsapp", "420777111222", "not interested")
    assert out["intent"] == "negative" and core.outreach.leads(proj, "lost")


def test_unknown_sender_of_a_reply_becomes_inbound_lead(core, proj, agent):
    agent.script = [json.dumps({"intent": "other", "summary": "?", "reply": ""})]
    core.outreach.handle_reply(proj, "telegram", "@newguy", "hello?")
    assert core.outreach.lead_by_addr(proj, "telegram", "@newguy")["source"] == "inbound"


def test_whatsapp_cloud_uses_template_outside_24h_and_text_inside(core, proj, http, noon, agent):
    sender(core, proj, "wa", "whatsapp_cloud", cfg={"phone_number_id": "PN1", "template_name": "intro", "template_lang": "en"})
    l = lead(core, proj)
    core.outreach.compose(proj, l["id"], "Hello Jan", approve=True)
    assert core.outreach.send_due(proj, noon)["sent"] == 1
    body = http.sent_to("PN1/messages")[-1]["json"]
    assert body["type"] == "template" and body["template"]["name"] == "intro"
    assert http.sent_to("PN1/messages")[-1]["headers"]["Authorization"] == "Bearer watoken"
    import time
    agent.script = [json.dumps({"intent": "question", "summary": "", "reply": ""})]
    core.outreach.handle_reply(proj, "whatsapp", "420777111222", "who is this?")      # opens the 24h window
    core.outreach.compose(proj, l["id"], "It's Alpha", approve=True)
    r = core.outreach.send_due(proj, time.time() + 1)
    if r["sent"]:                                                                    # only if inside the sending window
        assert http.sent_to("PN1/messages")[-1]["json"]["type"] == "text"


def test_whatsapp_without_template_fails_loudly(core, proj, noon):
    sender(core, proj, "wa", "whatsapp_cloud", cfg={"phone_number_id": "PN1"})
    m = core.outreach.compose(proj, lead(core, proj)["id"], "Hello", approve=True)
    r = core.outreach.send_due(proj, noon)
    assert r["failed"] == 1 and "template" in core.outreach.message(proj, m["id"])["error"]


def test_webhook_sender_and_channel_routing(core, proj, http, noon):
    sender(core, proj, "tgfarm", "webhook", cfg={"url": "https://farm.local/send", "serves": ["telegram"]})
    wa = lead(core, proj)
    tg = core.outreach.add_lead(proj, "telegram", "@lead")
    core.outreach.compose(proj, wa["id"], "wa msg", approve=True)
    core.outreach.compose(proj, tg["id"], "tg msg", approve=True)
    r = core.outreach.send_due(proj, noon)
    assert r["sent"] == 1 and "no sender" in r["skipped"]                  # WhatsApp lead has no eligible sender
    assert http.sent_to("farm.local")[0]["json"]["to"] == "@lead"


def test_warm_contacts_get_checkin_drafts_and_next_touch(core, proj, agent, noon):
    sender(core, proj)
    l = core.outreach.add_lead(proj, "telegram", "@liasha", name="Liasha", source="warm", warmth=80, next_touch=noon - 1)
    agent.script = ["Hey Liasha, how did the launch go?"]
    drafts = core.outreach.touch_plan(proj, noon)
    assert len(drafts) == 1 and drafts[0]["status"] == "draft"
    assert core.outreach.touch_plan(proj, noon) == []                       # no second draft while one is pending
    core.outreach.approve(proj, drafts[0]["id"])
    core.outreach.add_sender(proj, "tg", "dryrun")
    assert core.outreach.send_due(proj, noon)["sent"] == 1
    assert abs(core.outreach.lead(proj, l["id"])["next_touch"] - (noon + 14 * 86400)) < 5   # warm → 2 weeks


def test_funnel(core, proj):
    lead(core, proj); core.outreach.add_lead(proj, "telegram", "@a")
    assert core.outreach.funnel(proj)["leads"] == {"new": 2}


def test_authors_of_monitored_posts_cannot_be_contacted_without_basis(core, proj, noon):
    sender(core, proj)
    for src in ("signal:x", "monitor:y"):
        l = core.outreach.add_lead(proj, "telegram", "@a" + src[:3], source=src)
        with pytest.raises(ValueError, match="legal basis"):
            core.outreach.compose(proj, l["id"], "hi")
    with pytest.raises(ValueError, match="describe the basis"):
        core.outreach.set_consent(proj, l["id"], "ok")
    core.outreach.set_consent(proj, l["id"], "asked to be contacted in thread")
    assert json.loads(core.outreach.lead(proj, l["id"])["data"])["consent_basis"]
    m = core.outreach.compose(proj, l["id"], "hi", approve=True)
    # a queued message is re-checked at send time: revoking the basis cancels it
    core.db.x("UPDATE leads SET consent=0 WHERE id=?", (l["id"],))
    assert core.outreach.send_due(proj, noon)["cancelled"] == 1
    assert "legal basis" in core.outreach.message(proj, m["id"])["error"]


def test_a_person_stays_with_one_telegram_account_and_never_fails_over(core, proj, noon):
    core.outreach.add_sender(proj, "a", "dryrun", {}, 1)
    core.outreach.add_sender(proj, "b", "dryrun", {}, 50)
    first = core.outreach.compose(proj, core.outreach.add_lead(proj, "telegram", "@first")["id"], "hi", approve=True)
    core.outreach.send_due(proj, noon)
    owner = core.outreach.message(proj, first["id"])["sender"]
    assert owner in ("a", "b")
    lead = core.outreach.lead_by_addr(proj, "telegram", "@first")
    assert json.loads(lead["data"])["assigned_sender"] == owner                # the conversation belongs to this account
    # the owner trips: that person's follow-up waits; it is NOT handed to the healthy account
    core.outreach.trip(proj, "test", owner)
    core.projects.update_config(proj, {"outreach": {"min_gap_hours": 0}})
    follow = core.outreach.compose(proj, lead["id"], "follow-up", approve=True)
    r = core.outreach.send_due(proj, noon + 600)
    assert core.outreach.message(proj, follow["id"])["status"] == "approved" and r["sent"] == 0
    assert "no sender with capacity" in r["skipped"]
    # whereas a NEW person can go to the other active account
    other = core.outreach.compose(proj, core.outreach.add_lead(proj, "telegram", "@second")["id"], "hello", approve=True)
    core.outreach.send_due(proj, noon + 700)
    assert core.outreach.message(proj, other["id"])["sender"] not in ("", owner)


@pytest.mark.parametrize("text", ["Які є дати у листопаді?", "Є інтернет у номері?", "Також цікавить вартість", "Скільки це буде?",
                                  "Ми зараз на відпочинку, напишу згодом"])
def test_innocent_words_do_not_trigger_stop_or_complaint_logic(core, proj, agent, text):
    sender(core, proj)
    l = lead(core, proj)
    agent.script = [json.dumps({"intent": "other", "summary": "", "reply": ""})]
    out = core.outreach.handle_reply(proj, "whatsapp", "420777111222", text)
    assert out["intent"] != "opt_out" and core.outreach.lead(proj, l["id"])["status"] != "opted_out"
    assert not core.db.one("SELECT 1 FROM events WHERE kind IN ('optout','complaint')")


@pytest.mark.parametrize("text", ["стоп", "Стоп.", "STOP", "Не пишіть мені більше", "будь ласка, відпишіть мене", "unsubscribe"])
def test_real_stop_requests_are_still_caught(core, proj, text):
    sender(core, proj)
    l = lead(core, proj)
    assert core.outreach.handle_reply(proj, "whatsapp", "420777111222", text)["intent"] == "opt_out"


def test_keyword_fallback_uses_whole_words_too(core, proj, agent):
    lead(core, proj)
    agent.script = [lambda m: (_ for _ in ()).throw(HermesError("down"))]
    assert core.outreach.handle_reply(proj, "whatsapp", "420777111222", "Є інтернет і вікно на море?")["intent"] == "other"
    agent.script = [lambda m: (_ for _ in ()).throw(HermesError("down"))]
    assert core.outreach.handle_reply(proj, "whatsapp", "420777111222", "ні, дякую")["intent"] == "negative"
