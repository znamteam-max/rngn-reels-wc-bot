from __future__ import annotations

import json, os, time, traceback, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://project-dcd2y.vercel.app/api/internal/archive-world-cup-2026-atomic"
RESULT = Path("ops/world_cup_2026_archive_result.json")


def token() -> str:
    base = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    sep = "&" if "?" in base else "?"
    url = base + sep + urllib.parse.urlencode({"audience":"rngn-reels-wc-worker"})
    req = urllib.request.Request(url, headers={"Authorization":f"Bearer {os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())["value"]


def call(tok: str, action: str, timeout: int = 290) -> dict:
    req = urllib.request.Request(f"{BASE}?action={action}",
        headers={"Authorization":f"Bearer {tok}","User-Agent":"rngn-wc26-archive/1.0"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def execute(final: dict) -> bool:
    tok = token()
    initial = None
    for _ in range(120):
        try:
            initial = call(tok,"status",20)
            if initial.get("ok"): break
        except Exception as exc:
            final["last_wait_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(5)
    if not initial or not initial.get("ok"):
        raise RuntimeError("migration endpoint did not become ready")
    final["initial_status"] = initial
    initial_projects = initial.get("project_counts_live") or {}
    expected_archive = int(initial_projects.get("world_cup_2026") or 0) + int(initial_projects.get("ves_sport") or 0)
    expected_changed = int(initial_projects.get("ves_sport") or 0)

    applied = call(tok,"apply",60)
    final["apply_response"] = applied
    run_id = int(applied.get("run_id") or 0)
    if int(applied.get("changed_count") or 0) != expected_changed:
        raise RuntimeError("changed_count differs from initial Весь Спорт count")

    for _ in range(240):
        s = call(tok,"status",30); final["last_status"] = s
        if int(s.get("run_id") or 0)==run_id and s.get("run_status")=="awaiting_confirmation": break
        if s.get("run_status") in {"failed","cancelled"}: raise RuntimeError(f"audit ended as {s.get('run_status')}")
        final["last_drain"] = call(tok,"drain",290)
        time.sleep(3)
    else: raise RuntimeError("audit did not reach confirmation")

    final["confirm_response"] = call(tok,"confirm",30)
    for _ in range(360):
        s = call(tok,"status",30); final["last_status"] = s
        if int(s.get("run_id") or 0)==run_id and s.get("run_status") in {"done","failed","cancelled"}: break
        final["last_drain"] = call(tok,"drain",290)
        time.sleep(3)
    else: raise RuntimeError("rebuild did not finish")

    s = call(tok,"status",30); final["final_status"] = s
    live=s.get("project_counts_live") or {}; sheets=s.get("project_sheet_counts") or {}
    active=int(s.get("db_active_count") or 0)
    eq={"db_active":active,"videos":int(s.get("sheet_videos_count") or 0),
        "project_union":int(s.get("sheet_project_union_count") or 0),"month_union":int(s.get("sheet_month_union_count") or 0),
        "world_cup_2026_live":int(live.get("world_cup_2026") or 0),"world_cup_2026_sheet":int(sheets.get("ЧМ 2026") or 0),
        "ves_sport_live":int(live.get("ves_sport") or 0),"ves_sport_sheet":int(sheets.get("Весь Спорт") or 0)}
    final["equality"] = eq
    final["expected_archive_count"] = expected_archive
    ok=(s.get("run_status")=="done" and int(s.get("mismatch_count") or 0)==0
        and eq["videos"]==active and eq["project_union"]==active and eq["month_union"]==active
        and eq["world_cup_2026_live"]==expected_archive and eq["world_cup_2026_sheet"]==expected_archive
        and eq["ves_sport_live"]==0 and eq["ves_sport_sheet"]==0)
    return ok


def main() -> int:
    RESULT.parent.mkdir(parents=True,exist_ok=True)
    final={"ok":False,"started_at":datetime.now(timezone.utc).isoformat()}
    try:
        ok=execute(final)
        final["ok"]=ok; final["stage"]="done" if ok else "validation_failed"
        return 0 if ok else 1
    except Exception as exc:
        final["stage"]="exception"; final["error"]=f"{type(exc).__name__}: {exc}"; final["traceback"]=traceback.format_exc()[-4000:]
        return 1
    finally:
        final["finished_at"]=datetime.now(timezone.utc).isoformat()
        RESULT.write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding="utf-8")

if __name__=="__main__": raise SystemExit(main())
