from __future__ import annotations
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "bot" / "multiplatform_metrics.py").read_text(encoding="utf-8")

def test_daily_core_metrics_sync_is_incremental() -> None:
    assert "_fetch_core_rows(lookback_days=3)" in SOURCE
    assert 'params["since"]' in SOURCE
    assert "timedelta(days=days)" in SOURCE
    assert "max(1, min(31" in SOURCE
