# GOOGL Winner Strategy

## Summary

- Universe: `GOOGL`
- Benchmark regime reference: `QQQ`
- Frequency: `day` signal, next-day open execution.
- Active signals: `ema_trend, rsi, benchmark_regime`.
- Position model: fixed `95.00%` of account on entry.

## Final Metrics

- Research score: `1.8886`
- Sharpe: `1.8886`
- Cumulative return: `89.91%`
- Max drawdown: `-9.86%`
- Completed trades: `16`

## Frozen DSL

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