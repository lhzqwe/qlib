import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from feishu_bot import (
    enqueue_feishu_error,
    enqueue_feishu_preview,
    enqueue_feishu_submission,
)
from tiger_bridge import (
    assess_execution_guard,
    build_execution_key,
    build_live_strategy_snapshot,
    build_tiger_account_summary,
    build_tiger_portfolio_snapshot,
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
    parser.add_argument("--feishu-config", default=None)
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


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _maybe_notify(
    notifier: Callable[..., bool],
    config_path: Optional[str],
    *,
    run_dir: Path,
    tiger_config_path: str,
) -> bool:
    if not config_path:
        return False
    return notifier(
        config_path,
        strategy_run_dir_override=str(run_dir),
        tiger_config_override=tiger_config_path,
    )


def _load_daily_state_row(run_dir: Path, trade_date: str) -> Optional[Dict[str, Any]]:
    path = Path(run_dir).expanduser().resolve() / "daily_state.csv"
    if not path.exists():
        return None
    frame = pd.read_csv(path)
    if "date" not in frame.columns:
        return None
    matched = frame.loc[frame["date"].astype(str) == str(trade_date)]
    if matched.empty:
        return None
    return {str(key): value for key, value in matched.iloc[-1].to_dict().items()}


def _load_completed_trade_row(run_dir: Path, trade_date: str) -> Optional[Dict[str, Any]]:
    path = Path(run_dir).expanduser().resolve() / "completed_trades.csv"
    if not path.exists():
        return None
    frame = pd.read_csv(path)
    if "exit_date" not in frame.columns:
        return None
    matched = frame.loc[frame["exit_date"].astype(str) == str(trade_date)]
    if matched.empty:
        return None
    return {str(key): value for key, value in matched.iloc[-1].to_dict().items()}


def _portfolio_position_quantity(portfolio_snapshot: Any, symbol: str) -> Optional[float]:
    positions = list(getattr(portfolio_snapshot, "positions", ()) or ())
    normalized_symbol = str(symbol or "").upper()
    total_quantity = 0.0
    matched = False
    for item in positions:
        if str(getattr(item, "symbol", "") or "").upper() != normalized_symbol:
            continue
        total_quantity += float(getattr(item, "quantity", 0.0) or 0.0)
        matched = True
    if not matched:
        return 0.0
    return total_quantity


def build_trade_summary_payload(
    *,
    run_dir: Path,
    submission_payload: Dict[str, Any],
    portfolio_snapshot: Optional[Any] = None,
) -> Dict[str, Any]:
    resolved_run_dir = Path(run_dir).expanduser().resolve()
    trade_date = str(submission_payload.get("trade_date") or "")
    symbol = str(submission_payload.get("symbol") or "")
    trade_plan = dict(submission_payload.get("trade_plan") or {})
    action = str(trade_plan.get("action") or submission_payload.get("action") or "").lower()
    base_currency = "USD"
    end_of_day_cash = None
    end_of_day_position = None
    end_of_day_account_value = None

    if portfolio_snapshot is not None:
        base_currency = str(getattr(portfolio_snapshot, "base_currency", None) or base_currency)
        end_of_day_cash = _coerce_float(getattr(portfolio_snapshot, "available_cash", None))
        end_of_day_position = _portfolio_position_quantity(portfolio_snapshot, symbol)
        end_of_day_account_value = _coerce_float(getattr(portfolio_snapshot, "net_liquidation", None))

    daily_state_row = _load_daily_state_row(resolved_run_dir, trade_date)
    if daily_state_row:
        end_of_day_cash = _coerce_float(daily_state_row.get("cash"))
        end_of_day_position = _coerce_float(daily_state_row.get("position_amount"))
        end_of_day_account_value = _coerce_float(daily_state_row.get("account"))

    position_status = "updated"
    if action == "buy":
        position_status = "opened" if (end_of_day_position or 0.0) > 0 else "flat"
    elif action == "sell":
        if end_of_day_position is not None and float(end_of_day_position) <= 0.0:
            position_status = "closed"
        else:
            position_status = "reduced"

    summary_payload = {
        "run_dir": str(resolved_run_dir),
        "symbol": symbol,
        "strategy_name": submission_payload.get("strategy_name"),
        "trade_date": trade_date,
        "signal_bar_date": submission_payload.get("signal_bar_date"),
        "action": action or None,
        "trade_plan": trade_plan,
        "planned_quantity": trade_plan.get("quantity"),
        "order_type": trade_plan.get("order_type"),
        "order_id": submission_payload.get("order_id"),
        "final_order_status": submission_payload.get("final_order_status") or submission_payload.get("status"),
        "filled_quantity": submission_payload.get("filled_quantity"),
        "avg_fill_price": submission_payload.get("avg_fill_price"),
        "base_currency": base_currency,
        "end_of_day_cash": end_of_day_cash,
        "end_of_day_position": end_of_day_position,
        "end_of_day_account_value": end_of_day_account_value,
        "position_status": position_status,
    }

    completed_trade_row = _load_completed_trade_row(resolved_run_dir, trade_date) if action == "sell" else None
    if completed_trade_row:
        summary_payload.update(
            {
                "realized_pnl": _coerce_float(completed_trade_row.get("pnl")),
                "return_pct": _coerce_float(completed_trade_row.get("return_pct")),
                "entry_date": completed_trade_row.get("entry_date"),
                "exit_date": completed_trade_row.get("exit_date"),
                "entry_signal_date": completed_trade_row.get("entry_signal_date"),
                "exit_signal_date": completed_trade_row.get("exit_signal_date"),
            }
        )
    return summary_payload


@dataclass(frozen=True)
class AutomationSessionHooks:
    resolve_market_status: Callable[[Any, Any], Any]
    determine_trade_window: Callable[[Any, int, int], Dict[str, Any]]
    sleep_fn: Callable[[float], None]
    load_context: Callable[..., Any]
    build_snapshot: Callable[..., Any]
    build_account_summary: Callable[..., Any]
    fetch_quote_snapshot: Callable[..., Any]
    build_trade_plan: Callable[..., Any]
    build_execution_key: Callable[..., str]
    fetch_symbol_orders: Callable[..., Any]
    assess_execution_guard: Callable[..., Any]
    create_order: Callable[..., Any]
    place_order: Callable[[Any, Any], int]
    upsert_execution_record: Callable[..., Any]
    poll_order: Callable[..., Dict[str, Any]]
    build_portfolio_snapshot: Callable[..., Any]
    notify_preview: Callable[..., bool]
    notify_submission: Callable[..., bool]
    notify_summary: Callable[..., bool]


@dataclass(frozen=True)
class AutomationSessionResult:
    run_dir: Path
    output_dir: Path
    preview_payload: Dict[str, Any]
    submission_payload: Optional[Dict[str, Any]] = None
    summary_payload: Optional[Dict[str, Any]] = None
    execution_key: Optional[str] = None
    order_id: Optional[int] = None
    final_order_status: Optional[str] = None
    order_submitted: bool = False


def _default_session_hooks() -> AutomationSessionHooks:
    return AutomationSessionHooks(
        resolve_market_status=_resolve_us_market_status,
        determine_trade_window=_determine_trade_window,
        sleep_fn=time.sleep,
        load_context=load_frozen_strategy_context,
        build_snapshot=build_live_strategy_snapshot,
        build_account_summary=build_tiger_account_summary,
        fetch_quote_snapshot=fetch_tiger_quote_snapshot,
        build_trade_plan=build_tiger_trade_plan,
        build_execution_key=build_execution_key,
        fetch_symbol_orders=fetch_tiger_symbol_orders,
        assess_execution_guard=assess_execution_guard,
        create_order=create_tiger_order,
        place_order=lambda trade_client, order: int(trade_client.place_order(order)),
        upsert_execution_record=upsert_execution_record,
        poll_order=poll_tiger_order_to_terminal,
        build_portfolio_snapshot=build_tiger_portfolio_snapshot,
        notify_preview=enqueue_feishu_preview,
        notify_submission=enqueue_feishu_submission,
        notify_summary=lambda *args, **kwargs: False,
    )


def execute_automation_session(
    args: argparse.Namespace,
    *,
    run_dir: Path,
    output_dir: Path,
    tiger_namespace: Dict[str, Any],
    config_obj: Any,
    quote_client: Any,
    trade_client: Any,
    hooks: Optional[AutomationSessionHooks] = None,
) -> AutomationSessionResult:
    hooks = hooks or _default_session_hooks()
    run_dir = Path(run_dir).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = output_dir / "execution_ledger.json"

    market_status = hooks.resolve_market_status(quote_client, tiger_namespace["Market"].US)
    trade_window = hooks.determine_trade_window(
        market_status=market_status,
        execution_window_minutes=args.execution_window_minutes,
        max_preopen_wait_minutes=args.max_preopen_wait_minutes,
    )

    if args.submit and trade_window["wait_seconds"] > 0:
        print("Waiting %d seconds for the US regular open." % round(trade_window["wait_seconds"]))
        hooks.sleep_fn(float(trade_window["wait_seconds"]))
        market_status = hooks.resolve_market_status(quote_client, tiger_namespace["Market"].US)
        trade_window = hooks.determine_trade_window(
            market_status=market_status,
            execution_window_minutes=args.execution_window_minutes,
            max_preopen_wait_minutes=args.max_preopen_wait_minutes,
        )

    trade_date = args.signal_end_date or trade_window["trade_date"]
    if args.submit and trade_date != trade_window["trade_date"]:
        raise RuntimeError("signal_end_date must match the current Tiger trade_date when --submit is used.")

    context = hooks.load_context(
        run_dir=run_dir,
        signal_start_date=args.signal_start_date,
        signal_end_date=trade_date,
    )
    snapshot = hooks.build_snapshot(
        context=context,
        output_dir=output_dir,
        executable_trade_date=trade_date,
        tiger_namespace=tiger_namespace,
        quote_client=quote_client,
        bars_lookback_buffer=args.bars_lookback_buffer,
    )
    account_summary = hooks.build_account_summary(
        trade_client=trade_client,
        config_account=getattr(config_obj, "account", None),
        symbol=context.symbol,
        region=context.region,
        requested_paper_account=args.paper_account,
    )
    quote_snapshot = hooks.fetch_quote_snapshot(
        quote_client=quote_client,
        symbol=context.symbol,
        allow_delayed_preview=True,
    )
    plan = hooks.build_trade_plan(
        snapshot=snapshot,
        account_summary=account_summary,
        order_type=args.order_type,
        quote_snapshot=quote_snapshot,
        cash_buffer_pct=args.cash_buffer_pct,
        limit_price=args.limit_price,
        max_quote_staleness_sec=args.max_quote_staleness_sec,
        require_realtime_quote=True,
    )
    execution_key = hooks.build_execution_key(
        paper_account=account_summary.selected_account,
        symbol=context.symbol,
        strategy_name=context.winner_spec.name,
        trade_date=snapshot.next_trade_date,
        signal_bar_date=snapshot.signal_bar_date,
        action=plan.action if plan.action in {"buy", "sell"} else "hold",
    )
    open_orders, historical_orders = hooks.fetch_symbol_orders(
        trade_client=trade_client,
        account=account_summary.selected_account,
        symbol=context.symbol,
        region=context.region,
        trade_date=snapshot.next_trade_date,
    )
    guard = hooks.assess_execution_guard(
        execution_key=execution_key,
        symbol=context.symbol,
        action=plan.action.upper() if plan.action else "HOLD",
        open_orders=open_orders,
        historical_orders=historical_orders,
        ledger_record=get_execution_record(ledger_path, execution_key),
    )

    preview_payload = {
        "run_dir": str(run_dir),
        "symbol": context.symbol,
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
    preview_path = output_dir / "tiger_paper_auto_preview.json"
    _write_json(preview_path, preview_payload)
    _maybe_notify(
        hooks.notify_preview,
        args.feishu_config,
        run_dir=run_dir,
        tiger_config_path=args.tiger_config,
    )

    print("Strategy run:", run_dir)
    print("Tiger paper account:", mask_account(account_summary.selected_account))
    print("Market status:", json.dumps(preview_payload["market_status"], ensure_ascii=True, sort_keys=True))
    print("Trade window:", json.dumps(preview_payload["trade_window"], ensure_ascii=True, sort_keys=True))
    print("Signal:", snapshot.next_session_action or "hold", "for", snapshot.next_trade_date)
    print("Plan:", json.dumps(plan.to_dict(), ensure_ascii=True, sort_keys=True))
    print("Execution guard:", json.dumps(guard.to_dict(), ensure_ascii=True, sort_keys=True, default=str))
    print("Preview artifact:", preview_path)

    result = AutomationSessionResult(
        run_dir=run_dir,
        output_dir=output_dir,
        preview_payload=preview_payload,
        execution_key=execution_key,
    )

    def _should_notify_submission(payload: Optional[Dict[str, Any]]) -> bool:
        if not payload:
            return False
        filled_quantity = _coerce_float(payload.get("filled_quantity"))
        return filled_quantity is not None and filled_quantity > 0.0

    if not args.submit:
        print("Submission skipped. Re-run with --submit to auto-place the Tiger paper order.")
        return result
    if account_summary.selected_account_type.upper() != "PAPER":
        raise RuntimeError("Refusing to submit because the selected Tiger account is not PAPER.")
    if not trade_window["can_trade_today"]:
        print("Submission skipped because the market window is not actionable:", trade_window["reason"])
        return result
    if not plan.can_submit or plan.action not in {"buy", "sell"}:
        print("Submission skipped because there is no executable paper order plan.")
        return result

    deadline = trade_window["submission_deadline"].astimezone(timezone.utc)
    if guard.decision == "adopt_existing_order":
        order_id = int(guard.matched_order["order_id"])
        order_result = hooks.poll_order(
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
            "symbol": context.symbol,
            "strategy_name": context.winner_spec.name,
            "signal_bar_date": snapshot.signal_bar_date,
            "trade_date": snapshot.next_trade_date,
            "execution_key": execution_key,
            "submission_deadline": trade_window["submission_deadline"].isoformat(),
            "trade_plan": plan.to_dict(),
            "quote_snapshot": quote_snapshot.to_dict(),
            "guard": guard.to_dict(),
            **order_result,
        }
        submission_path = output_dir / "tiger_paper_auto_submission.json"
        _write_json(submission_path, submission_payload)
        if _should_notify_submission(submission_payload):
            _maybe_notify(
                hooks.notify_submission,
                args.feishu_config,
                run_dir=run_dir,
                tiger_config_path=args.tiger_config,
            )
        portfolio_snapshot = None
        try:
            portfolio_snapshot = hooks.build_portfolio_snapshot(
                trade_client=trade_client,
                config_account=getattr(config_obj, "account", None),
                requested_paper_account=args.paper_account,
            )
        except Exception as exc:
            print("Post-trade portfolio snapshot failed: %s" % exc, file=sys.stderr)
        summary_payload = build_trade_summary_payload(
            run_dir=run_dir,
            submission_payload=submission_payload,
            portfolio_snapshot=portfolio_snapshot,
        )
        summary_path = output_dir / "tiger_paper_auto_summary.json"
        _write_json(summary_path, summary_payload)
        print("Adopted existing Tiger paper order:", order_id)
        print("Submission artifact:", submission_path)
        print("Summary artifact:", summary_path)
        return AutomationSessionResult(
            run_dir=run_dir,
            output_dir=output_dir,
            preview_payload=preview_payload,
            submission_payload=submission_payload,
            summary_payload=summary_payload,
            execution_key=execution_key,
            order_id=order_id,
            final_order_status=str(submission_payload.get("final_order_status") or ""),
            order_submitted=False,
        )

    if guard.decision != "submit_new_order":
        print("Submission skipped because execution_guard=%s." % guard.reason)
        return result

    order = hooks.create_order(
        tiger_namespace=tiger_namespace,
        account=account_summary.selected_account,
        symbol=context.symbol,
        region=context.region,
        plan=plan,
        user_mark=execution_key,
        external_id=execution_key,
    )
    order_id = hooks.place_order(trade_client, order)
    hooks.upsert_execution_record(
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
    order_result = hooks.poll_order(
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
        "symbol": context.symbol,
        "strategy_name": context.winner_spec.name,
        "signal_bar_date": snapshot.signal_bar_date,
        "trade_date": snapshot.next_trade_date,
        "execution_key": execution_key,
        "submission_deadline": trade_window["submission_deadline"].isoformat(),
        "trade_plan": plan.to_dict(),
        "quote_snapshot": quote_snapshot.to_dict(),
        **order_result,
    }
    submission_path = output_dir / "tiger_paper_auto_submission.json"
    _write_json(submission_path, submission_payload)
    if _should_notify_submission(submission_payload):
        _maybe_notify(
            hooks.notify_submission,
            args.feishu_config,
            run_dir=run_dir,
            tiger_config_path=args.tiger_config,
        )
    portfolio_snapshot = None
    try:
        portfolio_snapshot = hooks.build_portfolio_snapshot(
            trade_client=trade_client,
            config_account=getattr(config_obj, "account", None),
            requested_paper_account=args.paper_account,
        )
    except Exception as exc:
        print("Post-trade portfolio snapshot failed: %s" % exc, file=sys.stderr)
    summary_payload = build_trade_summary_payload(
        run_dir=run_dir,
        submission_payload=submission_payload,
        portfolio_snapshot=portfolio_snapshot,
    )
    summary_path = output_dir / "tiger_paper_auto_summary.json"
    _write_json(summary_path, summary_payload)
    print("Submitted Tiger paper order:", order_id)
    print("Submission artifact:", submission_path)
    print("Summary artifact:", summary_path)
    return AutomationSessionResult(
        run_dir=run_dir,
        output_dir=output_dir,
        preview_payload=preview_payload,
        submission_payload=submission_payload,
        summary_payload=summary_payload,
        execution_key=execution_key,
        order_id=order_id,
        final_order_status=str(submission_payload.get("final_order_status") or ""),
        order_submitted=True,
    )


def main() -> None:
    args = parse_args()
    run_dir = None
    try:
        run_dir = Path(args.strategy_run_dir).expanduser().resolve() if args.strategy_run_dir else find_latest_run_dir(args.runs_root)
        output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else (run_dir / "tiger_paper_auto")
        tiger_namespace, config_obj, quote_client, trade_client = connect_tiger_clients(args.tiger_config)
        execute_automation_session(
            args,
            run_dir=run_dir,
            output_dir=output_dir,
            tiger_namespace=tiger_namespace,
            config_obj=config_obj,
            quote_client=quote_client,
            trade_client=trade_client,
        )
    except Exception as exc:
        if args.feishu_config:
            enqueue_feishu_error(
                args.feishu_config,
                strategy_run_dir_override=str(run_dir) if run_dir else None,
                tiger_config_override=args.tiger_config,
                error=str(exc),
                stage="run_tiger_paper_automation",
            )
        raise


if __name__ == "__main__":
    main()
