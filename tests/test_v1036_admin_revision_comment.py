from __future__ import annotations

import unittest

from bot import admin_revision_comment as revision
from bot import project_workflow_patch as workflow


class AdminRevisionCommentV1036Tests(unittest.TestCase):
    def test_empty_comment_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            revision.normalize_comment("   ")

    def test_comment_is_persisted_as_visible_revision_block(self) -> None:
        value = revision.merge_revision_comment(None, "Поправить титр")
        self.assertEqual(value, "Комментарий администратора:\nПоправить титр")
        self.assertIsNone(revision.strip_revision_comment(value))

    def test_existing_aircut_marker_survives_revision_cycle(self) -> None:
        value = revision.merge_revision_comment(
            workflow.AIR_CUT_MARKER,
            "Заменить начало",
        )
        self.assertTrue(value.startswith(workflow.AIR_CUT_MARKER))
        self.assertEqual(
            revision.strip_revision_comment(value),
            workflow.AIR_CUT_MARKER,
        )

    def test_existing_non_revision_comment_is_preserved(self) -> None:
        value = revision.merge_revision_comment("Служебная заметка", "Исправить звук")
        self.assertEqual(
            revision.strip_revision_comment(value),
            "Служебная заметка",
        )

    def test_author_message_contains_comment_and_action(self) -> None:
        message = revision.author_revision_message(349, "Добавить YouTube ссылку")
        self.assertIn("Заявка #349", message)
        self.assertIn("Добавить YouTube ссылку", message)
        self.assertIn("/my_requests", message)


if __name__ == "__main__":
    unittest.main()
