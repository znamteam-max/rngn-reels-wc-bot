from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BASE_URL = "https://project-dcd2y.vercel.app/api/internal/reassign-all-world-cup-to-ves-sport-8f3bda9146"
AUDIENCE = "rngn-reels-wc-worker"
RESULT_PATH = Path("ops/world_cup_project_correction_result.json")


def github_oidc_token() -> str:
    request_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    separator = "&" if "?" in request_url else "?"
    url = request_url + separator + urllib.parse.urlencode({"audience": AUDIENCE})
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}",
            "User-Agent": "rngn-world-cup-project-correction/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    token = str(payload.get("value") or "")
    if not token:
        raise RuntimeError("GitHub OIDC token missing")
    return token


def call(token: str, action: str, timeout: int = 290) -> dict:
    request = urllib.request.Request(
        f"{BASE_URL}?action={action}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "rngn-world-cup-project-correction/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    final: dict = {
        "ok": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stage": "waiting_for_endpoint",
    }

    token = github_oidc_token()

    ready = False
    for _ in range(90):
        try:
            status = call(token, "status", timeout=30)
            if status.get("ok"):
                final["initial_status"] = status
                ready = True
                break
        except Exception as exc:
            final["last_wait_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(10)

    if not ready:
        final["stage"] = "endpoint_unavailable"
        final["finished_at"] = datetime.now(timezone.utc).isoformat()
        RESULT_PATH.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    token = github_oidc_token()
    final["apply_response"] = call(token, "apply", timeout=60)
    final["stage"] = "auditing"

    confirmed = False
    last_status: dict = {}
    last_drain: dict = {}

    for _ in range(240):
        token = github_oidc_token()
        last_status = call(token, "status", timeout=30)
        final["last_status"] = last_status

        run_status = last_status.get("run_status")
        if run_status == "awaiting_confirmation" and not confirmed:
            token = github_oidc_token()
            final["confirm_response"] = call(token, "confirm", timeout=60)
            confirmed = True
            final["stage"] = "rebuilding"
            time.sleep(3)
            continue

        if run_status in {"done", "failed", "cancelled"}:
            break

        token = github_oidc_token()
        last_drain = call(token, "drain", timeout=290)
        final["last_drain"] = last_drain
        time.sleep(5)

    token = github_oidc_token()
    status = call(token, "status", timeout=30)

    active = int(status.get("db_active_count") or 0)
    videos = int(status.get("sheet_videos_count") or 0)
    projects = int(status.get("sheet_project_union_count") or 0)
    months = int(status.get("sheet_month_union_count") or 0)
    mismatches = int(status.get("mismatch_count") or 0)
    project_live = status.get("project_counts_live") or {}
    ves_sport = int(project_live.get("ves_sport") or 0)
    non_target = sum(int(value or 0) for key, value in project_live.items() if key != "ves_sport")
    work_chat_present = bool(status.get("work_chat_id_present"))
    jobs = status.get("jobs") or {}
    blocking_jobs = sum(int(jobs.get(key) or 0) for key in ("queued", "processing", "failed", "dead"))

    passed = (
        status.get("run_status") == "done"
        and active == 310
        and active == videos == projects == months == ves_sport
        and non_target == 0
        and mismatches == 0
        and not work_chat_present
        and blocking_jobs == 0
    )

    final.update(
        {
            "ok": passed,
            "stage": "done" if passed else "validation_failed",
            "final_status": status,
            "equality": {
                "db_active": active,
                "videos": videos,
                "project_union": projects,
                "month_union": months,
                "ves_sport": ves_sport,
            },
            "non_target_project_count": non_target,
            "mismatch_count": mismatches,
            "work_chat_id_present": work_chat_present,
            "blocking_jobs": blocking_jobs,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    RESULT_PATH.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
