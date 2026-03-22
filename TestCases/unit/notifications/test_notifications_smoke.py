from __future__ import annotations

from pathlib import Path

from codex_trading.notifications.feishu import (
    NotificationConfig,
    StrategyStatusSnapshot,
    TigerRuntimeConfig,
    build_notification_text,
)


def test_preview_and_submission_titles_are_action_explicit():
    status = StrategyStatusSnapshot(
        run_dir=Path("D:/QLib/TradingBot/Strategies/googl_momo/live"),
        symbol="GOOGL",
        strategy_name="trend_short_momo_market_holdcap_28",
        research_score=1.23,
        sharpe=1.23,
        cumulative_return_pct=12.0,
        max_drawdown_pct=-3.0,
        latest_preview={
            "trade_date": "2026-03-23",
            "signal_bar_date": "2026-03-20",
            "live_snapshot": {"next_session_action": "buy"},
            "trade_plan": {"action": "buy", "quantity": 10, "order_type": "market", "reason": "ready"},
            "execution_guard": {"decision": "submit_new_order", "reason": "no_conflict"},
        },
        latest_submission={
            "trade_date": "2026-03-23",
            "signal_bar_date": "2026-03-20",
            "trade_plan": {"action": "sell", "quantity": 10, "order_type": "market"},
            "order_id": 42,
            "final_order_status": "Filled",
            "filled_quantity": 10.0,
            "avg_fill_price": 100.5,
        },
    )

    preview_title, _preview_body = build_notification_text("preview", status)
    submission_title, submission_body = build_notification_text("submission", status)

    assert preview_title == "【买入】盘前计划"
    assert submission_title == "【卖出成交】盘中成交"
    assert "动作: 卖出" in submission_body


def test_notification_config_defaults_are_non_blocking_safe():
    config = NotificationConfig()
    runtime = TigerRuntimeConfig(config_path="C:/fake/tiger.properties")

    assert config.push_preview is True
    assert config.push_submission is True
    assert config.push_summary is False
    assert config.push_errors is True
    assert runtime.paper_account is None
