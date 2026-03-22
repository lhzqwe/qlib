from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codex_trading.autoresearch import ResearchConfig, run_autoresearch
from codex_trading import get_repo_layout
from codex_trading.deployment import build_strategy_package, publish_strategy_release


LAYOUT = get_repo_layout()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AutoResearch and optionally publish the winner to TradingBot.")
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--region", default="us")
    parser.add_argument("--provider-uri", default="~/.qlib/qlib_data/us_data")
    parser.add_argument("--benchmark-symbol", default=None)
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    parser.add_argument("--llm-model", default="gpt-5.4")
    parser.add_argument("--auth-profile-id", default=None)
    parser.add_argument("--max-rounds", type=int, default=8)
    parser.add_argument("--candidates-per-round", type=int, default=6)
    parser.add_argument("--patience", type=int, default=2)
    parser.add_argument("--time-budget-sec", type=int, default=900)
    parser.add_argument("--output-dir", default=str(LAYOUT.autoresearch_runs_root))
    parser.add_argument("--package-label", default=None)
    parser.add_argument("--automation-id", default=None)
    parser.add_argument("--notification-profile", default=str(LAYOUT.tradingbot_local_config_root / "feishu_bot.local.yml"))
    parser.add_argument("--publish", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_autoresearch(
        ResearchConfig(
            symbol=args.symbol.upper(),
            region=args.region,
            provider_uri=args.provider_uri,
            start_date=args.start_date,
            end_date=args.end_date,
            benchmark_symbol=args.benchmark_symbol.upper() if args.benchmark_symbol else None,
            initial_cash=args.initial_cash,
            llm_model=args.llm_model,
            auth_profile_id=args.auth_profile_id,
            max_rounds=args.max_rounds,
            candidates_per_round=args.candidates_per_round,
            patience=args.patience,
            time_budget_sec=args.time_budget_sec,
            output_dir=args.output_dir,
        )
    )
    print("Research result:", json.dumps(result, indent=2, ensure_ascii=False))
    package_manifest = build_strategy_package(
        result["output_dir"],
        strategy_id=args.strategy_id,
        package_label=args.package_label,
        instrument_universe=[args.symbol.upper()],
        automation_id=args.automation_id,
    )
    print("Built package:", json.dumps(package_manifest.to_dict(), indent=2, ensure_ascii=False))
    if args.publish:
        release_manifest = publish_strategy_release(
            strategy_id=args.strategy_id,
            package_id=package_manifest.package_id,
            notification_profile=args.notification_profile,
        )
        print("Published release:", json.dumps(release_manifest.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
