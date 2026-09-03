from __future__ import annotations

import unittest

from bot import content_core_integration as core
from bot import project_workflow_patch as workflow


class ProjectWorkflowV1032Tests(unittest.TestCase):
    def test_vzyal_myach_author_roster_is_explicit(self) -> None:
        self.assertEqual(
            [item["display_name"] for item in workflow.VM_AUTHOR_ROSTER],
            ["Артём Тихонов", "Знамбо", "Матвей Юдкин", "Сергей Абаев"],
        )
        self.assertEqual(
            [item["display_username"] for item in workflow.VM_AUTHOR_ROSTER],
            ["tikhonov32", "ZnamBo", None, "SergeAbaka"],
        )

    def test_aircut_marker_is_backward_compatible(self) -> None:
        self.assertTrue(
            workflow._is_aircut_data(
                {"video_type": "regular", "submission_kind": "aircut"}
            )
        )
        self.assertTrue(
            workflow._is_aircut_video({"comment": workflow.AIR_CUT_MARKER})
        )
        self.assertFalse(workflow._is_aircut_video({"comment": ""}))

    def test_core_urls_reuse_existing_private_bridge(self) -> None:
        videos_url, attach_url, bridge_url = core._urls()
        self.assertIn("/mirror/", videos_url)
        self.assertTrue(videos_url.endswith("/videos-v2.tsv"))
        self.assertIn("/editor/", attach_url)
        self.assertTrue(attach_url.endswith("/attach-publication.tsv"))
        self.assertTrue(bridge_url.endswith("/bot-metrics.tsv"))

    def test_core_resolver_prefers_instagram_target(self) -> None:
        video = {
            "id": 10,
            "instagram_id": "ig-code",
            "instagram_url": "https://www.instagram.com/reel/ig-code/",
            "youtube_id": "abcdefghijk",
            "youtube_url": "https://youtu.be/abcdefghijk",
        }
        rows = [
            {
                "Instagram URL": "https://www.instagram.com/reel/ig-code/",
                "YouTube URL": "",
                "TikTok URL": "",
                "VK URL": "",
                "raw_video_ids": "core-instagram",
                "video_id": "core-instagram",
            },
            {
                "Instagram URL": "",
                "YouTube URL": "https://youtu.be/abcdefghijk",
                "TikTok URL": "",
                "VK URL": "",
                "raw_video_ids": "core-youtube",
                "video_id": "core-youtube",
            },
        ]
        target, platform, candidates = core._resolve_target(video, rows)
        self.assertEqual(target, "core-instagram")
        self.assertEqual(platform, "instagram")
        self.assertEqual(candidates["youtube"], ["core-youtube"])

    def test_raw_target_uses_stable_raw_id(self) -> None:
        self.assertEqual(
            core._raw_target(
                {
                    "video_id": "group-deadbeef",
                    "raw_video_ids": "raw-a,raw-b",
                }
            ),
            "raw-a",
        )

    def test_content_core_integration_does_not_push_people_or_roles(self) -> None:
        self.assertFalse(hasattr(core, "_sync_attributions"))
        self.assertNotIn("production-attribution", " ".join(core._urls()))


if __name__ == "__main__":
    unittest.main()
