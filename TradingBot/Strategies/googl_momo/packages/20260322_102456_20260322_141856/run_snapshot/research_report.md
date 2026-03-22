# GOOGL Daily Codex Autoresearch Report

## Summary

- Region: `us`
- Research window: `2024-03-22` to `2026-03-20`.
- Provider mode: `local_yahoo_dump`.
- Final winner: `trend_short_momo_market_holdcap_28`.
- Final research score: `1.9959`.

## Market Summary

```json
{
  "benchmark_return_pct": 31.661665439605713,
  "benchmark_source": "yahoo",
  "benchmark_symbol": "QQQ",
  "end_date": "2026-03-20",
  "num_bars": 500,
  "provider_mode": "local_yahoo_dump",
  "provider_path": "D:\\QLib\\examples\\codex_daily_autoresearch\\cache\\yahoo_provider_20240322_20260322\\qlib_day_data",
  "start_date": "2024-03-22",
  "symbol": "GOOGL",
  "symbol_realized_vol_pct": 29.615470833822403,
  "symbol_return_pct": 101.2918472290039,
  "symbol_source": "yahoo"
}
```

## Rounds

### round_00_baseline

- Objective: Seed the autoresearch run with fixed baseline strategies.
- Candidate count: `7`
- Winner: `ensemble_plus_volume`
- Improved best: `yes`
- Leaderboard: `D:\QLib\examples\codex_daily_autoresearch\output\20260322_141856\round_00_baseline\round_leaderboard.csv`

### round_01_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `core_triple_confirm`
- Improved best: `no`
- Leaderboard: `D:\QLib\examples\codex_daily_autoresearch\output\20260322_141856\round_01_codex\round_leaderboard.csv`

### round_02_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `4`
- Winner: `market_aware_triple_confirm`
- Improved best: `yes`
- Leaderboard: `D:\QLib\examples\codex_daily_autoresearch\output\20260322_141856\round_02_codex\round_leaderboard.csv`

### round_03_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `6`
- Winner: `trend_rsi_market_filter`
- Improved best: `no`
- Leaderboard: `D:\QLib\examples\codex_daily_autoresearch\output\20260322_141856\round_03_codex\round_leaderboard.csv`

### round_04_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `5`
- Winner: `trend_volume_market_strict`
- Improved best: `yes`
- Leaderboard: `D:\QLib\examples\codex_daily_autoresearch\output\20260322_141856\round_04_codex\round_leaderboard.csv`

### round_05_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `6`
- Winner: `trend_short_momo_market`
- Improved best: `yes`
- Leaderboard: `D:\QLib\examples\codex_daily_autoresearch\output\20260322_141856\round_05_codex\round_leaderboard.csv`

### round_06_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `4`
- Winner: `ablation_trend_short_momo_no_market`
- Improved best: `no`
- Leaderboard: `D:\QLib\examples\codex_daily_autoresearch\output\20260322_141856\round_06_codex\round_leaderboard.csv`

### round_07_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `4`
- Winner: `ema_macd_market_consensus`
- Improved best: `no`
- Leaderboard: `D:\QLib\examples\codex_daily_autoresearch\output\20260322_141856\round_07_codex\round_leaderboard.csv`

### round_08_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `ablation_trend_short_momo_market_holdcap`
- Improved best: `yes`
- Leaderboard: `D:\QLib\examples\codex_daily_autoresearch\output\20260322_141856\round_08_codex\round_leaderboard.csv`

### round_09_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `7`
- Winner: `trend_short_momo_market_cooldown`
- Improved best: `yes`
- Leaderboard: `D:\QLib\examples\codex_daily_autoresearch\output\20260322_141856\round_09_codex\round_leaderboard.csv`

### round_10_codex

- Objective: Codex proposes new typed DSL candidates plus one ablation.
- Candidate count: `6`
- Winner: `trend_short_momo_market_holdcap_28`
- Improved best: `yes`
- Leaderboard: `D:\QLib\examples\codex_daily_autoresearch\output\20260322_141856\round_10_codex\round_leaderboard.csv`

## Global Leaderboard

| candidate_name | research_score | sharpe | max_drawdown_pct | completed_trades | cumulative_return | accepted |
| --- | --- | --- | --- | --- | --- | --- |
| trend_short_momo_market_holdcap_28 | 1.9959469462444672 | 1.9959469462444672 | -9.239786124830896 | 15 | 1.0893521585275274 | True |
| trend_short_momo_market_cooldown | 1.7927676945949347 | 1.7927676945949347 | -10.987707274157044 | 12 | 0.9229291456105042 | True |
| ablation_trend_short_momo_market_holdcap | 1.746211497120205 | 1.7727840452013026 | -12.332156851013721 | 16 | 0.9330668289659121 | True |
| trend_short_momo_rsi_no_market | 1.7402760704666773 | 1.915242340413187 | -11.70564207820226 | 32 | 1.0723579997024535 | True |
| trend_short_momo_market_volume | 1.683477886236225 | 1.683477886236225 | -10.847702076220767 | 16 | 0.882824007292786 | True |
| trend_short_momo_market | 1.6780977400715713 | 1.704670288152669 | -12.332156851013721 | 12 | 0.9372513036045071 | True |
| ablation_trend_volume_market_minimal | 1.6349671034680364 | 1.661539651549134 | -12.332156851013721 | 16 | 0.912399930827255 | True |
| ablation_trend_short_momo_no_market | 1.6312213413429617 | 1.6578605926176946 | -12.33299064093416 | 12 | 0.9056781275672146 | True |
| trend_short_momo_market_holdcap | 1.5646708768158297 | 1.5912434248969274 | -12.332156851013721 | 14 | 0.8338491782823945 | True |
| trend_volume_short_momo_no_market | 1.561898194161058 | 1.561898194161058 | -11.203451092668127 | 15 | 0.7870308855360417 | True |

## Final Winner

```json
{
  "atr_stop_mult": 0.0,
  "atr_window": 14,
  "bb_percentile": 0.35,
  "bb_window": 20,
  "benchmark_symbol": "QQQ",
  "cooldown_days": 2,
  "ema_fast": 12,
  "ema_slow": 40,
  "enabled_signals": [
    "ema_trend",
    "short_momentum",
    "benchmark_regime"
  ],
  "macd_fast": 12,
  "macd_signal": 9,
  "macd_slow": 26,
  "market_ma_window": 50,
  "max_hold_days": 28,
  "momentum_window": 63,
  "name": "trend_short_momo_market_holdcap_28",
  "position_size_pct": 0.95,
  "rsi_entry_midline": 55.0,
  "rsi_period": 14,
  "rsi_take_profit": 101.0,
  "short_momentum_window": 21,
  "volume_ratio_threshold": 1.05,
  "volume_window": 20,
  "vote_threshold": 0.67
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