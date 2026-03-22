from __future__ import annotations

import json
from pathlib import Path

import yaml

from codex_trading.layout import RepoLayout
from codex_trading.deployment import service as deployment_service


def _make_layout(tmp_path: Path) -> RepoLayout:
    return RepoLayout(
        repo_root=tmp_path,
        saved_root=tmp_path / "Saved",
        autoresearch_root=tmp_path / "Saved" / "AutoResearch",
        autoresearch_runs_root=tmp_path / "Saved" / "AutoResearch" / "Runs",
        autoresearch_cache_root=tmp_path / "Saved" / "AutoResearch" / "Cache",
        testcase_output_root=tmp_path / "Saved" / "TestCases",
        tradingbot_root=tmp_path / "TradingBot",
        tradingbot_saved_root=tmp_path / "TradingBot" / "Saved",
        strategies_root=tmp_path / "TradingBot" / "Strategies",
        automation_templates_root=tmp_path / "TradingBot" / "automations" / "templates",
        automation_generated_root=tmp_path / "TradingBot" / "automations" / "generated",
        tradingbot_config_templates_root=tmp_path / "TradingBot" / "config" / "templates",
        tradingbot_local_config_root=tmp_path / "TradingBot" / "config" / "local",
        testcase_root=tmp_path / "TestCases",
    ).ensure()


def _write_fake_run(run_dir: Path, *, symbol: str, candidate_name: str, sharpe: float) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "winner_strategy.py").write_text("WINNER = 'ok'\n", encoding="utf-8")
    (run_dir / "winner_strategy.md").write_text("# winner\n", encoding="utf-8")
    (run_dir / "run_winner_backtest.py").write_text(
        "from __future__ import annotations\n\nimport json\n\nFROZEN_CONFIG = json.loads('{\"symbol\": \"%s\", \"region\": \"us\", \"provider_uri\": \"~/.qlib/qlib_data/us_data\", \"start_date\": \"2024-01-01\", \"end_date\": \"2024-12-31\", \"benchmark_symbol\": \"QQQ\", \"initial_cash\": 100000.0, \"auto_download_us_data\": false}')\n"
        % symbol,
        encoding="utf-8",
    )
    (run_dir / "run_config.json").write_text(json.dumps({"symbol": symbol}, indent=2), encoding="utf-8")
    (run_dir / "winner_summary.json").write_text(
        json.dumps(
            {
                "candidate_name": candidate_name,
                "research_score": sharpe,
                "sharpe": sharpe,
                "cumulative_return": 0.12,
                "max_drawdown_pct": -5.0,
                "completed_trades": 6,
                "spec_json": json.dumps({"name": candidate_name, "enabled_signals": ["momentum", "ema_trend"]}),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_build_publish_and_rollback_release(tmp_path, monkeypatch):
    layout = _make_layout(tmp_path)
    monkeypatch.setattr(deployment_service, "LAYOUT", layout)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    run_one = layout.autoresearch_runs_root / "20260322_141856_GOOGL"
    run_two = layout.autoresearch_runs_root / "20260323_141856_GOOGL"
    _write_fake_run(run_one, symbol="GOOGL", candidate_name="googl_alpha", sharpe=1.1)
    _write_fake_run(run_two, symbol="GOOGL", candidate_name="googl_beta", sharpe=1.5)

    feishu_config = layout.tradingbot_local_config_root / "feishu_bot.local.yml"
    feishu_config.write_text(
        yaml.safe_dump(
            {
                "feishu": {"app_id": "cli", "app_secret": "secret", "domain": "feishu", "push_chat_id": "oc_x", "allowed_dm_open_ids": ["ou_x"]},
                "tiger": {"config_path": "C:/fake/tiger.properties", "paper_account": None},
                "strategy": {"run_dir": "D:/placeholder"},
                "notifications": {"push_preview": True, "push_submission": True, "push_errors": True},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    package_one = deployment_service.build_strategy_package(run_one, "googl_momo", instrument_universe=["GOOGL"], automation_id="tiger-paper-open")
    release_one = deployment_service.publish_strategy_release("googl_momo", package_one.package_id, notification_profile=str(feishu_config))
    package_two = deployment_service.build_strategy_package(run_two, "googl_momo", instrument_universe=["GOOGL"])
    release_two = deployment_service.publish_strategy_release("googl_momo", package_two.package_id, notification_profile=str(feishu_config))

    deployment_manifest = json.loads((layout.strategies_root / "googl_momo" / "deployment_manifest.json").read_text(encoding="utf-8"))
    generated_automation = (layout.automation_generated_root / "googl_momo" / "automation.toml").read_text(encoding="utf-8")
    external_automation = ((tmp_path / "home") / ".codex" / "automations" / "tiger-paper-open" / "automation.toml").read_text(encoding="utf-8")
    synced_feishu = yaml.safe_load(feishu_config.read_text(encoding="utf-8"))

    assert Path(release_two.live_dir).exists()
    assert deployment_manifest["current_release_id"] == release_two.release_id
    assert package_two.automation_id == "tiger-paper-open"
    assert deployment_manifest["automation_id"] == "tiger-paper-open"
    assert deployment_manifest["releases"][0]["status"] == "history"
    assert deployment_manifest["releases"][-1]["status"] == "live"
    assert "run_tiger_paper_automation.py --strategy-id googl_momo" in generated_automation
    assert generated_automation == external_automation
    assert synced_feishu["strategy"]["run_dir"] == release_two.live_dir

    deployment_manifest["automation_id"] = "googl-paper-automation"
    (layout.strategies_root / "googl_momo" / "deployment_manifest.json").write_text(
        json.dumps(deployment_manifest, indent=2),
        encoding="utf-8",
    )
    rollback = deployment_service.rollback_strategy_release("googl_momo", release_one.release_id)
    deployment_manifest_after = json.loads((layout.strategies_root / "googl_momo" / "deployment_manifest.json").read_text(encoding="utf-8"))
    generated_after_rollback = (layout.automation_generated_root / "googl_momo" / "automation.toml").read_text(encoding="utf-8")
    external_after_rollback = ((tmp_path / "home") / ".codex" / "automations" / "googl-paper-automation" / "automation.toml").read_text(encoding="utf-8")
    synced_feishu_after = yaml.safe_load(feishu_config.read_text(encoding="utf-8"))

    assert rollback.release_id == release_one.release_id
    assert rollback.automation_id == "googl-paper-automation"
    assert deployment_manifest_after["current_release_id"] == release_one.release_id
    assert deployment_manifest_after["automation_id"] == "googl-paper-automation"
    assert 'id = "googl-paper-automation"' in generated_after_rollback
    assert generated_after_rollback == external_after_rollback
    assert synced_feishu_after["strategy"]["run_dir"] == release_one.live_dir
