from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://project-dcd2y.vercel.app/api/internal/finalize-report-v1-0-20-93c1e7"
AUDIENCE = "rngn-reels-wc-worker"
RESULT = Path("ops/report_v1_0_20_final.json")


def token() -> str:
    request_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    request_token = os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
    sep = "&" if "?" in request_url else "?"
    url = request_url + sep + urllib.parse.urlencode({"audience": AUDIENCE})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {request_token}"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))["value"]


def call(action: str, timeout: int = 290) -> dict:
    req = urllib.request.Request(
        f"{BASE}?action={action}",
        headers={
            "Authorization": f"Bearer {token()}",
            "User-Agent": "rngn-report-v1.0.20-finalizer/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_endpoint(result: dict) -> bool:
    for _ in range(120):
        try:
            status = call("status", timeout=20)
            if status.get("ok"):
                result["initial_status"] = status
                return True
        except Exception as exc:
            result["last_wait_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(5)
    return False


def run() -> int:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    out: dict = {"ok": False, "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        if not wait_endpoint(out):
            out["stage"] = "endpoint_unavailable"
            return 1

        out["ensure"] = call("ensure", timeout=295)
        status = (out["ensure"] or {}).get("status") or {}
        run_id = int(status.get("run_id") or 0)
        if not run_id:
            raise RuntimeError("no reconciliation run")

        # Read-only audit may already have completed inside ensure. Otherwise drain
        # durable jobs until confirmation is available.
        for _ in range(180):
            status = call("status", timeout=30)
            out["last_status_before_confirm"] = status
            if int(status.get("run_id") or 0) == run_id and status.get("run_status") in {"awaiting_confirmation", "done"}:
                break
            if status.get("run_status") in {"failed", "cancelled"}:
                raise RuntimeError(f"reconciliation stopped: {status.get('run_status')}")
            out["last_drain_before_confirm"] = call("drain", timeout=295)
            time.sleep(2)
        else:
            raise RuntimeError("audit did not reach confirmation")

        if status.get("run_status") == "awaiting_confirmation":
            out["confirm"] = call("confirm", timeout=60)

        for _ in range(300):
            status = call("status", timeout=30)
            out["last_status"] = status
            if int(status.get("run_id") or 0) == run_id and status.get("run_status") == "done":
                break
            if status.get("run_status") in {"failed", "cancelled"}:
                raise RuntimeError(f"reconciliation stopped: {status.get('run_status')}")
            out["last_drain"] = call("drain", timeout=295)
            time.sleep(2)
        else:
            raise RuntimeError("reconciliation did not finish")

        post = call("postprocess", timeout=295)
        out["postprocess"] = post
        final = (post or {}).get("status") or call("status", timeout=60)
        out["final_status"] = final

        author_rows = final.get("author_report_all") or []
        egor_rows = [row for row in author_rows if len(row) >= 8 and row[1] == "Егор Петрушков"]
        ham_rows = [row for row in author_rows if len(row) >= 8 and "Хамидулин" in row[1]]
        ham_video = final.get("hamidulin") or {}
        preambles = (post or {}).get("preambles") or {}
        project_counts = final.get("project_sheet_counts") or {}

        checks = {
            "run_done": final.get("run_status") == "done",
            "mismatches_zero": int(final.get("mismatch_count") or 0) == 0,
            "active_309": int(final.get("db_active_count") or 0) == 309,
            "approved_309": int(final.get("approved_count") or 0) == 309,
            "regular_295": int(final.get("approved_regular") or 0) == 295,
            "bigrecap_14": int(final.get("approved_bigrecap") or 0) == 14,
            "needs_revision_zero": int(final.get("needs_revision") or 0) == 0,
            "missing_date_zero": int(final.get("missing_date") or 0) == 0,
            "world_cup_309": int(project_counts.get("ЧМ 2026") or 0) == 309,
            "ves_sport_zero": int(project_counts.get("Весь Спорт") or 0) == 0,
            "egor_one_author_row": len(egor_rows) == 1,
            "egor_canonical_username": len(egor_rows) == 1 and egor_rows[0][2] == "@RayBallPro",
            "hamidulin_author_row": len(ham_rows) == 1,
            "hamidulin_one_bigrecap": len(ham_rows) == 1 and int(ham_rows[0][6]) == 1,
            "hamidulin_video_approved": ham_video.get("status") == "approved" and ham_video.get("video_type") == "bigrecap",
            "all_preambles": not preambles.get("failures"),
            "legacy_people_report_removed": not bool((post or {}).get("people_projects_exists")),
        }
        out["checks"] = checks
        out["ok"] = all(checks.values())
        out["stage"] = "done" if out["ok"] else "validation_failed"
        return 0 if out["ok"] else 1
    except Exception as exc:
        import traceback

        out["stage"] = "exception"
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()[-8000:]
        return 1
    finally:
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        RESULT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(run())
