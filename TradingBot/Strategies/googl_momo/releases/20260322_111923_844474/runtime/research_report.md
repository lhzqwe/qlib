# GOOGL Daily Codex Autoresearch Report

## Summary

- Region: `us`
- Research window: `2024-03-22` to `2026-03-20`.
- Provider mode: `local_yahoo_dump`.
- Final winner: `trend_rsi_market_timebox_strict`.
- Final research score: `1.8886`.

## Market Summary

```json
{
  "benchmark_return_pct": 31.661677360534668,
  "benchmark_source": "yahoo",
  "benchmark_symbol": "QQQ",
  "end_date": "2026-03-20",
  "num_bars": 500,
  "provider_mode": "local_yahoo_dump",
  "provider_path": "D:\\QLib\\Saved\\AutoResearch\\Cache\\yahoo_provider_20240322_20260322\\qlib_day_data",
  "start_date": "2024-03-22",
  "symbol": "GOOGL",
  "symbol_realized_vol_pct": 29.615470833822403,
  "symbol_return_pct": 101.29187107086182,
  "symbol_source": "yahoo"
}
```

## Rounds

### round_00_baseline

- Objective: Seed the autoresearch run with fixed baseline strategies.
- Candidate count: `7`
- Winner: `ensemble_plus_volume`
- Improved best: `yes`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_00_baseline\round_leaderboard.csv`

### round_01_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `trend_short_momo_confirm`
- Improved best: `yes`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_01_codex\round_leaderboard.csv`

### round_02_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `6`
- Winner: `trend_short_momo_market_gate`
- Improved best: `yes`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_02_codex\round_leaderboard.csv`

### round_03_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `6`
- Winner: `ablation_trend_market_only`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_03_codex\round_leaderboard.csv`

### round_04_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `5`
- Winner: `compression_trend_market_simple`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_04_codex\round_leaderboard.csv`

### round_05_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `5`
- Winner: `trend_short_momo_rsi_market`
- Improved best: `yes`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_05_codex\round_leaderboard.csv`

### round_06_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `trend_rsi_timebox_market`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_06_codex\round_leaderboard.csv`

### round_07_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `6`
- Winner: `trend_rsi_market_cooldown`
- Improved best: `yes`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_07_codex\round_leaderboard.csv`

### round_08_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `ablation_trend_rsi_only`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_08_codex\round_leaderboard.csv`

### round_09_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `6`
- Winner: `trend_rsi_market_timebox_light`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_09_codex\round_leaderboard.csv`

### round_10_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `6`
- Winner: `trend_rsi_market_tighter_rsi`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_10_codex\round_leaderboard.csv`

### round_11_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `trend_rsi_market_timebox_strict`
- Improved best: `yes`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_11_codex\round_leaderboard.csv`

### round_12_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `trend_rsi_market_tighter_filter`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_12_codex\round_leaderboard.csv`

### round_13_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `trend_short_momo_market_compact`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_13_codex\round_leaderboard.csv`

### round_14_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `trend_rsi_market_relaxed_hold`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_14_codex\round_leaderboard.csv`

### round_15_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `5`
- Winner: `trend_rsi_market_atr_guard`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_15_codex\round_leaderboard.csv`

### round_16_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `trend_rsi_volume_market_core`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_16_codex\round_leaderboard.csv`

### round_17_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `trend_rsi_market_atr_guard`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_17_codex\round_leaderboard.csv`

### round_18_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `6`
- Winner: `trend_rsi_volume_market_balanced`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_18_codex\round_leaderboard.csv`

### round_19_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `trend_rsi_market_looser_regime`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_19_codex\round_leaderboard.csv`

### round_20_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `5`
- Winner: `trend_rsi_market_timebox_relaxed`
- Improved best: `no`
- Leaderboard: `D:\QLib\Saved\AutoResearch\Runs\20260322_190311_GOOGL\round_20_codex\round_leaderboard.csv`

## Global Leaderboard

| candidate_name | research_score | sharpe | max_drawdown_pct | completed_trades | cumulative_return | accepted |
| --- | --- | --- | --- | --- | --- | --- |
| trend_rsi_market_timebox_strict | 1.8886220513404723 | 1.8886220513404723 | -9.863010141625528 | 16 | 0.8990667407891091 | True |
| trend_rsi_volume_market_core | 1.8749977607441721 | 1.8749977607441721 | -9.90799720160862 | 12 | 0.8502582338134004 | True |
| trend_rsi_market_cooldown | 1.8561257323481342 | 1.856888170100989 | -9.262747152460339 | 20 | 0.8281902895961759 | True |
| trend_rsi_market_timebox_light | 1.8561257323481342 | 1.856888170100989 | -9.262747152460339 | 20 | 0.8281902895961759 | True |
| trend_short_momo_market_compact | 1.8026984315722836 | 1.8026984315722836 | -9.834440868016515 | 18 | 0.9469188356576539 | True |
| ablation_trend_rsi_only | 1.7980116750579118 | 1.7980116750579118 | -9.26576334211342 | 20 | 0.8002237029853825 | True |
| trend_rsi_volume_market_balanced | 1.791553534316022 | 1.791553534316022 | -9.911971525832897 | 13 | 0.8000683522057344 | True |
| trend_rsi_market_tighter_filter | 1.7899458926478722 | 1.7899458926478722 | -9.898858531363574 | 14 | 0.8200222368933872 | True |
| ablation_trend_rsi_market_only | 1.7829456367018315 | 1.9646770872772532 | -11.71063589241016 | 32 | 1.1056208728849786 | True |
| trend_short_momo_rsi_market | 1.7829456367018315 | 1.9646770872772532 | -11.71063589241016 | 32 | 1.1056208728849786 | True |

## Final Winner

```json
{
  "atr_stop_mult": 0.0,
  "atr_window": 14,
  "bb_percentile": 0.35,
  "bb_window": 20,
  "benchmark_symbol": "QQQ",
  "cooldown_days": 3,
  "ema_fast": 12,
  "ema_slow": 40,
  "enabled_signals": [
    "ema_trend",
    "rsi",
    "benchmark_regime"
  ],
  "macd_fast": 12,
  "macd_signal": 9,
  "macd_slow": 26,
  "market_ma_window": 50,
  "max_hold_days": 30,
  "momentum_window": 63,
  "name": "trend_rsi_market_timebox_strict",
  "position_size_pct": 0.95,
  "rsi_entry_midline": 55.0,
  "rsi_period": 14,
  "rsi_take_profit": 74.0,
  "short_momentum_window": 21,
  "volume_ratio_threshold": 1.05,
  "volume_window": 20,
  "vote_threshold": 1.0
}
```

## Output Files

- `leaderboard.csv`
- `experiment_log.jsonl`
- `research_report.md`
- `codex_trace/`
- `winner_strategy.py`
- `run_winner_backtest.py`
- `orders.csv`
- `report_1day.csv`
- `winner_strategy.md`
- `winner_summary.json`