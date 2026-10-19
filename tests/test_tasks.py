import json

from olian.tasks import format_task

PLAN = json.dumps({"tasks": [
    {"title": "Scrape Bali travel groups", "executor": "scraper", "detail": "t.me groups"},
    {"title": "Create Meta business account", "executor": "human", "assignee": "mia",
     "steps": [{"text": "Open business.facebook.com", "link": "https://business.facebook.com"},
               {"text": "Paste company name", "copy": "Alpha Villas s.r.o."}]},
    {"title": "Draft first-touch templates", "executor": "agent"}]})


def test_plan_breaks_goal_into_tasks_and_pings_the_human(core, proj, agent, http):
    g = core.tasks.add_goal(proj, "100 qualified leads", "leads", 0)
    agent.script = ["Sure!\n```json\n" + PLAN + "\n```"]
    tasks = core.tasks.plan(proj, goal_id=g["id"])
    assert [t["executor"] for t in tasks] == ["scraper", "human", "agent"] and all(t["goal_id"] == g["id"] for t in tasks)
    human = tasks[1]
    assert human["steps"][1]["copy"] == "Alpha Villas s.r.o."
    sent = http.sent_to("api.telegram.org/botT/") + http.sent_to("api.telegram.org/bottgtoken")
    assert sent, "human task should notify managers"
    assert "https://business.facebook.com" in format_task(core.tasks.get(proj, human["id"]))


def test_unparsable_plan_is_an_error_not_garbage(core, proj, agent):
    agent.script = ["I could not do that"]
    try:
        core.tasks.plan(proj, text="x")
        assert False
    except ValueError as e:
        assert "parse" in str(e)
    assert core.tasks.list(proj) == []


def test_goal_linked_and_urgent_tasks_float_up(core, proj):
    early = core.tasks.add_goal(proj, "soon", deadline=1_900_000_000)
    late = core.tasks.add_goal(proj, "later", deadline=2_900_000_000)
    a = core.tasks.add(proj, "no goal")
    b = core.tasks.add(proj, "late goal", goal_id=late["id"])
    c = core.tasks.add(proj, "early goal", goal_id=early["id"])
    assert [t["id"] for t in core.tasks.list(proj)] == [c["id"], b["id"], a["id"]]


def test_sweep_done_needs_human_and_agent_down(core, proj, agent, http):
    t1 = core.tasks.add(proj, "Summarise competitors")
    t2 = core.tasks.add(proj, "Post to Instagram stories")
    agent.script = ["DONE: 3 competitors summarised",
                    json.dumps({"needs_human": True, "reason": "no Instagram access",
                                "steps": [{"text": "Open Instagram", "copy": "story text"}]})]
    done = core.tasks.sweep(proj)
    assert core.tasks.get(proj, t1["id"])["status"] == "done" and "3 competitors" in core.tasks.get(proj, t1["id"])["result"]
    t2n = core.tasks.get(proj, t2["id"])
    assert t2n["executor"] == "human" and t2n["status"] == "todo" and "no Instagram" in t2n["blocked_reason"]
    assert t2n["steps"][0]["copy"] == "story text"
    # agent unreachable: task goes back to todo instead of being lost
    from olian.hermes import HermesError
    t3 = core.tasks.add(proj, "Another")
    agent.script = [lambda m: (_ for _ in ()).throw(HermesError("down"))]
    core.tasks.sweep(proj)
    assert core.tasks.get(proj, t3["id"])["status"] == "todo"


def test_goal_must_belong_to_project(core, proj):
    core.projects.create("beta", "Beta")
    g = core.tasks.add_goal("beta", "other")
    try:
        core.tasks.add(proj, "x", goal_id=g["id"])
        assert False
    except ValueError:
        pass
