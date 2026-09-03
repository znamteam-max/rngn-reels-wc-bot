from __future__ import annotations

from bot import project_workflow_patch


WORLD_CUP_CODE = "world_cup_2026"


def install(author_reports) -> None:
    """Compatibility entrypoint after the World Cup-only reporting freeze was retired.

    The webhook still imports this module, so keep the entrypoint stable while the
    permanent project-aware reporting workflow is rolled out. World Cup 2026 stays
    available as archived data through author_reports; it no longer replaces the
    global reporting dataset.
    """

    del author_reports
    project_workflow_patch.install()
