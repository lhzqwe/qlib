# TradingBot

## Layout

- `scripts/`: thin CLI entrypoints for publish / rollback / live trading / bot service
- `Strategies/<strategy_id>/packages/`: built strategy packages from AutoResearch runs
- `Strategies/<strategy_id>/releases/`: published versioned releases
- `Strategies/<strategy_id>/live/`: current live runtime snapshot
- `automations/templates/`: Codex automation templates
- `automations/generated/`: generated automation TOML files
- `config/templates/`: checked-in config templates
- `config/local/`: local runtime and Feishu config files, gitignored
- `Saved/<strategy_id>/...`: TradingBot runtime artifacts

## Primary Commands

```bash
python -m codex_trading.autoresearch.run --symbol GOOGL --start-date 2024-01-01
python TradingBot/scripts/publish_strategy.py --strategy-id googl_momo --run-id <run_id>
python TradingBot/scripts/run_tiger_paper_automation.py --strategy-id googl_momo --runtime-config TradingBot/config/local/tradingbot.runtime.yml --submit
python TradingBot/scripts/rollback_strategy.py --strategy-id googl_momo --release-id <release_id>
python TradingBot/scripts/run_feishu_bot_service.py --config TradingBot/config/local/feishu_bot.local.yml
```
