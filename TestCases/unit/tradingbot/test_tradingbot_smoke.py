from __future__ import annotations

from codex_trading.tradingbot.tiger import assess_execution_guard, build_execution_key


def test_execution_key_and_guard_smoke():
    execution_key = build_execution_key(
        paper_account="paper-1234",
        symbol="GOOGL",
        strategy_name="googl_momo",
        trade_date="2026-03-23",
        signal_bar_date="2026-03-20",
        action="buy",
    )
    guard = assess_execution_guard(
        execution_key=execution_key,
        symbol="GOOGL",
        action="BUY",
        open_orders=[],
        historical_orders=[],
        ledger_record=None,
    )

    assert execution_key.endswith(":buy")
    assert guard.decision == "submit_new_order"
