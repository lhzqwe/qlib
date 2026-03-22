from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from tiger_bridge import (
    assess_execution_guard,
    build_execution_key,
    build_live_strategy_snapshot,
    build_tiger_account_summary,
    build_tiger_trade_plan,
    connect_tiger_clients,
    create_tiger_order,
    fetch_tiger_quote_snapshot,
    fetch_tiger_symbol_orders,
    find_latest_run_dir,
    get_execution_record,
    load_frozen_strategy_context,
    mask_account,
    poll_tiger_order_to_terminal,
    upsert_execution_record,
)


US_MARKET_TZ = ZoneInfo("America/New_York")
REGULAR_OPEN_TIME = dt_time(hour=9, minute=30)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for the US open and auto-submit Tiger paper orders from a frozen daily winner.")
    parser.add_argument("--strategy-run-dir", default=None)
    parser.add_argument("--runs-root", default=str(SCRIPT_DIR / "output"))
    parser.add_argument("--tiger-config", required=True)
    parser.add_argument("--paper-account", default=None)
    parser.add_argument("--signal-start-date", default=None)
    parser.add_argument("--signal-end-date", default=None, help="Defaults to the current market date in New York.")
    parser.add_argument("--order-type", choices=["market", "limit"], default="market")
    parser.add_argument("--limit-price", type=float, default=None)
    parser.add_argument("--cash-buffer-pct", type=float, default=0.98)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--execution-window-minutes", type=int, default=15)
    parser.add_argument("--max-preopen-wait-minutes", type=int, default=180)
    parser.add_argument("--order-poll-sec", type=int, default=3)
    parser.add_argument("--max-quote-staleness-sec", type=int, default=15)
    parser.add_argument("--bars-lookback-buffer", type=int, default=30)
    parser.add_argument("--submit", action="store_true")
    return parser.parse_args()


def _resolve_us_market_status(quote_client, market_enum):
    statuses = quote_client.get_market_status(market=market_enum) or []
    for item in statuses:
        market_value = str(getattr(item, "market", "") or "").upper()
        if market_value == "US":
            return item
    raise RuntimeError("Tiger did not return US market status.")


def _now_in_market_tz() -> datetime:
    return datetime.now(timezone.utc).astimezone(US_MARKET_TZ)


def _regular_open_datetime(reference_date) -> datetime:
    return datetime.combine(reference_date, REGULAR_OPEN_TIME, tzinfo=US_MARKET_TZ)


def _determine_trade_window(market_status, execution_window_minutes: int, max_preopen_wait_minutes: int) -> dict:
    now_market = _now_in_market_tz()
    trading_status = str(getattr(market_status, "trading_status", "") or "").upper()
    open_time = getattr(market_status, "open_time", None)
    if open_time is not None and getattr(open_time, "tzinfo", None) is not None:
        open_market = open_time.astimezone(US_MARKET_TZ)
    else:
        open_market = _regular_open_datetime(now_market.date())
    trade_date_obj = now_market.date() if trading_status == "TRADING" else open_market.date()
    trade_date = trade_date_obj.isoformat()
    submission_deadline = _regular_open_datetime(trade_date_obj) + timedelta(minutes=execution_window_minutes)

    if trading_status in {"NOT_YET_OPEN", "PRE_HOUR_TRADING"}:
        wait_seconds = max(0.0, (open_market - now_market).total_seconds())
        if wait_seconds > max_preopen_wait_minutes * 60:
            return {
                "can_trade_today": False,
                "reason": "market_not_opening_soon",
                "trade_date": trade_date,
                "now_market": now_market,
                "wait_seconds": wait_seconds,
                "submission_deadline": submission_deadline,
            }
        return {
            "can_trade_today": True,
            "reason": "wait_for_open",
            "trade_date": trade_date,
            "now_market": now_market,
            "wait_seconds": wait_seconds,
            "submission_deadline": submission_deadline,
        }

    if trading_status == "TRADING":
        if now_market <= submission_deadline:
            return {
                "can_trade_today": True,
                "reason": "within_execution_window",
                "trade_date": now_market.date().isoformat(),
                "now_market": now_market,
                "wait_seconds": 0.0,
                "submission_deadline": submission_deadline,
            }
        return {
            "can_trade_today": False,
            "reason": "beyond_execution_window",
            "trade_date": now_market.date().isoformat(),
            "now_market": now_market,
            "wait_seconds": 0.0,
            "submission_deadline": submission_deadline,
        }

    return {
        "can_trade_today": False,
        "reason": "market_not_actionable",
        "trade_date": trade_date,
        "now_market": now_market,
        "wait_seconds": 0.0,
        "submission_deadline": submission_deadline,
    }


