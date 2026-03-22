# AGENTS.md

## Start Here
- Read [docs/developer/llm_repo_kb.md](/D:/QLib/docs/developer/llm_repo_kb.md) before changing the repo-local trading workflow.
- Treat `codex_trading/`, `TradingBot/`, `TestCases/`, and `Saved/` as the formal workflow. `examples/codex_daily_autoresearch/` is legacy/reference code, not the live source of truth.
- Keep secrets and runtime state out of git. `TradingBot/config/local/`, `Saved/`, `TradingBot/Saved/`, and generated replay/runtime artifacts are intentionally ignored.

## Commit Rule
- Before commit, run `py -3 scripts/check_repo_kb.py --staged`.
- If the script reports `needs_update`, update [docs/developer/llm_repo_kb.md](/D:/QLib/docs/developer/llm_repo_kb.md) first, then commit.
- If the repo start-up guidance changes, update this file together with the KB.

## Automation Safety
- Manage Codex App automation create/update/pause/delete through the App UI or `::automation-update{...}` directives.
- Do not directly edit `~/.codex/automations/` or the Codex App SQLite state unless the user explicitly approves an emergency recovery.

## Current Live Default
- Current live strategy binding is tracked in [deployment_manifest.json](/D:/QLib/TradingBot/Strategies/googl_momo/deployment_manifest.json).
- The current repo-local knowledge base is expected to stay in sync with any live strategy, release, automation-id, or workflow boundary change.
