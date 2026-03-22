from __future__ import annotations

from codex_trading.qlib_adapter import build_feature_frame, run_frozen_winner_backtest


def test_qlib_adapter_public_exports_exist():
    assert callable(build_feature_frame)
    assert callable(run_frozen_winner_backtest)
