# QLib Repo Knowledge Base

Last updated: 2026-03-23

## Purpose

This repository is no longer just upstream `pyqlib`. It now contains a repo-local trading workflow layered on top of Qlib:

- `codex_trading/`: formal Python package for research, deployment, notifications, and trading runtime
- `TradingBot/`: published strategies, live runtime entrypoints, automation templates, local configs
- `TestCases/`: test controller plus unit/integration/e2e/manual validation
- `Saved/`: repo-local research runs, caches, and testcase outputs

Use this file to recover context quickly in a new session or another LLM CLI.

## Repo Layers

### 1. Upstream Qlib

- `qlib/` remains the upstream library layer.
- `README.md` and most of `docs/` still describe the upstream Qlib platform.
- Repo-local trading changes should avoid modifying `qlib/` unless the change really belongs in the core library.

### 2. Repo-local formal workflow

- `codex_trading/layout.py`
  Defines the formal repo layout and the canonical roots for `Saved/`, `TradingBot/`, and `TestCases/`.
- `codex_trading/autoresearch/`
  AutoResearch runner, auth, Codex client integration, prompt generation, candidate evaluation, and frozen winner export.
- `codex_trading/qlib_adapter/`
  Thin boundary layer that currently re-exports Qlib-facing helpers from `codex_trading.autoresearch.service`.
- `codex_trading/deployment/`
  Strategy package build, release publish, rollback, deployment manifest maintenance, and Codex automation sync.
- `codex_trading/tradingbot/`
  Tiger runtime helpers, automation orchestration, entry execution, runtime config loading.
- `codex_trading/notifications/`
  Notification abstractions plus Feishu implementation, bot service, async preview/submission/error delivery.

### 3. Legacy/reference code

- `examples/codex_daily_autoresearch/`
  Historical implementation kept as a reference path.
- Do not treat it as the live deployment source unless a task is explicitly about legacy compatibility.

## Formal Directory Map

### Repo roots

- `Saved/AutoResearch/Runs/`
  AutoResearch run outputs.
- `Saved/AutoResearch/Cache/`
  Cached providers and downloaded market data.
- `Saved/TestCases/`
  Outputs from `TestCases/run_testcases.py` and ad hoc replay validation.
- `TradingBot/Strategies/<strategy_id>/packages/`
  Packaged strategy snapshots created from research runs.
- `TradingBot/Strategies/<strategy_id>/releases/`
  Published immutable releases.
- `TradingBot/Strategies/<strategy_id>/live/`
  Current live runtime snapshot used by the automation runner.
- `TradingBot/Strategies/<strategy_id>/deployment_manifest.json`
  The source of truth for the current live release and automation binding.
- `TradingBot/scripts/`
  Thin CLIs for publish, rollback, live automation, entry runtime, and Feishu bot service.
- `TradingBot/config/templates/`
  Checked-in config templates.
- `TradingBot/config/local/`
  Local gitignored configs, including Tiger runtime and Feishu bot config.
- `TradingBot/automations/templates/`
  Checked-in automation templates.
- `TradingBot/automations/generated/`
  Generated automation TOML under repo control path; gitignored runtime output.

## Stable Entry Points

### AutoResearch

- `python -m codex_trading.autoresearch.run --symbol GOOGL ...`
- Primary implementation: `codex_trading/autoresearch/run.py`

### Publish a strategy

- `python TradingBot/scripts/publish_strategy.py --strategy-id googl_momo --run-id <run_id>`
- Build package from research run and publish a live release.

### Roll back a strategy

- `python TradingBot/scripts/rollback_strategy.py --strategy-id googl_momo --release-id <release_id>`

### Run live paper automation

- `python TradingBot/scripts/run_tiger_paper_automation.py --strategy-id googl_momo --runtime-config TradingBot/config/local/tradingbot.runtime.yml --submit`

### Run Feishu bot service

- `python TradingBot/scripts/run_feishu_bot_service.py --config TradingBot/config/local/feishu_bot.local.yml`

### Test controller

- `python TestCases/run_testcases.py --suite all --module all`
- `python TestCases/run_testcases.py --suite smoke-release --module deployment`

## Workflow Graph

### Research -> Package -> Release -> Live

