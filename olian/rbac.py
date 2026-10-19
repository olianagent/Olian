"""Roles and permissions. A user is bound to a project with a role; no binding = no access."""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    VIEWER = "viewer"


# action -> minimal role rank required
_RANK = {Role.VIEWER: 1, Role.MANAGER: 2, Role.ADMIN: 3}
PERMISSIONS = {
    "chat": Role.VIEWER,           # talk to the project agent (read-only questions)
    "tasks.read": Role.VIEWER,
    "tasks.write": Role.MANAGER,   # create / change tasks, run the controller
    "scrapers.run": Role.MANAGER,
    "outreach.approve": Role.MANAGER,
    "outreach.write": Role.MANAGER,
    "members.write": Role.ADMIN,
    "project.control": Role.ADMIN,  # start/stop agent, edit config
}


class Forbidden(Exception):
    pass


def parse_role(value: str) -> Role:
    try:
        return Role(value.lower())
    except ValueError as e:
        raise ValueError(f"unknown role {value!r}; use admin|manager|viewer") from e


def allowed(role: Role | str | None, action: str) -> bool:
    if role is None:
        return False
    need = PERMISSIONS[action]
    return _RANK[Role(role)] >= _RANK[need]


def require(role: Role | str | None, action: str) -> None:
    if not allowed(role, action):
        raise Forbidden(f"role {role!r} may not {action}")
