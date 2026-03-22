from __future__ import annotations

import argparse
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from feishu_bot import run_feishu_bot_service


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Feishu app bot service for Codex daily autoresearch.")
    parser.add_argument("--config", required=True, help="Path to the Feishu bot YAML config.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_feishu_bot_service(args.config)


if __name__ == "__main__":
    main()
