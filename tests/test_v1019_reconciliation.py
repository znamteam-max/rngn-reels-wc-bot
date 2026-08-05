from __future__ import annotations

import copy
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot import reconciliation, sheets
from bot.jobs import ALLOWED_JOB_KINDS
from bot.projects import PROJECT_SHEET_TITLES
from bot.version import VERSION
from scripts.init_db import SCHEMA_SQL


ROOT = Path(__file__).resolve().parents[1]


def make_video(
    video_id: int,
    status: str,
    project_code: str | None,
    publish_date: date | None,
    *,
    video_type: str = "regular",
) -> dict[str, object]:
    return {
        "id": video_id,
        "status": status,
        "video_type": video_type,
        "project_id": video_id if project_code else None,
        "project_code": project_code,
        "project_name": reconciliation.PROJECT_NAMES.get(project_code or "unassigned") if project_code else None,
        "publish_date": publish_date,
        "instagram_id": f"ig-{video_id}" if video_type == "regular" else None,
        "instagram_url": f"https://instagram.com/reel/ig-{video_id}" if video_type == "regular" else None,
        "youtube_id": f"yt-{video_id}" if video_type == "bigrecap" else None,
        "youtube_url": f"https://youtu.be/yt-{video_id}" if video_type == "bigrecap" else None,
        "author_id": 10,
        "author_name": "Author",
        "author_username": "author",
        "montage_id": 20,
        "montage_name": "Editor",
        "montage_username": "editor",
        "voice_name": None,
        "added_by_tg_id": 100,
        "added_by_username": "owner",
        "created_at": datetime(2026, 5, 1, 10, video_id, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 1, 11, video_id, tzinfo=timezone.utc),
    }


class ReconciliationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.videos = [
            make_video(1, "approved", "bolshe", date(2026, 5, 4)),
            make_video(2, "pending", "ves_sport", date(2026, 6, 5)),
            make_video(3, "needs_revision", None, None),
            make_video(4, "duplicate", "bolshe", date(2027, 1, 6)),
            make_video(5, "deleted", "bolshe", date(2026, 8, 7)),
        ]
        specs = sheets.build_managed_sheet_specs(
            self.videos,
            [],
            [],
            reconciliation.reconciliation_rows(reconciliation._expected_pass_result(4)),
        )
        self.specs = {spec["name"]: spec for spec in specs}
        self.tables = {
            name: [spec["columns"], *spec["rows"]]
            for name, spec in self.specs.items()
        }

    def ids(self, sheet_name: str) -> list[int]:
        return [int(row[0]) for row in self.specs[sheet_name]["rows"]]


class CanonicalPartitionsV1019Tests(ReconciliationFixture):
    def test_every_active_id_appears_once_in_master(self) -> None:
        self.assertEqual(self.ids(sheets.SHEET_NAME), [1, 2, 4, 3])
        self.assertEqual(len(self.ids(sheets.SHEET_NAME)), len(set(self.ids(sheets.SHEET_NAME))))

    def test_every_active_id_appears_once_in_project_partition(self) -> None:
        ids = [video_id for title in PROJECT_SHEET_TITLES.values() for video_id in self.ids(title)]
        self.assertCountEqual(ids, [1, 2, 3, 4])
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_active_id_appears_once_in_month_partition(self) -> None:
        month_names = [name for name in self.specs if reconciliation.MONTH_RE.match(name)]
        month_names.append(reconciliation.NO_DATE_SHEET)
        ids = [video_id for title in month_names for video_id in self.ids(title)]
        self.assertCountEqual(ids, [1, 2, 3, 4])
        self.assertEqual(len(ids), len(set(ids)))

    def test_null_project_and_date_have_explicit_partitions(self) -> None:
        self.assertEqual(reconciliation.project_partition_sheet(self.videos[2]), PROJECT_SHEET_TITLES["unassigned"])
        self.assertEqual(reconciliation.month_partition_sheet(self.videos[2]), reconciliation.NO_DATE_SHEET)
        self.assertEqual(self.ids(PROJECT_SHEET_TITLES["unassigned"]), [3])
        self.assertEqual(self.ids(reconciliation.NO_DATE_SHEET), [3])

    def test_month_uses_publish_date_and_future_month_is_automatic(self) -> None:
        self.assertEqual(self.ids("2026-05"), [1])
        self.assertEqual(self.ids("2027-01"), [4])
        self.assertIn("2027-01", self.specs)

    def test_derived_columns_are_present_and_correct(self) -> None:
        row = dict(zip(sheets.SHEET_COLUMNS, sheets.video_to_row(self.videos[2])))
        self.assertEqual(row["publish_month"], "")
        self.assertEqual(row["is_published"], "FALSE")
        self.assertEqual(row["is_incomplete"], "TRUE")
        self.assertIn("project", row["missing_fields"])
        self.assertIn("publish_date", row["missing_fields"])

    def test_rebuild_rows_are_compact_and_unknown_sheets_are_not_managed(self) -> None:
        partition_names = [
            sheets.SHEET_NAME,
            *PROJECT_SHEET_TITLES.values(),
            *[name for name in self.specs if reconciliation.MONTH_RE.match(name)],
            reconciliation.NO_DATE_SHEET,
        ]
        for name in partition_names:
            spec = self.specs[name]
            self.assertTrue(all(row and row[0] != "" for row in spec["rows"]) or not spec["rows"])
        self.assertNotIn("My Notes", self.specs)


