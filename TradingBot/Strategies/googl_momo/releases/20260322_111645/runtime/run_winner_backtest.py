from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent
for candidate in [RUN_DIR]:
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import winner_strategy
from codex_trading.autoresearch.service import ResearchConfig, run_frozen_winner_backtest


FROZEN_CONFIG = json.loads('{\n  "auto_download_us_data": true,\n  "benchmark_symbol": "QQQ",\n  "end_date": "2026-03-22",\n  "initial_cash": 100000.0,\n  "provider_uri": "~/.qlib/qlib_data/us_data",\n  "region": "us",\n  "start_date": "2024-03-22",\n  "symbol": "GOOGL"\n}')


def parse_args():
    parser = argparse.ArgumentParser(description="Replay the exported Codex daily autoresearch winner with Qlib.")
    parser.add_argument("--provider-uri", default=FROZEN_CONFIG["provider_uri"])
    parser.add_argument("--output-dir", default=str(RUN_DIR / "rerun"))
    parser.add_argument("--auto-download-us-data", dest="auto_download_us_data", action="store_true")
    parser.add_argument("--no-auto-download-us-data", dest="auto_download_us_data", action="store_false")
    parser.set_defaults(auto_download_us_data=bool(FROZEN_CONFIG["auto_download_us_data"]))
    return parser.parse_args()


def main():
    args = parse_args()
    config = ResearchConfig(
        symbol=FROZEN_CONFIG["symbol"],
        region=FROZEN_CONFIG["region"],
        provider_uri=args.provider_uri,
        start_date=FROZEN_CONFIG["start_date"],
        end_date=FROZEN_CONFIG["end_date"],
        benchmark_symbol=FROZEN_CONFIG["benchmark_symbol"],
        initial_cash=float(FROZEN_CONFIG["initial_cash"]),
        output_dir=args.output_dir,
        auto_download_us_data=bool(args.auto_download_us_data),
    )
    result = run_frozen_winner_backtest(
        config=config,
        winner_spec=winner_strategy.get_spec(),
        output_dir=Path(args.output_dir),
    )
    print("Winner replay output:", result["output_dir"])


if __name__ == "__main__":
    main()