1. AutoResearch writes a run into `Saved/AutoResearch/Runs/<run_id>/`.
2. `build_strategy_package()` copies winner artifacts into `TradingBot/Strategies/<strategy_id>/packages/<package_id>/run_snapshot/`.
3. `publish_strategy_release()` copies the package snapshot into `releases/<release_id>/runtime/`.
4. `publish_strategy_release()` replaces `TradingBot/Strategies/<strategy_id>/live/` with the new runtime snapshot.
5. `deployment_manifest.json` is updated with:
   - `current_package_id`
   - `current_release_id`
   - `live_dir`
   - `automation_id`
   - `notification_profile`
6. Codex automation TOML is rendered from deployment state and synced to:
   - `TradingBot/automations/generated/<strategy_id>/automation.toml`
   - `~/.codex/automations/<automation_id>/automation.toml`

### Live automation execution

1. Codex App automation invokes `TradingBot/scripts/run_tiger_paper_automation.py`.
2. The script loads `deployment_manifest.json`, resolves `live_dir`, and loads `TradingBot/config/local/tradingbot.runtime.yml`.
3. `codex_trading.tradingbot.automation_runtime.execute_automation_session()`:
   - loads the frozen winner context from `live_dir`
   - reads Tiger market status and quote state
   - builds preview payload
   - writes `tiger_paper_auto_preview.json`
   - optionally submits a Tiger paper order
   - writes `tiger_paper_auto_submission.json`
   - writes `tiger_paper_auto_summary.json`
   - updates `execution_ledger.json`
4. Feishu preview/submission notifications are enqueued asynchronously in production.

### Feishu bot query path

- DM command handling is in `codex_trading/notifications/feishu.py`.
- Supported DM commands:
  - `帮助 / help`
  - `持仓 / positions`
  - `状态 / strategy`
  - `最近执行 / last`
- Sensitive account queries are gated by `allowed_dm_open_ids`.
- Group push uses the configured `push_chat_id`.

## Current Live State

As of 2026-03-22:

- `strategy_id`: `googl_momo`
- `strategy_name`: `trend_rsi_market_timebox_strict`
- `current_release_id`: `20260322_114729_122011`
- `automation_id`: `googl-paper-automation`
- `live manifest`: `TradingBot/Strategies/googl_momo/deployment_manifest.json`
- `live runtime snapshot`: `TradingBot/Strategies/googl_momo/live/`
- `runtime config`: `TradingBot/config/local/tradingbot.runtime.yml`
- `notification profile`: `TradingBot/config/local/feishu_bot.local.yml`

If any of those change, this KB should usually be updated.

## Current Strategy Characteristics

The current live winner is a daily strategy with these important constraints:

- Frequency is `day` only.
- Execution model is `next-day open execution`.
- It is not a true `15min` or intraday live strategy.
- Position sizing is fixed at `95%` of account on entry.
- Key enabled signals:
  - `ema_trend`
  - `rsi`
  - `benchmark_regime`

This matters because:

- backtest entry/exit prices are based on next-session `open_adj`
- same-day close movement after an exit signal is not captured if the strategy exits at the next open

## Testing Model

### Controller

- `TestCases/run_testcases.py` is the primary test entrypoint.
- Module filters:
  - `notifications`
  - `autoresearch`
  - `qlib_adapter`
  - `tradingbot`
  - `deployment`

### Coverage shape

- `TestCases/unit/`
  Smoke-level checks for each repo-local module area.
- `TestCases/integration/`
  Publish/release flow integration.
- `TestCases/e2e/`
  Full automation replay, notification formatting, idempotency, and release path validation.
- `TestCases/e2e/generated/`
  Generated replay tests for a specific research run or live strategy; gitignored.
- `TestCases/manual/`
  Human validation playbooks.

### Important replay fixtures

- Compact historical replay fixture:
  - `2024-10-23` buy
  - `2024-10-24` sell
  - used in `TestCases/e2e/test_codex_trading_e2e.py`
- Current live-strategy generated replay:
  - `2025-09-08` buy
  - `2025-09-09` sell
  - used in `TestCases/e2e/generated/test_googl_momo_20260322_190311_googl_replay.py`

These tests validate:

- preview generation
- submission generation
- buy/sell Feishu title distinction
- idempotent rerun behavior
- replay-compatible end-to-end automation flow

## Notification and Automation Caveats

### Feishu behavior

