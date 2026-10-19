import json


def say(core, text, user="2", chat="777", platform="telegram"):
    return core.chat.handle(platform, chat, user, text)


def test_stranger_gets_no_info(core, proj):
    r = say(core, "/status", user="999")
    assert "Немає доступу" in r and "Alpha" not in r


def test_commands_follow_roles(core, proj):
    assert "Alpha Villas" in say(core, "/status", user="3")                  # viewer may look
    assert "⛔" in say(core, "/task buy tea", user="3")                       # but not change
    assert "Завдання #1" in say(core, "/task buy tea", user="2")             # manager may
    assert "⛔" in say(core, "/audit", user="2")                              # admin only
    assert "task" in say(core, "/audit", user="1") or "tasks.add" in say(core, "/audit", user="1")


def test_todo_assigns_to_me_and_done(core, proj):
    assert "human" in say(core, "/todo call the notary", user="2")
    assert "call the notary" in say(core, "/tasks", user="3")
    assert "виконано" in say(core, "/done 1 signed", user="2")
    assert "Відкритих завдань немає" in say(core, "/tasks", user="3")


def test_free_text_goes_to_agent_as_this_human(core, proj, agent):
    agent.script = ["Three villas are free."]
    assert say(core, "which villas are free?", user="3") == "Three villas are free."
    assert agent.calls[0][0] == "chat-telegram:3"                          # per-person session


def test_approve_flow_from_chat(core, proj, agent):
    core.outreach.add_sender(proj, "d", "dryrun")
    l = core.outreach.add_lead(proj, "whatsapp", "420111", name="Jan")
    m = core.outreach.compose(proj, l["id"], "hello Jan")
    assert "hello Jan" in say(core, "/outbox", user="2")
    assert "⛔" in say(core, "/approve %d" % m["id"], user="3")
    assert "схвалено" in say(core, "/approve %d" % m["id"], user="2")
    assert core.outreach.message(proj, m["id"])["status"] == "approved"


def test_dm_with_several_projects_needs_prefix(core, proj):
    core.projects.create("beta", "Beta Co")
    core.projects.add_member("beta", "telegram", "2", "manager")
    assert "Немає доступу" in say(core, "/status", user="2", chat="555")
    assert "Beta Co" in say(core, "#beta /status", user="2", chat="555")
    assert "Немає доступу" in say(core, "#beta /status", user="3", chat="555")   # vic is not in beta


def test_bad_input_is_a_message_not_a_crash(core, proj):
    assert "⚠️" in say(core, "/done abc", user="2") or "Невідома" in say(core, "/done abc", user="2")
    assert "Невідома" in say(core, "/nonsense", user="2")


def test_telegram_bridge_routes_updates(core, proj, http):
    from olian.bridge import TelegramBridge
    from olian.channels import Telegram
    TelegramBridge(core, http).handle_update(proj, Telegram("T", http), {"update_id": 1, "message": {
        "chat": {"id": 777}, "from": {"id": 2, "username": "mia"}, "text": "/status"}})
    sent = http.sent_to("botT/sendMessage")[-1]["json"]
    assert sent["chat_id"] == "777" and "Alpha Villas" in sent["text"]