class StatisticsV1019Tests(ReconciliationFixture):
    def test_only_approved_increments_published_and_workflow_is_separate(self) -> None:
        month = next(row for row in reconciliation.build_month_stats_rows(self.videos) if row[0] == "ALL")
        self.assertEqual(month[1:6], ["4", "1", "1", "1", "1"])

    def test_duplicate_is_visible_but_not_published(self) -> None:
        future = next(row for row in reconciliation.build_month_stats_rows(self.videos) if row[0] == "2027-01")
        self.assertEqual(future[2], "0")
        self.assertEqual(future[5], "1")

    def test_project_stats_include_unassigned_and_all_periods(self) -> None:
        rows = reconciliation.build_project_stats_rows(self.videos)
        self.assertTrue(any(row[0] == "ALL" and row[1] == "unassigned" for row in rows))
        self.assertTrue(any(row[0] == "2026-05" for row in rows))
        self.assertTrue(any(row[0] == "NO_DATE" for row in rows))

    def test_people_projects_counts_approved_only_and_has_period(self) -> None:
        rows = reconciliation.build_people_projects_rows(self.videos)
        self.assertTrue(rows)
        self.assertTrue(all(row[0] in {"ALL", "2026-05"} for row in rows))
        self.assertTrue(all(row[3] == "bolshe" for row in rows))


class AuditV1019Tests(ReconciliationFixture):
    def test_canonical_tables_pass_all_equality_invariants(self) -> None:
        result = reconciliation.audit_sheet_tables(
            self.videos, self.tables, video_columns=sheets.SHEET_COLUMNS
        )
        self.assertEqual(result["mismatch_count"], 0)
        self.assertEqual(result["sheet_videos_unique_count"], 4)
        self.assertEqual(result["sheet_project_union_count"], 4)
        self.assertEqual(result["sheet_month_union_count"], 4)

    def test_audit_detects_duplicate_missing_extra_and_row_mismatch(self) -> None:
        tables = copy.deepcopy(self.tables)
        tables[sheets.SHEET_NAME].append(copy.deepcopy(tables[sheets.SHEET_NAME][1]))
        tables[sheets.SHEET_NAME][1][1] = "pending"
        tables[sheets.SHEET_NAME].append(["999"])
        result = reconciliation.audit_sheet_tables(
            self.videos, tables, video_columns=sheets.SHEET_COLUMNS
        )
        self.assertEqual(result["duplicate_in_videos"], 1)
        self.assertEqual(result["extra_in_videos"], 1)
        self.assertEqual(result["videos_row_mismatches"], 1)
        self.assertGreater(result["mismatch_count"], 0)

    def test_audit_detects_wrong_and_multiple_project_membership(self) -> None:
        tables = copy.deepcopy(self.tables)
        tables[PROJECT_SHEET_TITLES["ves_sport"]].append(copy.deepcopy(tables[PROJECT_SHEET_TITLES["bolshe"]][1]))
        result = reconciliation.audit_sheet_tables(
            self.videos, tables, video_columns=sheets.SHEET_COLUMNS
        )
        self.assertEqual(result["duplicate_in_projects"], 1)
        self.assertGreaterEqual(result["project_mismatches"], 1)

    def test_audit_detects_wrong_and_multiple_month_membership(self) -> None:
        tables = copy.deepcopy(self.tables)
        tables["2026-06"].append(copy.deepcopy(tables["2026-05"][1]))
        result = reconciliation.audit_sheet_tables(
            self.videos, tables, video_columns=sheets.SHEET_COLUMNS
        )
        self.assertEqual(result["duplicate_in_months"], 1)
        self.assertGreaterEqual(result["month_mismatches"], 1)

    def test_run_summary_uses_complete_mismatch_count(self) -> None:
        self.assertEqual(reconciliation.run_mismatch_count({"summary": {"mismatch_count": 17}}), 17)


