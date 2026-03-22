from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RepoLayout:
    repo_root: Path = REPO_ROOT
    saved_root: Path = REPO_ROOT / "Saved"
    autoresearch_root: Path = REPO_ROOT / "Saved" / "AutoResearch"
    autoresearch_runs_root: Path = REPO_ROOT / "Saved" / "AutoResearch" / "Runs"
    autoresearch_cache_root: Path = REPO_ROOT / "Saved" / "AutoResearch" / "Cache"
    testcase_output_root: Path = REPO_ROOT / "Saved" / "TestCases"
    tradingbot_root: Path = REPO_ROOT / "TradingBot"
    tradingbot_saved_root: Path = REPO_ROOT / "TradingBot" / "Saved"
    strategies_root: Path = REPO_ROOT / "TradingBot" / "Strategies"
    automation_templates_root: Path = REPO_ROOT / "TradingBot" / "automations" / "templates"
    automation_generated_root: Path = REPO_ROOT / "TradingBot" / "automations" / "generated"
    tradingbot_config_templates_root: Path = REPO_ROOT / "TradingBot" / "config" / "templates"
    tradingbot_local_config_root: Path = REPO_ROOT / "TradingBot" / "config" / "local"
    testcase_root: Path = REPO_ROOT / "TestCases"

    def ensure(self) -> "RepoLayout":
        for path in (
            self.saved_root,
            self.autoresearch_root,
            self.autoresearch_runs_root,
            self.autoresearch_cache_root,
            self.testcase_output_root,
            self.tradingbot_root,
            self.tradingbot_saved_root,
            self.strategies_root,
            self.automation_templates_root,
            self.automation_generated_root,
            self.tradingbot_config_templates_root,
            self.tradingbot_local_config_root,
            self.testcase_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self


def get_repo_layout() -> RepoLayout:
    return RepoLayout().ensure()

