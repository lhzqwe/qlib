from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from ..layout import get_repo_layout
from ..notifications.feishu import enqueue_feishu_error, enqueue_feishu_preview
from .tiger import (
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
    upsert_execution_record,
)


LAYOUT = get_repo_layout()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tiger-only single-shot paper execution for a frozen Codex daily winner.")
    parser.add_argument("--strategy-run-dir", default=None, help="Autoresearch output directory containing winner_strategy.py.")
    parser.add_argument("--runs-root", default=str(LAYOUT.autoresearch_runs_root), help="Search root when --strategy-run-dir is omitted.")
    parser.add_argument("--tiger-config", required=True, help="Path to tiger_openapi_config.properties or its parent directory.")
    parser.add_argument("--paper-account", default=None, help="Optional 17-digit Tiger paper account override.")
    parser.add_argument("--signal-start-date", default=None, help="Override the frozen signal start date.")
    parser.add_argument("--signal-end-date", default=datetime.now().strftime("%Y-%m-%d"), help="Trade date to prepare against, default is today.")
    parser.add_argument("--order-type", choices=["market", "limit"], default="market")
    parser.add_argument("--limit-price", type=float, default=None)
    parser.add_argument("--cash-buffer-pct", type=float, default=0.98, help="Buy-side cash haircut to reduce rejects.")
    parser.add_argument("--max-quote-staleness-sec", type=int, default=15)
    parser.add_argument("--bars-lookback-buffer", type=int, default=30)
    parser.add_argument("--output-dir", default=None, help="Directory for preview/submission artifacts.")
    parser.add_argument("--feishu-config", default=None, help="Optional YAML config for Feishu push notifications.")
    parser.add_argument("--submit", action="store_true", help="Actually place the Tiger paper order.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_dir = None
    try:
        run_dir = Path(args.strategy_run_dir).expanduser().resolve() if args.strategy_run_dir else find_latest_run_dir(args.runs_root)
        output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else (run_dir / "tiger_paper")
        output_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = output_dir / "execution_ledger.json"

        tiger_namespace, config_obj, quote_client, trade_client = connect_tiger_clients(args.tiger_config)
        context = load_frozen_strategy_context(
            run_dir=run_dir,
            signal_start_date=args.signal_start_date,
            signal_end_date=args.signal_end_date,
        )
        snapshot = build_live_strategy_snapshot(
            context=context,
            output_dir=output_dir,
            executable_trade_date=args.signal_end_date,
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
            "symbol": context.symbol,
            "strategy_name": context.winner_spec.name,
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
        (output_dir / "tiger_paper_preview.json").write_text(json.dumps(preview_payload, indent=2, sort_keys=True), encoding="utf-8")
        if args.feishu_config:
            enqueue_feishu_preview(
                args.feishu_config,
                strategy_run_dir_override=str(run_dir),
                tiger_config_override=args.tiger_config,
                artifacts_dir_override=str(output_dir),
            )

        print("Strategy run:", run_dir)
        print("Tiger paper account:", mask_account(account_summary.selected_account))
        print("Signal bar:", snapshot.signal_bar_date, "=> trade date:", snapshot.next_trade_date)
        print("Signal:", snapshot.next_session_action or "hold")
        print("Plan:", json.dumps(plan.to_dict(), ensure_ascii=True, sort_keys=True))
        print("Execution guard:", json.dumps(guard.to_dict(), ensure_ascii=True, sort_keys=True, default=str))
        print("Preview artifact:", output_dir / "tiger_paper_preview.json")

        if not args.submit:
            print("Submission skipped. Re-run with --submit to place the Tiger paper order.")
            return
        if account_summary.selected_account_type.upper() != "PAPER":
            raise RuntimeError("Refusing to submit because the selected Tiger account is not PAPER.")
        if not plan.can_submit or plan.action not in {"buy", "sell"}:
            print("Submission skipped because the plan is not executable.")
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
        submission_payload = {
            "order_id": order_id,
            "account": account_summary.selected_account,
            "account_type": account_summary.selected_account_type,
            "status": "Submitted",
            "symbol": context.symbol,
            "strategy_name": context.winner_spec.name,
            "signal_bar_date": snapshot.signal_bar_date,
            "trade_date": snapshot.next_trade_date,
            "execution_key": execution_key,
            "quote_snapshot": quote_snapshot.to_dict(),
            "trade_plan": plan.to_dict(),
        }
        (output_dir / "tiger_paper_submission.json").write_text(json.dumps(submission_payload, indent=2, sort_keys=True), encoding="utf-8")
        print("Submitted Tiger paper order:", order_id)
        print("Submission artifact:", output_dir / "tiger_paper_submission.json")
    except Exception as exc:
        if args.feishu_config:
            enqueue_feishu_error(
                args.feishu_config,
                strategy_run_dir_override=str(run_dir) if run_dir else None,
                tiger_config_override=args.tiger_config,
                error=str(exc),
                stage="run_tiger_paper_entry",
            )
        raise


if __name__ == "__main__":
    main()
