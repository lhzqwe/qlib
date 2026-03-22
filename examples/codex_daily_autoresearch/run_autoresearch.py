from __future__ import annotations

import argparse
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from research_core import ResearchConfig, run_autoresearch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Codex-driven daily single-stock autoresearch for Qlib.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--region", default="us")
    parser.add_argument("--provider-uri", default="~/.qlib/qlib_data/us_data")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--benchmark-symbol", default=None)
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    parser.add_argument("--llm-model", default="gpt-5.4")
    parser.add_argument("--auth-profile-id", default=None)
    parser.add_argument("--max-rounds", type=int, default=8)
    parser.add_argument("--candidates-per-round", type=int, default=6)
    parser.add_argument("--patience", type=int, default=2)
    parser.add_argument("--time-budget-sec", type=int, default=900)
    parser.add_argument("--output-dir", default=str(SCRIPT_DIR / "output"))
    parser.add_argument("--auto-download-us-data", dest="auto_download_us_data", action="store_true")
    parser.add_argument("--no-auto-download-us-data", dest="auto_download_us_data", action="store_false")
    parser.set_defaults(auto_download_us_data=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = ResearchConfig(
        symbol=args.symbol.upper(),
        region=args.region,
        provider_uri=args.provider_uri,
        start_date=args.start_date,
        end_date=args.end_date,
        benchmark_symbol=args.benchmark_symbol.upper() if isinstance(args.benchmark_symbol, str) and args.benchmark_symbol else None,
        initial_cash=args.initial_cash,
        llm_model=args.llm_model,
        auth_profile_id=args.auth_profile_id,
        max_rounds=args.max_rounds,
        candidates_per_round=args.candidates_per_round,
        patience=args.patience,
        time_budget_sec=args.time_budget_sec,
        output_dir=args.output_dir,
        auto_download_us_data=bool(args.auto_download_us_data),
    )
    result = run_autoresearch(config)
    print("Output directory:", result["output_dir"])
    print("Winner:", result["winner_name"])
    print("Winner score:", result["winner_score"])


if __name__ == "__main__":
    main()
