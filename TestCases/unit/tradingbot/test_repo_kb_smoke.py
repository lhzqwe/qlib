from __future__ import annotations

from scripts.check_repo_kb import build_report


def test_repo_kb_requires_update_for_live_runtime_changes():
    report = build_report(["codex_trading/tradingbot/automation_runtime.py"])
    assert report["status"] == "needs_update"
    assert "Workflow Graph" in report["suggested_sections"]


def test_repo_kb_ignores_runtime_artifacts():
    report = build_report(
        [
            "Saved/TestCases/20260322_201046_googl_live_feishu_replay/run/tiger_paper_auto/tiger_paper_auto_submission.json",
            "TradingBot/config/local/feishu_bot.local.yml",
        ]
    )
    assert report["status"] == "clean"
    assert report["suggested_sections"] == []


def test_repo_kb_marks_diff_as_updated_when_kb_is_touched():
    report = build_report(
        [
            "codex_trading/deployment/service.py",
            "docs/developer/llm_repo_kb.md",
        ]
    )
    assert report["status"] == "updated"
    assert report["kb_changed"] == ["docs/developer/llm_repo_kb.md"]
