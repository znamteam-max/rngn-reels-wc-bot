from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://project-dcd2y.vercel.app/api/internal/report-v1-0-20-7b4d91"
RESULT = Path("ops/report_v1_0_20_result.json")
AUDIENCE = "rngn-reels-wc-worker"


def oidc_token() -> str:
    request_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    request_token = os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
    sep = "&" if "?" in request_url else "?"
    url = request_url + sep + urllib.parse.urlencode({"audience": AUDIENCE})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {request_token}"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))["value"]


def call(action: str, timeout: int = 290) -> dict:
    # GitHub OIDC tokens are short lived. Refresh before every Vercel call.
    token = oidc_token()
    req = urllib.request.Request(
        f"{BASE}?action={action}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "rngn-report-v1.0.20/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_endpoint(final: dict) -> bool:
    for _ in range(120):
        try:
            status = call("status", timeout=20)
            if status.get("ok"):
                final["initial_status"] = status
                return True
        except Exception as exc:
            final["last_wait_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(6)
    return False


def drain_until(final: dict, run_id: int, wanted: set[str], limit: int) -> dict:
    status: dict = {}
    for _ in range(limit):
        status = call("status", timeout=30)
        final["last_status"] = status
        if int(status.get("run_id") or 0) == run_id and status.get("run_status") in wanted:
            return status
        if status.get("run_status") in {"failed", "cancelled"}:
            return status
        final["last_drain"] = call("drain", timeout=290)
        time.sleep(2)
    return status


def main() -> int:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    final: dict = {
        "ok": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        if not wait_endpoint(final):
            final["stage"] = "endpoint_unavailable"
            return 1

        apply = call("apply", timeout=90)
        final["apply"] = apply
        run_id = int(apply.get("run_id") or 0)
        if not run_id:
            raise RuntimeError("apply did not return reconciliation run_id")

        status = drain_until(final, run_id, {"awaiting_confirmation", "done"}, 180)
        if status.get("run_status") == "awaiting_confirmation":
            final["confirm"] = call("confirm", timeout=30)
            status = drain_until(final, run_id, {"done", "failed", "cancelled"}, 300)

        if status.get("run_status") != "done":
            final["stage"] = "reconciliation_not_done"
            final["final_status"] = status
            return 1

        post = call("postprocess", timeout=120)
        final["postprocess"] = post
        status = call("status", timeout=60)
        final["final_status"] = status

        manual = apply.get("manual_corrections") or {}
        preambles = (post.get("preambles") or {}) if isinstance(post, dict) else {}
        success = all(
            [
                status.get("run_status") == "done",
                int(status.get("mismatch_count") or 0) == 0,
                not preambles.get("failures"),
                bool(status.get("author_report_all") is not None),
                bool(status.get("montage_report_all") is not None),
                manual.get("levchenko_candidate_count") in {0, 1},
                manual.get("hamidulin_candidate_count") in {0, 1},
            ]
        )
        final["ok"] = success
        final["stage"] = "done" if success else "validation_failed"
        return 0 if success else 1
    except Exception as exc:
        import traceback

        final["stage"] = "exception"
        final["error"] = f"{type(exc).__name__}: {exc}"
        final["traceback"] = traceback.format_exc()[-6000:]
        return 1
    finally:
        final["finished_at"] = datetime.now(timezone.utc).isoformat()
        RESULT.write_text(json.dumps(final, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
