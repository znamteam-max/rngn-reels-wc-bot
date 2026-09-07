from __future__ import annotations

import unittest

from bot import aircut_presentation as presentation
from bot import project_workflow_patch as workflow


class AircutPresentationV1038Tests(unittest.TestCase):
    def test_technical_aircut_marker_is_hidden_from_comment(self) -> None:
        self.assertIsNone(presentation.visible_comment(workflow.AIR_CUT_MARKER))

    def test_admin_revision_comment_survives_after_aircut_marker(self) -> None:
        value = workflow.AIR_CUT_MARKER + "\n\nКомментарий администратора:\nПоменять титр"
        self.assertEqual(
            presentation.visible_comment(value),
            "Комментарий администратора:\nПоменять титр",
        )

    def test_aircut_type_replaces_regular_label(self) -> None:
        text = "Проект: Взял Мяч\nТип: ролик\nСтатус: ожидает проверки"
        self.assertEqual(
            presentation._insert_aircut_type(text),
            "Проект: Взял Мяч\nТип: отрез из эфира\nСтатус: ожидает проверки",
        )

    def test_aircut_type_is_inserted_when_card_has_no_regular_type_line(self) -> None:
        text = "Заявка #351\nПроект: Взял Мяч\nInstagram: https://example.com"
        self.assertEqual(
            presentation._insert_aircut_type(text),
            "Заявка #351\nПроект: Взял Мяч\nТип: отрез из эфира\nInstagram: https://example.com",
        )


if __name__ == "__main__":
    unittest.main()