class BackfillV1019Tests(unittest.TestCase):
    def test_unique_recognized_membership_is_safe(self) -> None:
        video = make_video(10, "pending", None, date(2026, 8, 1))
        item = reconciliation.classify_project_backfills(
            [video], {"10": [PROJECT_SHEET_TITLES["ves_sport"]]}
        )[0]
        self.assertEqual(item["classification"], "safe")
        self.assertEqual(item["proposed_project_code"], "ves_sport")

    def test_conflict_and_unknown_sheet_are_never_safe(self) -> None:
        video = make_video(10, "pending", None, date(2026, 8, 1))
        conflict = reconciliation.classify_project_backfills(
            [video], {"10": [PROJECT_SHEET_TITLES["ves_sport"], PROJECT_SHEET_TITLES["bolshe"]]}
        )[0]
        unknown = reconciliation.classify_project_backfills([video], {"10": ["Legacy Project"]})[0]
        self.assertEqual(conflict["classification"], "conflict")
        self.assertEqual(unknown["classification"], "unknown_sheet")


class UnfinishedV1019Tests(unittest.TestCase):
    def test_revision_and_missing_required_fields_are_listed(self) -> None:
        now = datetime(2026, 8, 5, tzinfo=timezone.utc)
        revision = make_video(1, "needs_revision", "bolshe", date(2026, 8, 1))
        missing = make_video(2, "pending", None, None)
        rows = reconciliation.build_unfinished_rows([revision, missing], now=now)
        self.assertEqual([row[0] for row in rows], ["1", "2"])
        self.assertIn("returned_for_revision", rows[0][14])
        self.assertIn("assign_project", rows[1][14])

    def test_stale_sessions_are_separate_and_never_counted_as_reels(self) -> None:
        now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        sessions = [{
            "tg_id": 42,
            "username": "draft",
            "state": "new:project",
            "data": {"instagram_id": "draft-id"},
            "created_at": now - timedelta(hours=3),
            "updated_at": now - timedelta(hours=2),
        }]
        rows = reconciliation.build_unsubmitted_rows(sessions, now=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "42")
        self.assertEqual(rows[0][5], "2")
        source = (ROOT / "bot" / "reconciliation.py").read_text(encoding="utf-8")
        self.assertIn("interval '60 minutes'", source)


