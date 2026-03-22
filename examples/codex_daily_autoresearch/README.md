# Codex Daily Autoresearch

This example adds a daily single-stock autoresearch loop on top of Qlib without changing `qlib/` core.

It keeps the external repository's core ideas:

- fixed evaluation harness
- single mutable strategy surface
- leaderboarded experiment loop
- keep-only-if-improves retention
- frozen winner export
- explicit ablation pressure

The mutable surface is a typed JSON/DSL, not arbitrary Python. Codex proposes DSL candidates, the local harness validates them, generates explicit orders, and Qlib remains the authority for scoring and final replay.

## Usage

```bash
python examples/codex_daily_autoresearch/run_autoresearch.py ^
  --symbol NVDA ^
  --region us ^
  --provider-uri ~/.qlib/qlib_data/us_data ^
  --start-date 2018-01-01 ^
  --end-date 2024-12-31 ^
  --llm-model gpt-5.4 ^
  --max-rounds 4 ^
  --candidates-per-round 6
```

## Output

Each run creates a timestamped directory under `examples/codex_daily_autoresearch/output/` with:

- `leaderboard.csv`
- `experiment_log.jsonl`
- `research_report.md`
- `codex_trace/`
- `winner_strategy.py`
- `run_winner_backtest.py`
- `orders.csv`
- `report_1day.csv`
- `winner_strategy.md`

`run_winner_backtest.py` replays the exported winner through `FileOrderStrategy + SimulatorExecutor + qlib.backtest.backtest(...)`.

## Tiger Paper Entry

`run_tiger_paper_entry.py` is the Tiger-only single-shot execution entry for a frozen winner.

- it resolves the latest exported run by default
- it rebuilds the winner signal from Tiger daily bars only
- it uses the last completed daily bar to generate the next-session action
- it auto-discovers a `PAPER` account from Tiger managed accounts
- it sizes orders from Tiger quote data, not Qlib or Yahoo
- it writes a local execution ledger and uses a stable execution key to avoid duplicate submissions
- it refuses to submit if the selected account is not a paper account
- it refuses to submit if a real-time Tiger quote is unavailable or stale
- it defaults to preview mode and only places an order when `--submit` is passed

Install the optional Tiger SDK first:

```bash
pip install tigeropen
```

Example:

```bash
python examples/codex_daily_autoresearch/run_tiger_paper_entry.py ^
  --strategy-run-dir examples/codex_daily_autoresearch/output/20260322_105032 ^
  --tiger-config C:/Users/hezhengli/Downloads/tiger_openapi_config.properties ^
  --signal-end-date 2026-03-23 ^
  --order-type market
```

The script writes:

- `tiger_paper_preview.json`
- `execution_ledger.json`
- `tiger_paper_submission.json` after a successful `--submit`

## Tiger Paper Automation

`run_tiger_paper_automation.py` is the scheduled open-window execution wrapper.

- it checks Tiger US market status before submitting
- it can wait from pre-open until the regular open
- it does not build signal, account, or order state before sleeping; everything is refreshed after the open
- it only submits inside the configurable post-open window
- it polls the Tiger order to a terminal state inside that window
- it cancels the remainder if a partially filled order is still open at the deadline
- it uses `execution_key = {paper_account}:{symbol}:{strategy_name}:{trade_date}:{signal_bar_date}:{action}`
- it checks both Tiger orders and the local execution ledger before placing any new order
- it still refuses to submit unless the selected account is `PAPER`
- it treats Tiger as the only live data source; there is no Qlib/Yahoo fallback on the execution path

Example:

```bash
python examples/codex_daily_autoresearch/run_tiger_paper_automation.py ^
  --strategy-run-dir examples/codex_daily_autoresearch/output/20260322_105032 ^
  --tiger-config C:/Users/hezhengli/Downloads/tiger_openapi_config.properties ^
  --execution-window-minutes 15 ^
  --order-poll-sec 3 ^
  --max-quote-staleness-sec 15 ^
  --submit
```

Artifacts from the automation path include:

- `tiger_paper_auto_preview.json`
- `execution_ledger.json`
- `tiger_paper_auto_submission.json`

The detailed scheduled flow is documented in [tiger_paper_automation_sequence.md](D:/QLib/examples/codex_daily_autoresearch/tiger_paper_automation_sequence.md).
