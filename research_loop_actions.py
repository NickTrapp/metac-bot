"""Restore the last durable loop ledger before a manual GitHub Actions run.

An absent or expired predecessor artifact blocks execution instead of starting
with an empty ledger. Only the known SQLite member is read from the archive.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import zipfile

from research_loop import LoopBlocked

WORKFLOW = "research_loop.yaml"
ARTIFACT = "research-loop-state"
REPOSITORY = "NickTrapp/metac-bot"
TARGET = Path(".work/research-loop.sqlite")


def gh_api(path: str, *, binary=False):
    data = subprocess.check_output(["gh", "api", f"repos/{REPOSITORY}/actions/" + path],
                                   stderr=subprocess.DEVNULL)
    if len(data) > 8 * 1024 * 1024:
        raise LoopBlocked("actions_response_too_large")
    return data if binary else json.loads(data)


def select_previous(history: dict, env: dict) -> dict | None:
    runs = history.get("workflow_runs")
    count = history.get("total_count")
    if not isinstance(runs, list) or type(count) is not int or count > 100 or count != len(runs):
        raise LoopBlocked("actions_history_incomplete")
    current = [r for r in runs if str(r.get("id")) == env.get("GITHUB_RUN_ID")]
    if len(current) != 1:
        raise LoopBlocked("current_run_unavailable")
    current = current[0]
    current_key = (current["created_at"], int(current["id"]))
    older = [r for r in runs if (r["created_at"], int(r["id"])) < current_key]
    if int(env.get("GITHUB_RUN_ATTEMPT", "0")) > 1:
        # A rerun must recover its previous attempt's state as well.
        older.append(current)
    if not older:
        return None
    previous = max(older, key=lambda r: (r["created_at"], int(r["id"])))
    if previous is not current and previous.get("status") != "completed":
        raise LoopBlocked("prior_run_not_terminal")
    return previous


def select_artifact(data: dict) -> dict:
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list) or data.get("total_count") != len(artifacts):
        raise LoopBlocked("artifact_history_incomplete")
    selected = [a for a in artifacts if a.get("name") == ARTIFACT and not a.get("expired")]
    if len(selected) != 1 or type(selected[0].get("id")) is not int:
        raise LoopBlocked("prior_ledger_missing_or_ambiguous")
    return selected[0]


def restore(env: dict) -> str:
    if (env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
        raise LoopBlocked("manual_loop_workflow_required")
    if TARGET.exists():
        raise LoopBlocked("unexpected_existing_ledger")
    previous = select_previous(gh_api(f"workflows/{WORKFLOW}/runs?per_page=100"), env)
    if previous is None:
        return "first_campaign_run"
    artifact = select_artifact(gh_api(f"runs/{previous['id']}/artifacts?per_page=100"))
    archive = gh_api(f"artifacts/{artifact['id']}/zip", binary=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        info = bundle.getinfo("research-loop.sqlite")
        if info.file_size > 8 * 1024 * 1024:
            raise LoopBlocked("ledger_too_large")
        ledger = bundle.read(info)
    if not ledger.startswith(b"SQLite format 3\x00"):
        raise LoopBlocked("invalid_prior_ledger")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temp = TARGET.with_suffix(".restore")
    temp.write_bytes(ledger)
    db = sqlite3.connect(temp)
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LoopBlocked("prior_ledger_integrity_failed")
    finally:
        db.close()
    os.replace(temp, TARGET)
    return "restored_previous_ledger"


if __name__ == "__main__":
    try:
        print(restore(dict(os.environ)))
    except Exception as exc:
        code = str(exc) if isinstance(exc, LoopBlocked) else "ledger_restore_failed"
        raise SystemExit("Research loop stopped: " + code) from None