def _serialize_market_status(market_status) -> dict:
    return {
        "market": getattr(market_status, "market", None),
        "status": getattr(market_status, "status", None),
        "trading_status": getattr(market_status, "trading_status", None),
        "open_time": getattr(market_status, "open_time", None).isoformat() if getattr(market_status, "open_time", None) else None,
    }


def main() -> None:
    args = parse_args()
    run_dir = Path(args.strategy_run_dir).expanduser().resolve() if args.strategy_run_dir else find_latest_run_dir(args.runs_root)
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else (run_dir / "tiger_paper_auto")
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = output_dir / "execution_ledger.json"

    tiger_namespace, config_obj, quote_client, trade_client = connect_tiger_clients(args.tiger_config)
    market_status = _resolve_us_market_status(quote_client, tiger_namespace["Market"].US)
    trade_window = _determine_trade_window(
        market_status=market_status,
        execution_window_minutes=args.execution_window_minutes,
        max_preopen_wait_minutes=args.max_preopen_wait_minutes,
    )

    if args.submit and trade_window["wait_seconds"] > 0:
        print("Waiting %d seconds for the US regular open." % round(trade_window["wait_seconds"]))
        time.sleep(float(trade_window["wait_seconds"]))
        market_status = _resolve_us_market_status(quote_client, tiger_namespace["Market"].US)
        trade_window = _determine_trade_window(
            market_status=market_status,
            execution_window_minutes=args.execution_window_minutes,
            max_preopen_wait_minutes=args.max_preopen_wait_minutes,
        )

    trade_date = args.signal_end_date or trade_window["trade_date"]
    if args.submit and trade_date != trade_window["trade_date"]:
        raise RuntimeError("signal_end_date must match the current Tiger trade_date when --submit is used.")

    context = load_frozen_strategy_context(
        run_dir=run_dir,
        signal_start_date=args.signal_start_date,
        signal_end_date=trade_date,
    )
    snapshot = build_live_strategy_snapshot(
        context=context,
        output_dir=output_dir,
        executable_trade_date=trade_date,
        tiger_namespace=tiger_namespace,
        quote_client=quote_client,
        bars_lookback_buffer=args.bars_lookback_buffer,
    )
    account_summary = build_tiger_account_summary(
        trade_client=trade_client,
        config_account=getattr(config_obj, "account", None),
        symbol=context.symbol,
        region=context.region,
        requested_paper_account=args.paper_account,
    )
    quote_snapshot = fetch_tiger_quote_snapshot(quote_client=quote_client, symbol=context.symbol, allow_delayed_preview=True)
    plan = build_tiger_trade_plan(
        snapshot=snapshot,
        account_summary=account_summary,
        order_type=args.order_type,
        quote_snapshot=quote_snapshot,
        cash_buffer_pct=args.cash_buffer_pct,
        limit_price=args.limit_price,
        max_quote_staleness_sec=args.max_quote_staleness_sec,
        require_realtime_quote=True,
    )
    execution_key = build_execution_key(
        paper_account=account_summary.selected_account,
        symbol=context.symbol,
        strategy_name=context.winner_spec.name,
        trade_date=snapshot.next_trade_date,
        signal_bar_date=snapshot.signal_bar_date,
        action=plan.action if plan.action in {"buy", "sell"} else "hold",
    )
    open_orders, historical_orders = fetch_tiger_symbol_orders(
        trade_client=trade_client,
        account=account_summary.selected_account,
        symbol=context.symbol,
        region=context.region,
        trade_date=snapshot.next_trade_date,
    )
    guard = assess_execution_guard(
        execution_key=execution_key,
        symbol=context.symbol,
        action=plan.action.upper() if plan.action else "HOLD",
        open_orders=open_orders,
        historical_orders=historical_orders,
        ledger_record=get_execution_record(ledger_path, execution_key),
    )

    preview_payload = {
        "run_dir": str(run_dir),
        "strategy_name": context.winner_spec.name,
        "market_status": _serialize_market_status(market_status),
        "trade_window": {
            "can_trade_today": bool(trade_window["can_trade_today"]),
            "reason": trade_window["reason"],
            "trade_date": trade_window["trade_date"],
            "wait_seconds": trade_window["wait_seconds"],
            "now_market": trade_window["now_market"].isoformat(),
            "submission_deadline": trade_window["submission_deadline"].isoformat(),
        },
        "signal_bar_date": snapshot.signal_bar_date,
        "trade_date": snapshot.next_trade_date,
        "execution_key": execution_key,
        "live_snapshot": snapshot.to_dict(),
        "quote_snapshot": quote_snapshot.to_dict(),
        "tiger_account": account_summary.to_dict(),
        "trade_plan": plan.to_dict(),
        "execution_guard": guard.to_dict(),
        "submit_requested": bool(args.submit),
    }
    (output_dir / "tiger_paper_auto_preview.json").write_text(json.dumps(preview_payload, indent=2, sort_keys=True), encoding="utf-8")

    print("Strategy run:", run_dir)
    print("Tiger paper account:", mask_account(account_summary.selected_account))
    print("Market status:", json.dumps(preview_payload["market_status"], ensure_ascii=True, sort_keys=True))
    print("Trade window:", json.dumps(preview_payload["trade_window"], ensure_ascii=True, sort_keys=True))
    print("Signal:", snapshot.next_session_action or "hold", "for", snapshot.next_trade_date)
    print("Plan:", json.dumps(plan.to_dict(), ensure_ascii=True, sort_keys=True))
    print("Execution guard:", json.dumps(guard.to_dict(), ensure_ascii=True, sort_keys=True, default=str))
    print("Preview artifact:", output_dir / "tiger_paper_auto_preview.json")

    if not args.submit:
        print("Submission skipped. Re-run with --submit to auto-place the Tiger paper order.")
        return
    if account_summary.selected_account_type.upper() != "PAPER":
        raise RuntimeError("Refusing to submit because the selected Tiger account is not PAPER.")
    if not trade_window["can_trade_today"]:
        print("Submission skipped because the market window is not actionable:", trade_window["reason"])
        return
    if not plan.can_submit or plan.action not in {"buy", "sell"}:
        print("Submission skipped because there is no executable paper order plan.")
        return

    deadline = trade_window["submission_deadline"].astimezone(timezone.utc)
    if guard.decision == "adopt_existing_order":
        order_id = int(guard.matched_order["order_id"])
        result = poll_tiger_order_to_terminal(
            trade_client=trade_client,
            account=account_summary.selected_account,
            order_id=order_id,
            deadline=deadline,
            poll_seconds=args.order_poll_sec,
            ledger_path=ledger_path,
            execution_key=execution_key,
            signal_bar_date=snapshot.signal_bar_date,
            trade_date=snapshot.next_trade_date,
        )
        result_payload = {
            "account": account_summary.selected_account,
            "account_type": account_summary.selected_account_type,
            "signal_bar_date": snapshot.signal_bar_date,
            "trade_date": snapshot.next_trade_date,
            "execution_key": execution_key,
            "submission_deadline": trade_window["submission_deadline"].isoformat(),
            "guard": guard.to_dict(),
            **result,
        }
        (output_dir / "tiger_paper_auto_submission.json").write_text(json.dumps(result_payload, indent=2, sort_keys=True), encoding="utf-8")
        print("Adopted existing Tiger paper order:", order_id)
        print("Submission artifact:", output_dir / "tiger_paper_auto_submission.json")
        return

    if guard.decision != "submit_new_order":
        print("Submission skipped because execution_guard=%s." % guard.reason)
        return

    order = create_tiger_order(
        tiger_namespace=tiger_namespace,
        account=account_summary.selected_account,
        symbol=context.symbol,
        region=context.region,
        plan=plan,
        user_mark=execution_key,
        external_id=execution_key,
    )
    order_id = trade_client.place_order(order)
    upsert_execution_record(
        ledger_path=ledger_path,
        execution_key=execution_key,
        order_id=order_id,
        account=account_summary.selected_account,
        trade_date=snapshot.next_trade_date,
        signal_bar_date=snapshot.signal_bar_date,
        status="Submitted",
        filled_quantity=0.0,
        avg_fill_price=None,
    )
    result = poll_tiger_order_to_terminal(
        trade_client=trade_client,
        account=account_summary.selected_account,
        order_id=order_id,
        deadline=deadline,
        poll_seconds=args.order_poll_sec,
        ledger_path=ledger_path,
        execution_key=execution_key,
        signal_bar_date=snapshot.signal_bar_date,
        trade_date=snapshot.next_trade_date,
    )
    submission_payload = {
        "account": account_summary.selected_account,
        "account_type": account_summary.selected_account_type,
        "signal_bar_date": snapshot.signal_bar_date,
        "trade_date": snapshot.next_trade_date,
        "execution_key": execution_key,
        "submission_deadline": trade_window["submission_deadline"].isoformat(),
        "trade_plan": plan.to_dict(),
        "quote_snapshot": quote_snapshot.to_dict(),
        **result,
    }
    (output_dir / "tiger_paper_auto_submission.json").write_text(json.dumps(submission_payload, indent=2, sort_keys=True), encoding="utf-8")
    print("Submitted Tiger paper order:", order_id)
    print("Submission artifact:", output_dir / "tiger_paper_auto_submission.json")


if __name__ == "__main__":
    main()
