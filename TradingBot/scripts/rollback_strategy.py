from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codex_trading.deployment import rollback_strategy_release


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rollback a TradingBot strategy to a previously published release.")
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--release-id", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = rollback_strategy_release(args.strategy_id, args.release_id)
    print("Rollback complete:", json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
