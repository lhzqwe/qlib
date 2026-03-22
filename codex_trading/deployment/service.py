from __future__ import annotations

import json
import shutil
import tomllib
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import yaml

from ..layout import get_repo_layout


LAYOUT = get_repo_layout()
DEFAULT_AUTOMATION_RRULE = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=21;BYMINUTE=20"
DEFAULT_AUTOMATION_TEMPLATE = LAYOUT.automation_templates_root / "default_strategy_automation.toml"


@dataclass(frozen=True)
class StrategyPackageManifest:
    package_id: str
    strategy_id: str
    strategy_name: str
    package_label: str
    source_run_id: str
    source_run_dir: str
    package_dir: str
    instrument_universe: List[str]
    entry_script: str
    config_fingerprint: str
    backtest_summary: Dict[str, Any]
    artifacts: Dict[str, str]
    created_at: str
    automation_id: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyReleaseManifest:
    release_id: str
    strategy_id: str
    strategy_name: str
    package_id: str
    package_dir: str
    release_dir: str
    live_dir: str
    runtime_dir: str
    source_run_id: str
    instrument_universe: List[str]
    automation_id: str
    automation_name: str
    automation_template: str
    notification_profile: Optional[str]
    published_at: str
    status: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_run_dir(run_ref: Union[str, Path]) -> Path:
    candidate = Path(run_ref).expanduser()
    if candidate.exists():
        return candidate.resolve()
    by_id = LAYOUT.autoresearch_runs_root / str(run_ref)
    if by_id.exists():
        return by_id.resolve()
    raise FileNotFoundError("AutoResearch run was not found: %s" % run_ref)


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _copy_if_exists(source: Path, destination: Path) -> Optional[Path]:
    if not source.exists():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _load_manifest(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"strategy_id": path.parent.name, "current_release_id": None, "releases": []}
    return _load_json(path)


def _strategy_root(strategy_id: str) -> Path:
    root = LAYOUT.strategies_root / strategy_id
    (root / "packages").mkdir(parents=True, exist_ok=True)
    (root / "releases").mkdir(parents=True, exist_ok=True)
    (root / "live").mkdir(parents=True, exist_ok=True)
    return root


def _manifest_automation_id(payload: Dict[str, Any], fallback: Optional[str] = None) -> str:
    value = payload.get("automation_id")
    if value is not None and str(value).strip():
        return str(value).strip()
    if fallback is not None and str(fallback).strip():
        return str(fallback).strip()
    return ""


def _default_automation_id(strategy_id: str) -> str:
    manifest = _load_manifest(_strategy_root(strategy_id) / "deployment_manifest.json")
    value = _manifest_automation_id(manifest, strategy_id)
    return value or str(strategy_id)


def _fingerprint_from_summary(summary_payload: Dict[str, Any]) -> str:
    spec_json = str(summary_payload.get("spec_json") or "")
    if spec_json:
        return str(abs(hash(spec_json)))
    return str(abs(hash(json.dumps(summary_payload, sort_keys=True))))


def _normalize_universe(instrument_universe: Optional[Sequence[str]], source_symbol: Optional[str]) -> List[str]:
    values = [str(item).upper() for item in (instrument_universe or []) if str(item).strip()]
    if values:
        return values
    if source_symbol:
        return [str(source_symbol).upper()]
    return []


def build_strategy_package(
    run_ref: Union[str, Path],
    strategy_id: str,
    package_label: Optional[str] = None,
    instrument_universe: Optional[Sequence[str]] = None,
    *,
    automation_id: Optional[str] = None,
) -> StrategyPackageManifest:
    run_dir = _resolve_run_dir(run_ref)
    summary_path = run_dir / "winner_summary.json"
    runner_path = run_dir / "run_winner_backtest.py"
    strategy_path = run_dir / "winner_strategy.py"
    if not summary_path.exists() or not runner_path.exists() or not strategy_path.exists():
        raise FileNotFoundError("Run %s is missing winner artifacts." % run_dir)

    summary_payload = _load_json(summary_path)
    run_config_path = run_dir / "run_config.json"
    run_config = _load_json(run_config_path) if run_config_path.exists() else {}
    strategy_name = str(summary_payload.get("candidate_name") or strategy_id)
    source_symbol = run_config.get("symbol")
    universe = _normalize_universe(instrument_universe, source_symbol)
    package_id = "%s_%s" % (_now_timestamp(), str(package_label or run_dir.name or strategy_id))
    package_dir = _strategy_root(strategy_id) / "packages" / package_id
    snapshot_dir = package_dir / "run_snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    artifacts = {}
    for file_name in (
        "winner_strategy.py",
        "winner_strategy.md",
        "winner_summary.json",
        "run_winner_backtest.py",
        "run_config.json",
        "research_report.md",
        "market_summary.json",
        "orders.csv",
        "daily_state.csv",
        "completed_trades.csv",
        "report_1day.csv",
        "signal_diagnostics.csv",
    ):
        copied = _copy_if_exists(run_dir / file_name, snapshot_dir / file_name)
        if copied is not None:
            artifacts[file_name] = str(copied)

    manifest = StrategyPackageManifest(
        package_id=package_id,
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        package_label=str(package_label or run_dir.name),
        source_run_id=run_dir.name,
        source_run_dir=str(run_dir),
        package_dir=str(package_dir),
        instrument_universe=universe,
        entry_script=str(snapshot_dir / "winner_strategy.py"),
        config_fingerprint=_fingerprint_from_summary(summary_payload),
        backtest_summary={
            "research_score": summary_payload.get("research_score"),
            "sharpe": summary_payload.get("sharpe"),
            "cumulative_return": summary_payload.get("cumulative_return"),
            "max_drawdown_pct": summary_payload.get("max_drawdown_pct"),
            "completed_trades": summary_payload.get("completed_trades"),
        },
        artifacts=artifacts,
        created_at=_iso_now(),
        automation_id=str(automation_id or _default_automation_id(strategy_id)),
    )
    _write_json(package_dir / "strategy_package.json", manifest.to_dict())
    return manifest


