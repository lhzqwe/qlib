from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codex_trading.notifications import run_feishu_bot_service


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Feishu bot service for TradingBot.")
    parser.add_argument("--config", required=True, help="Path to the Feishu bot YAML config.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_feishu_bot_service(args.config)


if __name__ == "__main__":
    main()
