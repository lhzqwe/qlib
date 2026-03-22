from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
KB_FILES = ("AGENTS.md", "docs/developer/llm_repo_kb.md")


@dataclass(frozen=True)
class Rule:
    pattern: str
    level: str
    sections: Tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Match:
    path: str
    pattern: str
    level: str
    sections: Tuple[str, ...]
    reason: str


RULES: Tuple[Rule, ...] = (
    Rule(".tmp_feishu/**", "ignore", tuple(), "temporary Feishu probe/runtime output"),
    Rule("Saved/**", "ignore", tuple(), "repo-local artifact output"),
    Rule("TradingBot/Saved/**", "ignore", tuple(), "TradingBot runtime artifacts"),
    Rule("TradingBot/config/local/**", "ignore", tuple(), "local gitignored runtime config"),
    Rule("TestCases/e2e/generated/**", "ignore", tuple(), "generated replay tests"),
    Rule("**/__pycache__/**", "ignore", tuple(), "python cache"),
    Rule("**/*.pyc", "ignore", tuple(), "python cache file"),
    Rule("AGENTS.md", "required", ("Start Here", "Commit-Time Knowledge Base Policy"), "agent bootstrap guidance changed"),
    Rule("docs/developer/llm_repo_kb.md", "required", ("Knowledge Base",), "knowledge base itself changed"),
    Rule("codex_trading/layout.py", "required", ("Formal Directory Map", "Repo Layers"), "repo layout contract changed"),
    Rule("codex_trading/autoresearch/**", "required", ("Stable Entry Points", "Workflow Graph", "Current Strategy Characteristics"), "AutoResearch behavior changed"),
    Rule("codex_trading/qlib_adapter/**", "required", ("Repo Layers", "Known Architectural Notes"), "Qlib adapter boundary changed"),
    Rule("codex_trading/deployment/**", "required", ("Workflow Graph", "Current Live State", "Notification and Automation Caveats"), "deployment/release behavior changed"),
    Rule("codex_trading/notifications/**", "required", ("Notification and Automation Caveats", "Stable Entry Points"), "notification behavior changed"),
    Rule("codex_trading/tradingbot/**", "required", ("Workflow Graph", "Current Strategy Characteristics", "Notification and Automation Caveats"), "live trading runtime changed"),
    Rule("TradingBot/scripts/**", "required", ("Stable Entry Points", "Workflow Graph"), "operator entrypoints changed"),
    Rule("TradingBot/README.md", "required", ("Formal Directory Map", "Stable Entry Points"), "TradingBot operator docs changed"),
    Rule("TradingBot/automations/templates/**", "required", ("Workflow Graph", "Notification and Automation Caveats"), "automation template changed"),
    Rule("TradingBot/config/templates/**", "required", ("Formal Directory Map", "Notification and Automation Caveats"), "runtime template config changed"),
    Rule("TradingBot/Strategies/**", "required", ("Current Live State", "Workflow Graph"), "published strategy state changed"),
    Rule("TestCases/run_testcases.py", "required", ("Testing Model", "Suggested Validation Before Commit"), "test controller changed"),
    Rule("TestCases/README.md", "required", ("Testing Model",), "test operator doc changed"),
    Rule("TestCases/manual/**", "required", ("Testing Model", "Suggested Validation Before Commit"), "manual validation flow changed"),
    Rule("pyproject.toml", "required", ("Repo Layers", "Suggested Validation Before Commit"), "package/dependency surface changed"),
    Rule("qlib/**", "recommended", ("Repo Layers", "Known Architectural Notes"), "upstream qlib behavior changed"),
    Rule("scripts/**", "recommended", ("Repo Layers",), "repo utility scripts changed"),
    Rule("TestCases/unit/**", "recommended", ("Testing Model",), "unit coverage changed"),
    Rule("TestCases/integration/**", "recommended", ("Testing Model",), "integration coverage changed"),
    Rule("TestCases/e2e/**", "recommended", ("Testing Model", "Workflow Graph"), "end-to-end coverage changed"),
    Rule("examples/**", "recommended", ("Known Architectural Notes",), "legacy/reference examples changed"),
    Rule(".gitignore", "recommended", ("Formal Directory Map", "Known Architectural Notes"), "tracked vs ignored path semantics changed"),
    Rule("README.md", "recommended", ("Repo Layers",), "top-level repo description changed"),
    Rule("docs/**", "recommended", ("Repo Layers",), "docs changed outside the KB"),
    Rule("tests/**", "recommended", ("Testing Model",), "legacy pytest coverage changed"),
)


