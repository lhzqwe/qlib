from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codex_trading import get_repo_layout
from codex_trading.tradingbot.entry_runtime import main as _legacy_entry_main
from codex_trading.tradingbot.runtime_config import load_tradingbot_runtime_config


LAYOUT = get_repo_layout()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the live TradingBot paper entry flow for a published strategy.")
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--runtime-config", default=str(LAYOUT.tradingbot_local_config_root / "tradingbot.runtime.yml"))
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--signal-start-date", default=None)
    parser.add_argument("--signal-end-date", default=None)
    parser.add_argument("--output-dir", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest_path = LAYOUT.strategies_root / args.strategy_id / "deployment_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError("Strategy deployment manifest is missing: %s" % manifest_path)
    deployment_manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    live_dir = Path(deployment_manifest["live_dir"]).expanduser().resolve()
    runtime = load_tradingbot_runtime_config(args.runtime_config)
    argv = [
        sys.argv[0],
        "--strategy-run-dir",
        str(live_dir),
        "--tiger-config",
        runtime.tiger_config,
    ]
    if runtime.paper_account:
        argv.extend(["--paper-account", runtime.paper_account])
    if runtime.feishu_config:
        argv.extend(["--feishu-config", runtime.feishu_config])
    if args.signal_start_date:
        argv.extend(["--signal-start-date", args.signal_start_date])
    if args.signal_end_date:
        argv.extend(["--signal-end-date", args.signal_end_date])
    if args.output_dir:
        argv.extend(["--output-dir", args.output_dir])
    if args.submit:
        argv.append("--submit")
    old_argv = sys.argv[:]
    try:
        sys.argv = argv
        _legacy_entry_main()
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    main()
