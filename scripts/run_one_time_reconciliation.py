from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = (
    "https://project-dcd2y.vercel.app/api/internal/"
    "reconcile-safe-backfill-f179b8536c79fb2e341a3cbf"
)
RESULT_PATH = Path("ops/reconciliation_v1_0_19_live_result.json")


def fetch(action: str, timeout: int = 290) -> dict:
    request = urllib.request.Request(
        f"{BASE_URL}?action={action}",
        headers={"User-Agent": "rngn-reconciliation-once/1.2"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def write_result(payload: dict) -> None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    result: dict = {
        "ok": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stage": "waiting_for_deployment",
    }

    ready = False
    for _ in range(60):
        try:
            status = fetch("status", timeout=20)
            if status.get("run_id") == 1:
                result["initial_status"] = status
                ready = True
                break
        except Exception as exc:
            result["last_wait_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(10)

    if not ready:
        result.update(
            {
                "stage": "endpoint_unavailable",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        write_result(result)
        return 1

    try:
        result["confirm_response"] = fetch("confirm", timeout=30)
    except Exception as exc:
        result["confirm_error"] = f"{type(exc).__name__}: {exc}"

    for _ in range(150):
        try:
            status = fetch("status", timeout=30)
            result["last_status"] = status
        except Exception as exc:
            result["last_status_error"] = f"{type(exc).__name__}: {exc}"
            time.sleep(8)
            continue

        if status.get("status") in {"done", "failed", "cancelled"}:
            break

        try:
            result["last_drain"] = fetch("drain", timeout=290)
        except Exception as exc:
            result["last_drain_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(8)

    try:
        status = fetch("status", timeout=30)
    except Exception as exc:
        status = result.get("last_status") or {
            "ok": False,
            "status": "unknown",
            "error": f"{type(exc).__name__}: {exc}",
        }

    active = int(status.get("db_active_count") or 0)
    videos = int(status.get("sheet_videos_count") or 0)
    projects = int(status.get("sheet_project_union_count") or 0)
    months = int(status.get("sheet_month_union_count") or 0)
    mismatches = int(status.get("mismatch_count") or 0)
    work_chat_present = bool(status.get("work_chat_id_present"))
    passed = (
        status.get("status") == "done"
        and active == videos == projects == months
        and mismatches == 0
        and not work_chat_present
    )

    result.update(
        {
            "ok": passed,
            "stage": "done" if passed else "validation_failed",
            "final_status": status,
            "equality": {
                "db_active": active,
                "videos": videos,
                "project_union": projects,
                "month_union": months,
            },
            "mismatch_count": mismatches,
            "work_chat_id_present": work_chat_present,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    write_result(result)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
