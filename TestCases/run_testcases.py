from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codex_trading import get_repo_layout


LAYOUT = get_repo_layout()

MODULE_PATHS = {
    "notifications": ["TestCases/unit/notifications"],
    "autoresearch": ["TestCases/unit/autoresearch"],
    "qlib_adapter": ["TestCases/unit/qlib_adapter"],
    "tradingbot": ["TestCases/unit/tradingbot", "TestCases/e2e"],
    "deployment": ["TestCases/unit/deployment", "TestCases/integration/test_publish_flow.py"],
}

SUITE_PATHS = {
    "unit": [
        "TestCases/unit/notifications",
        "TestCases/unit/autoresearch",
        "TestCases/unit/qlib_adapter",
        "TestCases/unit/tradingbot",
        "TestCases/unit/deployment",
    ],
    "integration": ["TestCases/integration"],
    "e2e": ["TestCases/e2e"],
    "smoke-release": ["TestCases/unit/deployment/test_release_flow.py", "TestCases/integration/test_publish_flow.py"],
}


def _dedupe(items):
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _resolve_targets(suite: str, module: str):
    if suite == "all":
        targets = []
        for name in ("unit", "integration", "e2e"):
            targets.extend(SUITE_PATHS[name])
    else:
        targets = list(SUITE_PATHS[suite])
    if module != "all":
        module_targets = MODULE_PATHS[module]
        targets = [item for item in targets if any(item.startswith(module_item) or module_item.startswith(item) for module_item in module_targets)]
        if not targets:
            targets = list(module_targets)
    return _dedupe(targets)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run TestCases suites with module filtering.")
    parser.add_argument("--suite", default="all", choices=["all", "unit", "integration", "e2e", "smoke-release"])
    parser.add_argument("--module", default="all", choices=["all", "notifications", "autoresearch", "qlib_adapter", "tradingbot", "deployment"])
    parser.add_argument("--keyword", default=None)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--strategy-id", default=None)
    parser.add_argument("--run-id", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    targets = _resolve_targets(args.suite, args.module)
    if args.list:
        print(json.dumps({"suite": args.suite, "module": args.module, "targets": targets}, indent=2))
        return

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else LAYOUT.testcase_output_root / ("%s_%s" % (datetime.now().strftime("%Y%m%d_%H%M%S"), args.suite))
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "pytest", *targets]
    if args.keyword:
        command.extend(["-k", args.keyword])
    result = subprocess.run(command, cwd=str(LAYOUT.repo_root), capture_output=True, text=True)
    (output_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (output_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "suite": args.suite,
                "module": args.module,
                "targets": targets,
                "strategy_id": args.strategy_id,
                "run_id": args.run_id,
                "returncode": result.returncode,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
