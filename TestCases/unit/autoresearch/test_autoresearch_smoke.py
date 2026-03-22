from __future__ import annotations

from codex_trading.autoresearch.service import CandidateStrategySpec, compute_research_score, normalize_candidate_payloads


def test_candidate_normalization_and_score_smoke():
    specs, rejections = normalize_candidate_payloads(
        [
            {"name": "alpha", "enabled_signals": ["momentum", "ema_trend"], "momentum_window": 999},
            {"name": "alpha", "enabled_signals": ["momentum", "ema_trend"]},
        ],
        default_benchmark_symbol="QQQ",
    )
    score = compute_research_score(
        num_bars=252,
        completed_trades=6,
        sharpe=1.2,
        max_drawdown_pct=-8.0,
        annual_turnover=100000.0,
        initial_cash=100000.0,
    )

    assert len(specs) >= 1
    assert isinstance(specs[0], CandidateStrategySpec)
    assert specs[0].benchmark_symbol == "QQQ"
    assert score["research_score"] > 0