def _render_automation_toml(
    *,
    automation_id: str,
    automation_name: str,
    strategy_id: str,
    repo_root: Path,
    app_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    prompt = (
        "Run `.\\\\.venv\\\\Scripts\\\\python.exe TradingBot\\\\scripts\\\\run_tiger_paper_automation.py "
        "--strategy-id {strategy_id} --runtime-config TradingBot\\\\config\\\\local\\\\tradingbot.runtime.yml --submit`. "
        "Stop without submitting if the selected account type is not PAPER, if Tiger reports the US market is not actionable "
        "for the regular open window, if there is no executable signal, if a real-time Tiger quote is unavailable or stale, or "
        "if the same execution_key was already processed or is already open. Report the strategy id, selected account type, "
        "market status, signal bar date, trade date, trade plan, execution guard, preview artifact path, and the order id plus "
        "final order status if a paper order is submitted."
    ).format(strategy_id=strategy_id)
    lines = [
        "version = 1",
        'id = "%s"' % automation_id,
        'name = "%s"' % automation_name,
        'prompt = "%s"' % prompt.replace("\\", "\\\\").replace('"', '\\"'),
        'status = "ACTIVE"',
        'rrule = "%s"' % DEFAULT_AUTOMATION_RRULE,
        'execution_environment = "worktree"',
        'model = "gpt-5.4"',
        'reasoning_effort = "medium"',
        'cwds = ["%s"]' % str(repo_root).replace("\\", "/"),
    ]
    if isinstance(app_metadata, dict):
        for key in ("created_at", "updated_at"):
            value = app_metadata.get(key)
            try:
                if value is not None:
                    lines.append("%s = %d" % (key, int(value)))
            except (TypeError, ValueError):
                continue
    return "\n".join(lines) + "\n"


def sync_codex_automation(
    strategy_id: str,
    release_manifest: StrategyReleaseManifest,
) -> Path:
    generated_dir = LAYOUT.automation_generated_root / strategy_id
    generated_dir.mkdir(parents=True, exist_ok=True)
    automation_name = "%s (%s)" % (release_manifest.strategy_name, strategy_id)
    payload = _render_automation_toml(
        automation_id=release_manifest.automation_id,
        automation_name=automation_name,
        strategy_id=strategy_id,
        repo_root=LAYOUT.repo_root,
    )
    generated_path = generated_dir / "automation.toml"
    generated_path.write_text(payload, encoding="utf-8")

    external_root = Path.home() / ".codex" / "automations" / release_manifest.automation_id
    external_root.mkdir(parents=True, exist_ok=True)
    external_path = external_root / "automation.toml"
    external_metadata = _load_toml(external_path)
    external_payload = _render_automation_toml(
        automation_id=release_manifest.automation_id,
        automation_name=automation_name,
        strategy_id=strategy_id,
        repo_root=LAYOUT.repo_root,
        app_metadata=external_metadata,
    )
    external_path.write_text(external_payload, encoding="utf-8")
    return generated_path


def _sync_notification_profile(notification_profile: Optional[str], live_dir: Path) -> None:
    if not notification_profile:
        return
    config_path = Path(notification_profile).expanduser().resolve()
    if not config_path.exists():
        return
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return
    strategy_payload = payload.get("strategy") or {}
    if not isinstance(strategy_payload, dict):
        strategy_payload = {}
    strategy_payload["run_dir"] = str(live_dir)
    payload["strategy"] = strategy_payload
    config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def publish_strategy_release(
    strategy_id: str,
    package_id: str,
    automation_template: Optional[str] = None,
    notification_profile: Optional[str] = None,
) -> StrategyReleaseManifest:
    strategy_root = _strategy_root(strategy_id)
    package_dir = strategy_root / "packages" / package_id
    package_manifest_path = package_dir / "strategy_package.json"
    if not package_manifest_path.exists():
        raise FileNotFoundError("Strategy package was not found: %s" % package_manifest_path)
    package_manifest = StrategyPackageManifest(**_load_json(package_manifest_path))

    release_id = _now_timestamp()
    release_dir = strategy_root / "releases" / release_id
    runtime_dir = release_dir / "runtime"
    live_dir = strategy_root / "live"
    shutil.copytree(package_dir / "run_snapshot", runtime_dir, dirs_exist_ok=True)
    if live_dir.exists():
        shutil.rmtree(live_dir)
    shutil.copytree(runtime_dir, live_dir)

    release_manifest = StrategyReleaseManifest(
        release_id=release_id,
        strategy_id=strategy_id,
        strategy_name=package_manifest.strategy_name,
        package_id=package_id,
        package_dir=str(package_dir),
        release_dir=str(release_dir),
        live_dir=str(live_dir),
        runtime_dir=str(runtime_dir),
        source_run_id=package_manifest.source_run_id,
        instrument_universe=list(package_manifest.instrument_universe),
        automation_id=package_manifest.automation_id,
        automation_name="%s (%s)" % (package_manifest.strategy_name, strategy_id),
        automation_template=str(automation_template or DEFAULT_AUTOMATION_TEMPLATE),
        notification_profile=notification_profile,
        published_at=_iso_now(),
        status="live",
    )
    _write_json(release_dir / "release_manifest.json", release_manifest.to_dict())

    deployment_manifest_path = strategy_root / "deployment_manifest.json"
    deployment_manifest = _load_manifest(deployment_manifest_path)
    releases = []
    for item in deployment_manifest.get("releases", []):
        if item.get("release_id") == release_id:
            continue
        item = dict(item)
        item["status"] = "history"
        releases.append(item)
    releases.append(
        {
            "release_id": release_id,
            "package_id": package_id,
            "published_at": release_manifest.published_at,
            "status": "live",
            "runtime_dir": str(runtime_dir),
        }
    )
    deployment_manifest.update(
        {
            "strategy_id": strategy_id,
            "strategy_name": package_manifest.strategy_name,
            "current_release_id": release_id,
            "current_package_id": package_id,
            "live_dir": str(live_dir),
            "instrument_universe": list(package_manifest.instrument_universe),
            "automation_id": package_manifest.automation_id,
            "notification_profile": notification_profile,
            "releases": releases,
        }
    )
    _write_json(deployment_manifest_path, deployment_manifest)
    sync_codex_automation(strategy_id, release_manifest)
    _sync_notification_profile(notification_profile, live_dir)
    return release_manifest


def rollback_strategy_release(strategy_id: str, release_id: str) -> StrategyReleaseManifest:
    strategy_root = _strategy_root(strategy_id)
    release_dir = strategy_root / "releases" / release_id
    manifest_path = release_dir / "release_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError("Release manifest was not found: %s" % manifest_path)
    release_manifest = StrategyReleaseManifest(**_load_json(manifest_path))
    runtime_dir = Path(release_manifest.runtime_dir)
    live_dir = Path(release_manifest.live_dir)
    if live_dir.exists():
        shutil.rmtree(live_dir)
    shutil.copytree(runtime_dir, live_dir)

    deployment_manifest_path = strategy_root / "deployment_manifest.json"
    deployment_manifest = _load_manifest(deployment_manifest_path)
    effective_automation_id = _manifest_automation_id(deployment_manifest, release_manifest.automation_id)
    effective_release_manifest = (
        release_manifest
        if effective_automation_id == release_manifest.automation_id
        else replace(release_manifest, automation_id=effective_automation_id)
    )
    for item in deployment_manifest.get("releases", []):
        item["status"] = "history"
        if item.get("release_id") == release_id:
            item["status"] = "live"
    deployment_manifest["current_release_id"] = release_id
    deployment_manifest["current_package_id"] = release_manifest.package_id
    deployment_manifest["live_dir"] = str(live_dir)
    deployment_manifest["automation_id"] = effective_automation_id
    deployment_manifest["notification_profile"] = release_manifest.notification_profile
    _write_json(deployment_manifest_path, deployment_manifest)
    sync_codex_automation(strategy_id, effective_release_manifest)
    _sync_notification_profile(release_manifest.notification_profile, live_dir)
    return effective_release_manifest
