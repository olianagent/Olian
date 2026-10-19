from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of an LLM reply (handles ```json fences and chatter)."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i = t.find(opener)
        j = t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON in agent reply")


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")
