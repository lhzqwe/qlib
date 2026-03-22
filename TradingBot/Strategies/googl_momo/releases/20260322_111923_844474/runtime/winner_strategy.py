from __future__ import annotations

import json
import sys
from pathlib import Path

from codex_trading.autoresearch.service import CandidateStrategySpec, simulate_candidate_orders


WINNER_SPEC = CandidateStrategySpec.from_payload(
    json.loads('{\n  "atr_stop_mult": 0.0,\n  "atr_window": 14,\n  "bb_percentile": 0.35,\n  "bb_window": 20,\n  "benchmark_symbol": "QQQ",\n  "cooldown_days": 3,\n  "ema_fast": 12,\n  "ema_slow": 40,\n  "enabled_signals": [\n    "ema_trend",\n    "rsi",\n    "benchmark_regime"\n  ],\n  "macd_fast": 12,\n  "macd_signal": 9,\n  "macd_slow": 26,\n  "market_ma_window": 50,\n  "max_hold_days": 30,\n  "momentum_window": 63,\n  "name": "trend_rsi_market_timebox_strict",\n  "position_size_pct": 0.95,\n  "rsi_entry_midline": 55.0,\n  "rsi_period": 14,\n  "rsi_take_profit": 74.0,\n  "short_momentum_window": 21,\n  "volume_ratio_threshold": 1.05,\n  "volume_window": 20,\n  "vote_threshold": 1.0\n}'),
    default_benchmark_symbol='QQQ',
)


def get_spec() -> CandidateStrategySpec:
    return WINNER_SPEC


def build_orders(price_frame, benchmark_frame, symbol: str, initial_cash: float):
    return simulate_candidate_orders(
        spec=WINNER_SPEC,
        price_frame=price_frame,
        benchmark_frame=benchmark_frame,
        symbol=symbol,
        initial_cash=initial_cash,
    )
