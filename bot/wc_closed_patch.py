from __future__ import annotations

from bot import (
    content_core_integration,
    flexible_first_link,
    period_report,
    project_workflow_patch,
    vm_active_rules,
)


WORLD_CUP_CODE = "world_cup_2026"
_REPORT_PATCHED = False


def _install_period_report(author_reports) -> None:
    global _REPORT_PATCHED
    if _REPORT_PATCHED:
        return
    original_handle_message = author_reports.handle_message

    def handle_message(message):
        if period_report.handle_message(message):
            return True
        return original_handle_message(message)

    author_reports.handle_message = handle_message
    _REPORT_PATCHED = True


def install(author_reports) -> None:
    """Compatibility entrypoint after the World Cup-only reporting freeze was retired.

    The webhook still imports this module, so keep the entrypoint stable while the
    permanent project-aware workflow is rolled out. World Cup 2026 stays available
    as archived data through author_reports; it no longer replaces the global
    reporting dataset.
    """

    _install_period_report(author_reports)
    project_workflow_patch.install()
    flexible_first_link.install()
    vm_active_rules.install()
    content_core_integration.install_submission_hooks()
