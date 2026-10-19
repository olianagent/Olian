"""Scrapers: small collectors that turn the outside world into events. They never talk to the agent
directly and never hold the project's tokens; the agent/rules decide what an item means.

Kinds: http (page or regex matches), rss, command (runs in a Docker sandbox), external (pushes via /ingest).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

import httpx

KINDS = ("http", "rss", "command", "external")


class ScrapeError(RuntimeError):
    pass


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def default_runner(argv: list[str], timeout: int, cwd: str | None = None) -> str:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if p.returncode != 0:
        raise ScrapeError(f"exit {p.returncode}: {p.stderr[-300:]}")
    return p.stdout


class Scrapers:
    def __init__(self, app, http: Any = httpx, runner: Callable = default_runner):
        self.app, self.db, self.http, self.runner = app, app.db, http, runner

    def register(self, pid: str, name: str, kind: str, spec: dict | None = None, interval_minutes: int = 0) -> None:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        spec = spec or {}
        if kind in ("http", "rss") and not spec.get("url"):
            raise ValueError("spec.url required")
        if kind == "command" and not (spec.get("cmd") and (spec.get("image") or spec.get("local"))):
            raise ValueError("command scraper needs spec.cmd and spec.image (or spec.local=true to opt out of the sandbox)")
        self.db.x("INSERT OR REPLACE INTO scrapers(project_id,name,kind,spec,interval_minutes) VALUES(?,?,?,?,?)",
                  (pid, name, kind, json.dumps(spec), interval_minutes))

    def list(self, pid: str) -> list[dict]:
        rows = self.db.q("SELECT * FROM scrapers WHERE project_id=? ORDER BY name", (pid,))
        for r in rows:
            r["spec"] = json.loads(r["spec"])
        return rows

    # ---- dedupe memory lives in the project's own directory (isolation)
    def _seen_path(self, pid: str, name: str) -> Path:
        d = self.app.s.projects_dir / pid / "scrapers"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{name}.seen"

    def _filter_new(self, pid: str, name: str, items: list[dict]) -> list[dict]:
        path = self._seen_path(pid, name)
        seen = set(path.read_text().split()) if path.exists() else set()
        fresh = []
        for it in items:
            key = _h(it.get("id") or it.get("link") or json.dumps(it, sort_keys=True, ensure_ascii=False))
            if key not in seen:
                seen.add(key)
                fresh.append(it)
        path.write_text("\n".join(list(seen)[-5000:]))
        return fresh

    def run(self, pid: str, name: str) -> dict:
        row = self.db.one("SELECT * FROM scrapers WHERE project_id=? AND name=?", (pid, name))
        if not row:
            raise ValueError(f"scraper {name!r} not found in project {pid}")
        spec = json.loads(row["spec"])
        if row["kind"] == "external":
            return {"scraper": name, "new_items": 0, "note": "external scrapers push via /ingest"}
        try:
            items = {"http": self._http, "rss": self._rss, "command": self._command}[row["kind"]](pid, spec)
        except (httpx.HTTPError, ScrapeError, ET.ParseError, subprocess.TimeoutExpired, OSError) as e:
            self.db.add_event(pid, f"scraper:{name}", "error", {"error": str(e)[:300]})
            self.db.x("UPDATE scrapers SET last_run=? WHERE project_id=? AND name=?", (time.time(), pid, name))
            return {"scraper": name, "new_items": 0, "error": str(e)[:300]}
        fresh = self._filter_new(pid, name, items)
        for it in fresh:
            self.app.signals.ingest(pid, f"scraper:{name}", "item", it)
        self.db.x("UPDATE scrapers SET last_run=? WHERE project_id=? AND name=?", (time.time(), pid, name))
        return {"scraper": name, "new_items": len(fresh)}

    def run_due(self, pid: str, now: float | None = None) -> list[dict]:
        now = now or time.time()
        out = []
        for s in self.list(pid):
            if s["interval_minutes"] and now - s["last_run"] >= s["interval_minutes"] * 60 and s["kind"] != "external":
                out.append(self.run(pid, s["name"]))
                self.db.x("UPDATE scrapers SET last_run=? WHERE project_id=? AND name=?", (now, pid, s["name"]))
        return out

    # ---- kinds
    def _http(self, pid: str, spec: dict) -> list[dict]:
        r = self.http.get(spec["url"], timeout=30, follow_redirects=True, headers={"User-Agent": "olian-scraper/0.1"})
        if r.status_code >= 400:
            raise ScrapeError(f"http {r.status_code}")
        body = r.text
        if spec.get("pattern"):
            return [{"text": m if isinstance(m, str) else " ".join(m), "url": spec["url"]}
                    for m in re.findall(spec["pattern"], body)]
        return [{"id": _h(body), "text": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))[:2000], "url": spec["url"]}]

    def _rss(self, pid: str, spec: dict) -> list[dict]:
        r = self.http.get(spec["url"], timeout=30, follow_redirects=True, headers={"User-Agent": "olian-scraper/0.1"})
        if r.status_code >= 400:
            raise ScrapeError(f"http {r.status_code}")
        root = ET.fromstring(r.text)
        items = []
        for it in root.iter():
            tag = it.tag.split("}")[-1]
            if tag not in ("item", "entry"):
                continue
            def get(n, it=it):
                return next((c for c in it if c.tag.split("}")[-1] == n), None)
            title, link = get("title"), get("link")
            desc = get("description") if get("description") is not None else get("summary")
            href = (link.get("href") if link is not None and link.get("href") else (link.text if link is not None else "")) or ""
            items.append({"title": (title.text or "").strip() if title is not None else "",
                          "text": re.sub(r"<[^>]+>", " ", (desc.text or "") if desc is not None else "").strip()[:1500],
                          "link": href.strip()})
        return items

    def _command(self, pid: str, spec: dict) -> list[dict]:
        work = self.app.s.projects_dir / pid / "work"
        work.mkdir(parents=True, exist_ok=True)
        timeout = int(spec.get("timeout", 300))
        if spec.get("local"):  # explicit opt-out of the sandbox, for trusted code only
            argv, cwd = list(spec["cmd"]), str(work)
        else:
            argv = ["docker", "run", "--rm", "--memory", spec.get("memory", "512m"), "--cpus", str(spec.get("cpus", 1)),
                    "--pids-limit", "256", "-v", f"{work}:/data", "-w", "/data"]
            for k, v in spec.get("env", {}).items():
                argv += ["-e", f"{k}={v}"]
            argv += [spec["image"], *spec["cmd"]]
            cwd = None
        out = self.runner(argv, timeout, cwd)
        items = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return items
