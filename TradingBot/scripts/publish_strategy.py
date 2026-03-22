from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codex_trading import get_repo_layout
from codex_trading.deployment import build_strategy_package, publish_strategy_release


LAYOUT = get_repo_layout()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a Strategy Package from an AutoResearch run and publish it to TradingBot.")
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--package-id", default=None)
    parser.add_argument("--package-label", default=None)
    parser.add_argument("--instrument", action="append", default=None)
    parser.add_argument("--automation-id", default=None)
    parser.add_argument("--automation-template", default=None)
    parser.add_argument("--notification-profile", default=str(LAYOUT.tradingbot_local_config_root / "feishu_bot.local.yml"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_ref = args.run_dir or args.run_id
    if not run_ref and not args.package_id:
        raise ValueError("publish_strategy requires --run-id/--run-dir or an existing --package-id.")
    if args.package_id:
        package_id = args.package_id
    else:
        package_manifest = build_strategy_package(
            run_ref,
            strategy_id=args.strategy_id,
            package_label=args.package_label,
            instrument_universe=args.instrument,
            automation_id=args.automation_id,
        )
        package_id = package_manifest.package_id
        print("Built package:", json.dumps(package_manifest.to_dict(), indent=2, ensure_ascii=False))
    release_manifest = publish_strategy_release(
        strategy_id=args.strategy_id,
        package_id=package_id,
        automation_template=args.automation_template,
        notification_profile=args.notification_profile,
    )
    print("Published release:", json.dumps(release_manifest.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
