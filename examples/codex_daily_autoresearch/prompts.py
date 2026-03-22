from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional


ALLOWED_SIGNALS = [
    "momentum",
    "short_momentum",
    "ema_trend",
    "rsi",
    "macd_histogram",
    "bollinger_compression",
    "volume_confirmation",
    "benchmark_regime",
]


def build_system_prompt() -> str:
    return (
        "You are designing daily single-stock Qlib order strategies. "
        "You must only return JSON. "
        "Do not write Python code. "
        "Bias strongly toward simpler, more robust strategies that can survive ablation. "
        "Use only the allowed schema fields and allowed signals."
    )


def build_schema_prompt() -> str:
    schema = {
        "type": "object",
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "description": "Return exactly N candidate strategy specs.",
                "items": {
                    "type": "object",
                    "required": ["name", "enabled_signals"],
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "benchmark_symbol": {"type": ["string", "null"]},
                        "enabled_signals": {"type": "array", "items": {"enum": ALLOWED_SIGNALS}},
                        "momentum_window": {"type": "integer"},
                        "short_momentum_window": {"type": "integer"},
                        "ema_fast": {"type": "integer"},
                        "ema_slow": {"type": "integer"},
                        "rsi_period": {"type": "integer"},
                        "rsi_entry_midline": {"type": "number"},
                        "rsi_take_profit": {"type": "number"},
                        "macd_fast": {"type": "integer"},
                        "macd_slow": {"type": "integer"},
                        "macd_signal": {"type": "integer"},
                        "bb_window": {"type": "integer"},
                        "bb_percentile": {"type": "number"},
                        "volume_window": {"type": "integer"},
                        "volume_ratio_threshold": {"type": "number"},
                        "market_ma_window": {"type": "integer"},
                        "atr_window": {"type": "integer"},
                        "atr_stop_mult": {"type": "number"},
                        "cooldown_days": {"type": "integer"},
                        "max_hold_days": {"type": "integer"},
                        "position_size_pct": {"type": "number"},
                        "vote_threshold": {"type": "number"},
                    },
                },
            },
            "ablation": {
                "type": ["object", "null"],
                "description": "A simpler ablation of the current winner.",
            },
            "notes": {"type": "string"},
        },
    }
    return (
        "Allowed signals: %s.\n\n"
        "Exit overlays are implicit:\n"
        "- `atr_stop_mult <= 0` disables ATR trailing stop.\n"
        "- `rsi_take_profit > 100` disables RSI take-profit.\n"
        "- `cooldown_days <= 0` disables cooldown.\n"
        "- `max_hold_days <= 0` disables max-hold.\n"
        "- MA breakdown exit is active only when `ema_trend` is enabled.\n\n"
        "Return JSON only with this shape:\n%s"
    ) % (", ".join(ALLOWED_SIGNALS), json.dumps(schema, indent=2))


def build_round_prompt(
    *,
    config_summary: Dict[str, Any],
    market_summary: Dict[str, Any],
    leaderboard_rows: Iterable[Dict[str, Any]],
    rejected_patterns: List[str],
    current_best: Optional[Dict[str, Any]],
    round_number: int,
    candidates_per_round: int,
) -> str:
    leaderboard_payload = list(leaderboard_rows)
    current_best_payload = current_best or {}
    return (
        "Research objective:\n"
        "- Create %d new daily long-only single-stock strategy specs and 1 ablation.\n"
        "- Keep them simpler than the current winner unless there is a clear reason not to.\n"
        "- Avoid tiny parameter nudges without a structural thesis.\n\n"
        "Scoring formula:\n"
        "- sample_years = max(num_bars / 252, 0.25)\n"
        "- completed_trades_per_year = completed_trades / sample_years\n"
        "- trade_factor = min(completed_trades_per_year / 6.0, 1.0)\n"
        "- drawdown_penalty = max(0, abs(max_drawdown_pct) - 12.0) * 0.08\n"
        "- turnover_ratio = annual_turnover / initial_cash\n"
        "- turnover_penalty = max(0, turnover_ratio - 25.0) * 0.01\n"
        "- research_score = sharpe * sqrt(trade_factor) - drawdown_penalty - turnover_penalty\n"
        "- Hard reject: no orders, completed_trades_per_year < 1.0, max_drawdown_pct < -35.0, or final_value < 0.8 * initial_cash\n\n"
        "Run context:\n%s\n\n"
        "Market/data summary:\n%s\n\n"
        "Current leaderboard:\n%s\n\n"
        "Current winner:\n%s\n\n"
        "Rejected or duplicate patterns to avoid:\n%s\n\n"
        "Round number: %d\n"
        "Return exactly %d candidates in `candidates` and one simpler `ablation`."
    ) % (
        candidates_per_round,
        json.dumps(config_summary, indent=2, sort_keys=True),
        json.dumps(market_summary, indent=2, sort_keys=True),
        json.dumps(leaderboard_payload, indent=2, sort_keys=True),
        json.dumps(current_best_payload, indent=2, sort_keys=True),
        json.dumps(rejected_patterns[-12:], indent=2, ensure_ascii=True),
        round_number,
        candidates_per_round,
    )
