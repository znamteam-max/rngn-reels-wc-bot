from __future__ import annotations

import unittest

from bot import project_workflow_patch as workflow
from bot import revision_type_switch as switch


class RevisionTypeSwitchV1037Tests(unittest.TestCase):
    def test_switch_is_off_for_normal_new_submission(self) -> None:
        self.assertFalse(
            switch.should_offer_type_switch(
                {
                    "status": "pending",
                    "project_code": workflow.VM_PROJECT_CODE,
                    "publish_date": "2026-08-04",
                }
            )
        )

    def test_switch_is_only_for_vm_revision_with_date(self) -> None:
        self.assertTrue(
            switch.should_offer_type_switch(
                {
                    "status": "needs_revision",
                    "project_code": workflow.VM_PROJECT_CODE,
                    "publish_date": "2026-08-04",
                }
            )
        )
        self.assertFalse(
            switch.should_offer_type_switch(
                {
                    "status": "needs_revision",
                    "project_code": "world_cup_2026",
                    "publish_date": "2026-08-04",
                }
            )
        )

    def test_regular_revision_can_become_aircut(self) -> None:
        data = switch.apply_revision_kind(
            {"edit_video_id": 351, "video_type": "regular"},
            switch.REVISION_KIND_AIRCUT,
        )
        self.assertEqual(data["revision_submission_kind"], "aircut")
        self.assertEqual(data["submission_kind"], "aircut")
        self.assertEqual(data["comment"], workflow.AIR_CUT_MARKER)
        self.assertTrue(workflow._is_aircut_data(data))

    def test_aircut_revision_can_become_regular(self) -> None:
        data = switch.apply_revision_kind(
            {
                "edit_video_id": 351,
                "video_type": "regular",
                "submission_kind": "aircut",
                "comment": workflow.AIR_CUT_MARKER,
            },
            switch.REVISION_KIND_REGULAR,
        )
        self.assertEqual(data["revision_submission_kind"], "regular")
        self.assertNotIn("submission_kind", data)
        self.assertNotIn("comment", data)
        self.assertFalse(workflow._is_aircut_data(data))

    def test_aircut_marker_can_be_removed_without_dropping_other_text(self) -> None:
        self.assertIsNone(switch._remove_aircut_marker(workflow.AIR_CUT_MARKER))
        self.assertEqual(
            switch._remove_aircut_marker(workflow.AIR_CUT_MARKER + "\nСлужебная пометка"),
            "Служебная пометка",
        )
        self.assertEqual(switch._remove_aircut_marker("Обычный комментарий"), "Обычный комментарий")


if __name__ == "__main__":
    unittest.main()
