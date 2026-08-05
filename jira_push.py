"""Push bugs to Jira Cloud (REST API v3) — ported from the old QA-Assistant repo.

JIRA_BASE_URL/JIRA_API_TOKEN/project not fully set in .env -> MOCK MODE:
issues are written to sessions/<id>/pushed_issues.json instead of sent for real.
Project key is adjustable at runtime (saved to jira_settings.json); secrets always come from .env.
"""
import json
import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
_FILE = ROOT / "jira_settings.json"
BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
EMAIL = os.getenv("JIRA_EMAIL", "")
TOKEN = os.getenv("JIRA_API_TOKEN", "")
LABEL = os.getenv("JIRA_LABEL", "qa-assistant")


def get_project() -> str:
    if _FILE.exists():
        key = json.loads(_FILE.read_text(encoding="utf-8")).get("project_key")
        if key:
            return key
    return os.getenv("JIRA_PROJECT_KEY", "")


def set_project(key: str):
    key = (key or "").strip()
    if not key:
        raise ValueError("Project key required")
    _FILE.write_text(json.dumps({"project_key": key}, indent=2), encoding="utf-8")


def enabled() -> bool:
    return bool(BASE_URL and TOKEN and get_project())


def public() -> dict:
    """For the UI — never leaks the token."""
    return {"base_url": BASE_URL, "email": EMAIL, "project_key": get_project(),
            "has_token": bool(TOKEN), "mode": "real" if enabled() else "mock"}


def list_projects() -> list[dict]:
    """Projects that accept the Bug issue type on the configured site."""
    if not (BASE_URL and EMAIL and TOKEN):
        raise ValueError("Fill in JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN in .env first")
    try:
        r = requests.get(f"{BASE_URL}/rest/api/3/project/search",
                         params={"expand": "issueTypes", "maxResults": 100, "action": "create"},
                         auth=(EMAIL, TOKEN), timeout=15)
    except requests.RequestException as e:
        raise ValueError(f"Could not connect to Jira: {e}")
    if r.status_code in (401, 403):
        raise ValueError("Wrong email/API token in .env")
    if not r.ok:
        raise ValueError(f"Jira returned HTTP {r.status_code}")
    return [{"key": p["key"], "name": p["name"]}
            for p in r.json().get("values", [])
            if any(t["name"] == "Bug" for t in p.get("issueTypes", []))]


# ---------------------------------------------------------------- payload ---
def _block(label: str, text: str) -> dict:
    return {"type": "paragraph", "content": [
        {"type": "text", "text": label, "marks": [{"type": "strong"}]},
        {"type": "hardBreak"},
        {"type": "text", "text": text},
    ]}


def _description_adf(bug: dict) -> dict:
    """Description in the studio's bug template: Time / Description / Actual / Expected."""
    t = bug.get("start_time", "") + (f" – {bug['end_time']}" if bug.get("end_time") else "")
    content = [_block("Time (in the attached video):", t)]
    for label, key in (("Description:", "description"),
                       ("Actual Result:", "actual_result"),
                       ("Expected Result:", "expected_result")):
        if bug.get(key):
            content.append(_block(label, bug[key]))
    return {"type": "doc", "version": 1, "content": content}


def _fields(bug: dict, project_key: str) -> dict:
    return {
        "project": {"key": project_key},
        "issuetype": {"name": "Bug"},
        "summary": bug.get("name", "Bug"),
        "description": _description_adf(bug),
        "labels": [LABEL] if LABEL else [],
    }


# ------------------------------------------------------------------- push ---
def push_bug(session_dir: Path, bug: dict, attachments: list[Path]) -> dict:
    """Create the issue + attach clip/screenshot. Returns {key, url, mock}."""
    project_key = get_project()
    if not enabled():
        return _push_mock(session_dir, bug, project_key or "MOCK")

    resp = requests.post(f"{BASE_URL}/rest/api/3/issue",
                         auth=(EMAIL, TOKEN),
                         json={"fields": _fields(bug, project_key)}, timeout=30)
    if not resp.ok:
        try:
            err = resp.json()
            msg = "; ".join(err.get("errorMessages", []) + list(err.get("errors", {}).values()))
        except Exception:
            msg = resp.text[:200]
        raise ValueError(f"Jira rejected (HTTP {resp.status_code}, project {project_key}): {msg}")
    key = resp.json()["key"]
    for path in attachments:  # best-effort: a failed attachment doesn't fail the push
        try:
            with open(path, "rb") as f:
                requests.post(f"{BASE_URL}/rest/api/3/issue/{key}/attachments",
                              auth=(EMAIL, TOKEN),
                              headers={"X-Atlassian-Token": "no-check"},
                              files={"file": (path.name, f)}, timeout=120,
                              ).raise_for_status()
        except Exception as e:
            print(f"[jira] attachment {path.name} failed: {e}")
    return {"key": key, "url": f"{BASE_URL}/browse/{key}", "mock": False}


def _push_mock(session_dir: Path, bug: dict, project_key: str) -> dict:
    out = session_dir / "pushed_issues.json"
    pushed = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
    key = f"MOCK-{len(pushed) + 1}"
    pushed.append({"key": key, "pushed_at": datetime.now().isoformat(),
                   "fields": _fields(bug, project_key)})
    out.write_text(json.dumps(pushed, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"key": key, "url": "", "mock": True}
