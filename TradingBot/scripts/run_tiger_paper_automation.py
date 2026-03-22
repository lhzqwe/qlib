from __future__ import annotations

import argparse
from datetime import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codex_trading import get_repo_layout
from codex_trading.tradingbot.automation_runtime import execute_automation_session
from codex_trading.tradingbot.runtime_config import load_tradingbot_runtime_config
from codex_trading.tradingbot.tiger import connect_tiger_clients


LAYOUT = get_repo_layout()


def _load_deployment_manifest(strategy_id: str) -> dict:
    manifest_path = LAYOUT.strategies_root / strategy_id / "deployment_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError("Strategy deployment manifest is missing: %s" % manifest_path)
    return __import__("json").loads(manifest_path.read_text(encoding="utf-8"))


def _default_output_dir(strategy_id: str) -> Path:
    now = datetime.now()
    return LAYOUT.tradingbot_saved_root / strategy_id / now.strftime("%Y%m%d") / now.strftime("%H%M%S") / "tiger_paper_auto"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the live TradingBot paper automation for a published strategy.")
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--runtime-config", default=str(LAYOUT.tradingbot_local_config_root / "tradingbot.runtime.yml"))
    parser.add_argument("--signal-start-date", default=None)
    parser.add_argument("--signal-end-date", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--submit", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    deployment_manifest = _load_deployment_manifest(args.strategy_id)
    live_dir = Path(deployment_manifest["live_dir"]).expanduser().resolve()
    runtime = load_tradingbot_runtime_config(args.runtime_config)
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else _default_output_dir(args.strategy_id)
    tiger_namespace, config_obj, quote_client, trade_client = connect_tiger_clients(runtime.tiger_config)
    namespace_args = SimpleNamespace(
        strategy_run_dir=str(live_dir),
        runs_root=str(LAYOUT.autoresearch_runs_root),
        tiger_config=runtime.tiger_config,
        paper_account=runtime.paper_account,
        signal_start_date=args.signal_start_date,
        signal_end_date=args.signal_end_date,
        order_type=runtime.order_type,
        limit_price=runtime.limit_price,
        cash_buffer_pct=runtime.cash_buffer_pct,
        output_dir=str(output_dir),
        execution_window_minutes=runtime.execution_window_minutes,
        max_preopen_wait_minutes=runtime.max_preopen_wait_minutes,
        order_poll_sec=runtime.order_poll_sec,
        max_quote_staleness_sec=runtime.max_quote_staleness_sec,
        bars_lookback_buffer=runtime.bars_lookback_buffer,
        feishu_config=runtime.feishu_config,
        submit=bool(args.submit),
    )
    execute_automation_session(
        namespace_args,
        run_dir=live_dir,
        output_dir=output_dir,
        tiger_namespace=tiger_namespace,
        config_obj=config_obj,
        quote_client=quote_client,
        trade_client=trade_client,
    )


if __name__ == "__main__":
    main()
