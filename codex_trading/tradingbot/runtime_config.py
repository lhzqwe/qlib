from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


@dataclass(frozen=True)
class TradingBotRuntimeConfig:
    tiger_config: str
    paper_account: Optional[str] = None
    feishu_config: Optional[str] = None
    order_type: str = "market"
    limit_price: Optional[float] = None
    cash_buffer_pct: float = 0.98
    execution_window_minutes: int = 15
    max_preopen_wait_minutes: int = 180
    order_poll_sec: int = 3
    max_quote_staleness_sec: int = 15
    bars_lookback_buffer: int = 30

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tiger_config": self.tiger_config,
            "paper_account": self.paper_account,
            "feishu_config": self.feishu_config,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "cash_buffer_pct": self.cash_buffer_pct,
            "execution_window_minutes": self.execution_window_minutes,
            "max_preopen_wait_minutes": self.max_preopen_wait_minutes,
            "order_poll_sec": self.order_poll_sec,
            "max_quote_staleness_sec": self.max_quote_staleness_sec,
            "bars_lookback_buffer": self.bars_lookback_buffer,
        }


def load_tradingbot_runtime_config(config_path: str) -> TradingBotRuntimeConfig:
    path = Path(config_path).expanduser().resolve()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("TradingBot runtime config must be a mapping: %s" % path)
    tiger_config = str(payload.get("tiger_config") or "").strip()
    if not tiger_config:
        raise ValueError("TradingBot runtime config requires tiger_config.")
    limit_price = payload.get("limit_price")
    return TradingBotRuntimeConfig(
        tiger_config=tiger_config,
        paper_account=str(payload.get("paper_account")).strip() if payload.get("paper_account") else None,
        feishu_config=str(payload.get("feishu_config")).strip() if payload.get("feishu_config") else None,
        order_type=str(payload.get("order_type") or "market"),
        limit_price=float(limit_price) if limit_price not in (None, "") else None,
        cash_buffer_pct=float(payload.get("cash_buffer_pct") or 0.98),
        execution_window_minutes=int(payload.get("execution_window_minutes") or 15),
        max_preopen_wait_minutes=int(payload.get("max_preopen_wait_minutes") or 180),
        order_poll_sec=int(payload.get("order_poll_sec") or 3),
        max_quote_staleness_sec=int(payload.get("max_quote_staleness_sec") or 15),
        bars_lookback_buffer=int(payload.get("bars_lookback_buffer") or 30),
    )