- Production notification sending is asynchronous via `ThreadPoolExecutor`.
- Group pushes are currently:
  - `盘前计划`
  - `盘中成交`
  - `异常`
- Summary artifacts are still written locally, but group summary push is usually disabled by config.
- The runtime `.venv` must include `lark-oapi` for real Feishu delivery. Missing the package does not prevent preview/submission artifact generation, but notification send calls will fail at runtime.

### Automation state

- Repo code renders automation TOML to the external Codex automation directory.
- Codex App has its own UI/state behavior on top of the TOML file.
- Normal lifecycle changes must go through the App UI or automation directives.
- Direct file or SQLite edits should only be used for explicit emergency recovery approved by the user.
- Do not assume Codex App `Runs in = 工作树` is safe on Windows. The App worktree clone can lag behind the live repo and may not contain the current `TradingBot/`, `codex_trading/`, `TestCases/`, or `.venv`.
- Deployment sync must not force `execution_environment = "worktree"`. Preserve the App-selected execution environment when one already exists.
- Automation prompts should use absolute repo paths such as `D:/QLib/.venv/Scripts/python.exe` and `D:/QLib/TradingBot/scripts/run_tiger_paper_automation.py` so worktree drift does not break script resolution.
- On this Windows setup, Codex automation startup recovered only after `%USERPROFILE%/.codex/config.toml` was switched to `[windows] sandbox = "unelevated"`. If local tools fail before process startup with `CreateProcessWithLogonW failed: 1385`, check the Codex App sandbox mode before blaming the trading script.

### Deployment/automation coupling

- `deployment_manifest.json` is the authoritative automation binding for a strategy.
- Publish and rollback should always keep `deployment_manifest.automation_id` aligned with the actual live App automation.

### Tiger PAPER order lookup

- Do not assume Tiger `place_order()` returns an id that can always be queried back through `get_order(order_id=...)`.
- In the current PAPER environment, `get_order()` may raise `1010 biz param error` even when the order was accepted and filled.
- Order terminal-state polling should therefore fall back to `get_open_orders()` / `get_orders()` and match by normalized `order_id` or `execution_key` / `user_mark`.

## Known Architectural Notes

- `codex_trading/qlib_adapter` is still a thin re-export layer, not a fully independent implementation boundary.
- `TradingBot/config/local/` and all runtime artifacts are intentionally gitignored.
- `Saved/` is the repo-local artifact root and is also gitignored.
- `TradingBot/Strategies/` is the deployment source of truth for live strategy state.
- `examples/codex_daily_autoresearch/` still exists, but it should not be used as the default answer for “how this repo works now”.

## Fast Recovery Checklist For A New Session

Open these files in order:

1. `AGENTS.md`
2. `docs/developer/llm_repo_kb.md`
3. `TradingBot/Strategies/googl_momo/deployment_manifest.json`
4. `TradingBot/README.md`
5. `TestCases/README.md`

Then decide whether the task belongs to:

- upstream `qlib/`
- repo-local `codex_trading/`
- published strategy state under `TradingBot/Strategies/`
- runtime/notification behavior
- test/controller behavior

## Commit-Time Knowledge Base Policy

Run:

```powershell
py -3 scripts/check_repo_kb.py --staged
```

Interpretation:

- `clean`
  No KB update is normally needed.
- `review`
  Review the KB and update it if the staged changes alter workflow assumptions or repo boundaries.
- `needs_update`
  KB update is expected before commit.
- `updated`
  The staged diff already includes KB changes.

### Changes that usually require a KB update

- repo layout or root path semantics
- `codex_trading` public module boundaries
- `TradingBot/scripts` entrypoints
- deployment/publish/rollback behavior
- notification semantics
- test controller behavior
- live strategy binding or current deployment manifest
- config template changes that affect runtime or operator workflow

### Changes that usually do not require a KB update

- gitignored runtime artifacts
- local secrets/config values
- ad hoc replay outputs
- temporary Feishu probe files

## Suggested Validation Before Commit

For repo-local trading workflow changes:

```powershell
py -3 scripts/check_repo_kb.py --staged
py -3 TestCases/run_testcases.py --suite all --module all
```

For deployment-only changes:

```powershell
py -3 scripts/check_repo_kb.py --staged
py -3 TestCases/run_testcases.py --suite smoke-release --module deployment
```