def _normalize_path(path: str) -> str:
    normalized = str(path).replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _matches(pattern: str, path: str) -> bool:
    return PurePosixPath(path).match(pattern)


def _git_diff_names(*, staged: bool) -> List[str]:
    command = ["git", "diff", "--name-only", "--diff-filter=ACMR"]
    if staged:
        command.insert(2, "--cached")
    result = subprocess.run(command, cwd=str(REPO_ROOT), capture_output=True, text=True, check=True)
    return [_normalize_path(line) for line in result.stdout.splitlines() if _normalize_path(line)]


def _classify_paths(paths: Sequence[str]) -> Tuple[List[Match], List[str]]:
    matches: List[Match] = []
    unmatched: List[str] = []
    for raw_path in paths:
        path = _normalize_path(raw_path)
        if not path:
            continue
        matched_rules = [rule for rule in RULES if _matches(rule.pattern, path)]
        if any(rule.level == "ignore" for rule in matched_rules):
            continue
        if not matched_rules:
            unmatched.append(path)
            continue
        chosen = None
        for level in ("required", "recommended"):
            candidate = next((rule for rule in matched_rules if rule.level == level), None)
            if candidate is not None:
                chosen = candidate
                break
        if chosen is None:
            unmatched.append(path)
            continue
        matches.append(
            Match(
                path=path,
                pattern=chosen.pattern,
                level=chosen.level,
                sections=chosen.sections,
                reason=chosen.reason,
            )
        )
    return matches, unmatched


def build_report(paths: Sequence[str]) -> Dict[str, object]:
    normalized_paths = sorted(dict.fromkeys(_normalize_path(path) for path in paths if _normalize_path(path)))
    kb_changed = [path for path in normalized_paths if path in KB_FILES]
    matches, unmatched = _classify_paths(normalized_paths)
    required = [item for item in matches if item.level == "required" and item.path not in KB_FILES]
    recommended = [item for item in matches if item.level == "recommended"]
    sections = sorted({section for item in matches for section in item.sections})

    if kb_changed and (required or recommended):
        status = "updated"
    elif required:
        status = "needs_update"
    elif recommended:
        status = "review"
    else:
        status = "clean"

    return {
        "status": status,
        "kb_files": list(KB_FILES),
        "kb_changed": kb_changed,
        "changed_files": normalized_paths,
        "required_matches": [asdict(item) for item in required],
        "recommended_matches": [asdict(item) for item in recommended],
        "unmatched_files": unmatched,
        "suggested_sections": sections,
        "summary": {
            "required_count": len(required),
            "recommended_count": len(recommended),
            "kb_changed": bool(kb_changed),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether the repo knowledge base should be updated before commit.")
    parser.add_argument("--staged", action="store_true", help="Inspect staged git changes.")
    parser.add_argument("--files", nargs="*", default=None, help="Explicit path list to classify instead of git diff.")
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    parser.add_argument("--fail-on-required", action="store_true", help="Exit non-zero when KB update is required but KB files are unchanged.")
    return parser.parse_args()


def _print_text(report: Dict[str, object]) -> None:
    print("KB status:", report["status"])
    summary = report["summary"]
    print(
        "required=%s recommended=%s kb_changed=%s"
        % (summary["required_count"], summary["recommended_count"], summary["kb_changed"])
    )
    sections = report["suggested_sections"]
    if sections:
        print("sections:", ", ".join(str(item) for item in sections))
    if report["required_matches"]:
        print("required matches:")
        for item in report["required_matches"]:
            print("- %s :: %s" % (item["path"], item["reason"]))
    if report["recommended_matches"]:
        print("recommended matches:")
        for item in report["recommended_matches"]:
            print("- %s :: %s" % (item["path"], item["reason"]))
    if report["kb_changed"]:
        print("kb files in diff:", ", ".join(str(item) for item in report["kb_changed"]))


def main() -> None:
    args = parse_args()
    paths = args.files if args.files else _git_diff_names(staged=bool(args.staged))
    report = build_report(paths)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_text(report)
    if args.fail_on_required and report["status"] == "needs_update":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