class StagingAndJobsV1019Tests(unittest.TestCase):
    def test_unknown_sheet_id_is_not_touched_during_promotion(self) -> None:
        service = MagicMock()
        properties = {
            "Videos": {"sheetId": 1},
            "__tmp__r7_00": {"sheetId": 2},
            "My Notes": {"sheetId": 999},
        }
        with (
            patch("bot.sheets.get_settings", return_value=SimpleNamespace(google_sheets_spreadsheet_id="sheet")),
            patch("bot.sheets._sheet_properties", return_value=properties),
        ):
            sheets.promote_staging_sheets({"Videos": "__tmp__r7_00"}, run_id=7, service=service)
        requests = service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        self.assertNotIn(999, [item.get("deleteSheet", {}).get("sheetId") for item in requests])

    def test_staging_write_failure_never_invokes_final_promotion(self) -> None:
        service = MagicMock()
        with (
            patch("bot.sheets.get_settings", return_value=SimpleNamespace(google_sheets_spreadsheet_id="sheet")),
            patch("bot.sheets._ensure_named_sheets"),
            patch("bot.sheets._replace_named_sheet", side_effect=RuntimeError("timeout")),
        ):
            with self.assertRaises(RuntimeError):
                sheets.write_staging_sheet("__tmp__r8_00", ["id"], [["1"]], service=service)
        service.spreadsheets.return_value.batchUpdate.assert_not_called()

    def test_resume_skips_already_staged_sheet_index(self) -> None:
        run = {"id": 9, "status": "rebuilding", "sheet_index": 2, "summary": {"sheet_names": ["A", "B", "C"]}}
        with (
            patch("bot.reconciliation.get_run", return_value=run),
            patch("bot.sheets.write_staging_sheet") as write,
        ):
            result = reconciliation.rebuild_sheet_chunk(9, 1)
        self.assertEqual(result, run)
        write.assert_not_called()

    def test_all_durable_job_kinds_and_priorities_are_wired(self) -> None:
        required = {
            "sheets_audit",
            "sheets_reconcile",
            "sheets_rebuild_chunk",
            "sheets_validate",
            "unfinished_requests_sync",
        }
        self.assertTrue(required.issubset(ALLOWED_JOB_KINDS))
        source = (ROOT / "bot" / "reconciliation.py").read_text(encoding="utf-8")
        for priority in (50, 55, 56, 57):
            self.assertIn(f"priority={priority}", source)
        self.assertIn("priority=65", (ROOT / "bot" / "handlers.py").read_text(encoding="utf-8"))

    def test_batch_api_and_incremental_project_month_sync_are_present(self) -> None:
        source = (ROOT / "bot" / "sheets.py").read_text(encoding="utf-8")
        self.assertIn(".batchGet(", source)
        self.assertIn(".batchUpdate(", source)
        self.assertIn('body={"valueInputOption": "RAW", "data": updates}', source)
        self.assertIn("_sync_video_project_sheet", source)
        self.assertIn("_sync_video_month_sheet", source)
        self.assertIn("sheets_sync_stats", (ROOT / "bot" / "job_worker.py").read_text(encoding="utf-8"))


class ContractsV1019Tests(unittest.TestCase):
    def test_schema_version_tables_and_progress_are_additive(self) -> None:
        self.assertEqual(VERSION, "1.0.19")
        self.assertIn("CREATE TABLE IF NOT EXISTS sheet_reconciliation_runs", SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS sheet_reconciliation_items", SCHEMA_SQL)
        self.assertIn("sheet_index integer NOT NULL DEFAULT 0", SCHEMA_SQL)
        self.assertIn("VALUES ('1.0.19')", SCHEMA_SQL)

    def test_admin_commands_and_confirmation_gate_are_registered(self) -> None:
        source = (ROOT / "bot" / "handlers.py").read_text(encoding="utf-8")
        for command in ("/sheets_audit", "/reconcile_sheets", "/unfinished_requests", "/sheets_status"):
            self.assertIn(command, source)
        self.assertIn("awaiting_confirmation", (ROOT / "bot" / "reconciliation.py").read_text(encoding="utf-8"))

    def test_first_audit_has_no_reminders_or_mass_return(self) -> None:
        source = (ROOT / "bot" / "reconciliation.py").read_text(encoding="utf-8")
        audit_body = source[source.index("def audit_run"):source.index("def confirm_run")]
        self.assertNotIn("enqueue_telegram_notification", audit_body)
        self.assertNotIn("return_missing_dates", audit_body)

    def test_approval_submission_date_and_project_paths_enqueue_sheet_sync(self) -> None:
        source = (ROOT / "bot" / "handlers.py").read_text(encoding="utf-8")
        for flow in ("submission", "project_changed", "publish_date_changed", "approval"):
            self.assertIn(f'flow="{flow}"', source)

    def test_atomic_fifo_regression_files_remain_and_work_chat_is_absent(self) -> None:
        self.assertTrue((ROOT / "tests" / "test_v1018_atomic_fifo.py").exists())
        production_source = "\n".join(
            path.read_text(encoding="utf-8")
            for folder in ("bot", "api", "scripts")
            for path in (ROOT / folder).rglob("*.py")
        )
        self.assertNotIn("WORK_CHAT_ID", production_source)
        self.assertNotIn("work_chat_id", production_source)


if __name__ == "__main__":
    unittest.main()
