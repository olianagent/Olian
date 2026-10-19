import json

import pytest

from olian.facade import ActionError, Actor
from olian.rbac import Forbidden, Role

ADMIN = Actor("admin", Role.ADMIN)


def audit(core, pid):
    return core.db.q("SELECT * FROM audit WHERE project_id=? ORDER BY id", (pid,))


def test_role_gating_and_audit_of_denials(core, proj):
    viewer = Actor("vic", Role.VIEWER)
    assert core.facade.call(proj, viewer, "tasks.list") == []
    with pytest.raises(Forbidden):
        core.facade.call(proj, viewer, "tasks.add", {"title": "x"})
    rows = audit(core, proj)
    assert [r["ok"] for r in rows] == [1, 0] and "may not tasks.add" in rows[1]["result"]


def test_scoped_actor_reception_only(core, proj):
    olesya = Actor("olesya", Role.MANAGER, "leads.*,tasks.list")
    core.facade.call(proj, olesya, "leads.add", {"channel": "whatsapp", "address": "+1 555 0100"})
    with pytest.raises(Forbidden):
        core.facade.call(proj, olesya, "outreach.send_due")
    with pytest.raises(Forbidden):
        core.facade.call(proj, olesya, "tasks.add", {"title": "wire money"})


def test_args_validation_and_secret_redaction(core, proj):
    with pytest.raises(ActionError):
        core.facade.call(proj, ADMIN, "tasks.add", {})
    with pytest.raises(ActionError):
        core.facade.call(proj, ADMIN, "tasks.add", {"title": "x", "bogus": 1})
    with pytest.raises(ActionError):
        core.facade.call(proj, ADMIN, "nope")
    core.facade.call(proj, ADMIN, "outreach.add_sender", {"label": "wa", "channel": "dryrun", "config": {"token": "SECRET"}})
    assert "SECRET" not in json.dumps(audit(core, proj))  # nested secrets are redacted too
    assert "***" in audit(core, proj)[-1]["args"]
    core.facade.call(proj, ADMIN, "tasks.add", {"title": "x"})


def test_agent_cannot_call_agent_driving_actions(core, proj):
    agent = Actor("agent", Role.MANAGER)
    with pytest.raises(Forbidden):
        core.facade.call(proj, agent, "tasks.plan", {"text": "x"}, via_agent=True)
    names = {a.name for a in core.facade.visible(agent, proj, via_agent=True)}
    assert "tasks.add" in names and "tasks.plan" not in names and "audit.list" not in names


def test_agent_is_narrowed_to_the_human_it_acts_for(core, proj):
    agent = Actor("agent", Role.MANAGER)
    core.facade.call(proj, agent, "tasks.add", {"title": "autonomous ok"}, via_agent=True)  # system work
    with core.facade.acting(proj, Actor("vic", Role.VIEWER)):
        assert core.facade.call(proj, agent, "tasks.list", {}, via_agent=True)
        with pytest.raises(Forbidden):  # the agent must not do more than the viewer who is asking
            core.facade.call(proj, agent, "tasks.add", {"title": "sneaky"}, via_agent=True)
    with core.facade.acting(proj, Actor("olesya", Role.MANAGER, "leads.*")):
        with pytest.raises(Forbidden):
            core.facade.call(proj, agent, "tasks.add", {"title": "x"}, via_agent=True)
    assert core.facade.call(proj, agent, "tasks.add", {"title": "again ok"}, via_agent=True)


def test_projects_cannot_see_each_others_data(core, proj):
    core.projects.create("beta", "Beta")
    t = core.facade.call(proj, ADMIN, "tasks.add", {"title": "alpha secret"})
    assert core.facade.call("beta", ADMIN, "tasks.list") == []
    with pytest.raises(ValueError):
        core.facade.call("beta", ADMIN, "tasks.update", {"id": t["id"], "status": "done"})
    core.facade.call(proj, ADMIN, "leads.add", {"channel": "telegram", "address": "@x"})
    assert core.facade.call("beta", ADMIN, "leads.list") == []
