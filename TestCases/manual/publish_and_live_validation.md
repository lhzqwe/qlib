# TradingBot Manual Validation

1. Run `python -m codex_trading.autoresearch.run --symbol GOOGL ...` and confirm a new run appears under `Saved/AutoResearch/Runs/`.
2. Run `python TradingBot/scripts/publish_strategy.py --strategy-id googl_momo --run-id <run_id>`.
3. Confirm `TradingBot/Strategies/googl_momo/releases/<release_id>/` and `TradingBot/Strategies/googl_momo/live/` exist.
4. Confirm `~/.codex/automations/<automation_id>/automation.toml` was updated.
5. Start `python TradingBot/scripts/run_feishu_bot_service.py --config TradingBot/config/local/feishu_bot.local.yml`.
6. Run `python TradingBot/scripts/run_tiger_paper_automation.py --strategy-id googl_momo --runtime-config TradingBot/config/local/tradingbot.runtime.yml --submit`.
7. Verify Feishu DM `帮助` / `状态` / `持仓` and group push `盘前计划` / `盘中成交`.
