from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://project-dcd2y.vercel.app/api/internal/archive-world-cup-2026-5d9f7a2c91.py"
RESULT = Path("ops/world_cup_2026_archive_result.json")


def sh(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def oidc_token() -> str:
    request_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    request_token = os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
    separator = "&" if "?" in request_url else "?"
    url = request_url + separator + urllib.parse.urlencode({"audience": "rngn-reels-wc-worker"})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {request_token}"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))["value"]


def call(token: str, action: str, timeout: int = 290) -> dict:
    req = urllib.request.Request(
        f"{BASE}?action={action}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "rngn-world-cup-archive/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    final = {"ok": False, "started_at": datetime.now(timezone.utc).isoformat()}
    token = oidc_token()

    # Wait until the source patch + temporary endpoint reach production.
    ready = False
    for _ in range(100):
        try:
            status = call(token, "status", timeout=20)
            if status.get("ok"):
                final["initial_status"] = status
                ready = True
                break
        except Exception as exc:
            final["last_wait_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(8)
    if not ready:
        final.update(stage="endpoint_unavailable", finished_at=datetime.now(timezone.utc).isoformat())
        RESULT.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    apply = call(token, "apply", timeout=60)
    final["apply_response"] = apply
    target_run_id = int(apply.get("run_id") or 0)

    # Audit stage.
    for _ in range(180):
        status = call(token, "status", timeout=30)
        final["last_status"] = status
        if int(status.get("run_id") or 0) == target_run_id and status.get("run_status") == "awaiting_confirmation":
            break
        if status.get("run_status") in {"failed", "cancelled"}:
            break
        final["last_drain"] = call(token, "drain", timeout=290)
        time.sleep(5)

    confirm = call(token, "confirm", timeout=30)
    final["confirm_response"] = confirm

    # Rebuild/validation stage.
    for _ in range(240):
        status = call(token, "status", timeout=30)
        final["last_status"] = status
        if int(status.get("run_id") or 0) == target_run_id and status.get("run_status") in {"done", "failed", "cancelled"}:
            break
        final["last_drain"] = call(token, "drain", timeout=290)
        time.sleep(5)

    status = call(token, "status", timeout=30)
    final["final_status"] = status
    counts = status.get("project_counts_live") or {}
    sheet_counts = status.get("project_sheet_counts") or {}
    active = int(status.get("db_active_count") or 0)
    final["equality"] = {
        "db_active": active,
        "videos": int(status.get("sheet_videos_count") or 0),
        "project_union": int(status.get("sheet_project_union_count") or 0),
        "month_union": int(status.get("sheet_month_union_count") or 0),
        "world_cup_2026_live": int(counts.get("world_cup_2026") or 0),
        "world_cup_2026_sheet": int(sheet_counts.get("ЧМ 2026") or 0),
        "ves_sport_live": int(counts.get("ves_sport") or 0),
        "ves_sport_sheet": int(sheet_counts.get("Весь Спорт") or 0),
    }
    passed = (
        status.get("run_status") == "done"
        and int(status.get("mismatch_count") or 0) == 0
        and active == int(status.get("sheet_videos_count") or 0)
        and active == int(status.get("sheet_project_union_count") or 0)
        and active == int(status.get("sheet_month_union_count") or 0)
        and int(counts.get("world_cup_2026") or 0) == active
        and int(counts.get("ves_sport") or 0) == 0
        and int(sheet_counts.get("ЧМ 2026") or 0) == active
        and int(sheet_counts.get("Весь Спорт") or 0) == 0
    )
    final.update(
        ok=passed,
        stage="done" if passed else "validation_failed",
        mismatch_count=int(status.get("mismatch_count") or 0),
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    RESULT.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
