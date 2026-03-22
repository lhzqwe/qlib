from __future__ import annotations

from pathlib import Path

from codex_trading.deployment import build_strategy_package, publish_strategy_release
from TestCases.unit.deployment.test_release_flow import _make_layout, _write_fake_run
from codex_trading.deployment import service as deployment_service


def test_publish_creates_live_runtime_without_source_run_dependency(tmp_path, monkeypatch):
    layout = _make_layout(tmp_path)
    monkeypatch.setattr(deployment_service, "LAYOUT", layout)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    run_dir = layout.autoresearch_runs_root / "20260322_141856_GOOGL"
    _write_fake_run(run_dir, symbol="GOOGL", candidate_name="googl_alpha", sharpe=1.2)

    package_manifest = build_strategy_package(run_dir, "googl_momo", instrument_universe=["GOOGL"])
    release_manifest = publish_strategy_release("googl_momo", package_manifest.package_id)

    live_dir = Path(release_manifest.live_dir)
    assert (live_dir / "winner_strategy.py").exists()
    assert str(live_dir).startswith(str(layout.strategies_root))
    assert str(run_dir) != str(live_dir)
