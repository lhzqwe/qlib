from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from pandas.tseries.offsets import BDay

from ..autoresearch.service import CandidateStrategySpec, ResearchConfig
from ..qlib_adapter import build_feature_frame


SCRIPT_DIR = Path(__file__).resolve().parent
US_MARKET_TZ = ZoneInfo("America/New_York")
OPEN_ORDER_STATUSES = {"PendingNew", "Initial", "Submitted", "PartiallyFilled", "PendingCancel"}
TERMINAL_ORDER_STATUSES = {"Filled", "Cancelled", "Invalid", "Inactive"}
COMPLETED_EXECUTION_STATUSES = TERMINAL_ORDER_STATUSES | {"PartialFillCancelled"}


@dataclass(frozen=True)
class FrozenStrategyContext:
    run_dir: Path
    research_config: ResearchConfig
    winner_spec: CandidateStrategySpec
    symbol: str
    region: str
    benchmark_symbol: Optional[str]
    strategy_name: str
    signal_start_date: str
    signal_end_date: str
    initial_cash: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_dir": str(self.run_dir),
            "research_config": self.research_config.to_prompt_summary(),
            "winner_spec": self.winner_spec.to_dict(),
            "symbol": self.symbol,
            "region": self.region,
            "benchmark_symbol": self.benchmark_symbol,
            "strategy_name": self.strategy_name,
            "signal_start_date": self.signal_start_date,
            "signal_end_date": self.signal_end_date,
            "initial_cash": self.initial_cash,
        }


@dataclass(frozen=True)
class TigerDailyBarBundle:
    symbol: str
    price_frame: pd.DataFrame
    benchmark_symbol: Optional[str]
    benchmark_frame: Optional[pd.DataFrame]
    trade_start_date: str
    trade_date: str
    warmup_start_date: str
    source: str = "tiger_day_bars"


@dataclass(frozen=True)
class LiveStrategySnapshot:
    symbol: str
    strategy_name: str
    as_of_date: str
    signal_bar_date: str
    next_trade_date: str
    latest_close: float
    signal_ready: bool
    vote_ratio: float
    signal_states: Dict[str, Optional[bool]]
    current_position_amount: float
    in_position: bool
    cooldown_remaining: int
    bars_in_position: int
    next_session_action: str
    action_reasons: Tuple[str, ...]
    target_position_pct: float
    benchmark_symbol: Optional[str] = None
    data_source: str = "tiger_day_bars"
    signal_frame_path: Optional[str] = None
    daily_state_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["action_reasons"] = list(self.action_reasons)
        return payload


@dataclass(frozen=True)
class TigerQuoteSnapshot:
    symbol: str
    source: str
    is_realtime: bool
    quote_timestamp: Optional[str]
    quote_age_seconds: Optional[float]
    latest_price: Optional[float]
    ask_price: Optional[float]
    bid_price: Optional[float]
    open_price: Optional[float]
    prev_close: Optional[float]
    volume: Optional[float]
    raw_payload: Dict[str, Any] = field(default_factory=dict)

    def reference_price(self, action: str) -> Optional[float]:
        if str(action).lower() == "buy":
            candidates = (self.ask_price, self.latest_price, self.open_price, self.prev_close)
        else:
            candidates = (self.bid_price, self.latest_price, self.open_price, self.prev_close)
        for value in candidates:
            if value is not None and value > 0:
                return float(value)
        return None

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["raw_payload"] = _json_safe(self.raw_payload)
        return payload


@dataclass(frozen=True)
class TigerAccountSummary:
    selected_account: str
    selected_account_type: str
    available_cash: float
    base_currency: str
    current_quantity: float
    salable_quantity: float
    open_orders: Tuple[Dict[str, Any], ...]
    config_account: Optional[str] = None
    net_liquidation: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["open_orders"] = list(self.open_orders)
        return payload


@dataclass(frozen=True)
class TigerPositionSnapshot:
    symbol: str
    quantity: float
    salable_quantity: float
    market_value: Optional[float] = None
    average_cost: Optional[float] = None
    currency: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TigerPortfolioSnapshot:
    selected_account: str
    selected_account_type: str
    available_cash: float
    base_currency: str
    positions: Tuple[TigerPositionSnapshot, ...]
    open_orders: Tuple[Dict[str, Any], ...]
    config_account: Optional[str] = None
    net_liquidation: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["positions"] = [item.to_dict() for item in self.positions]
        payload["open_orders"] = list(self.open_orders)
        return payload


@dataclass(frozen=True)
class TigerTradePlan:
    action: str
    quantity: int
    order_type: str
    can_submit: bool
    reason: str
    limit_price: Optional[float] = None
    reference_price: Optional[float] = None
    cash_buffer_pct: float = 1.0
    target_position_pct: float = 0.0
    quote_is_realtime: bool = False
    quote_source: Optional[str] = None
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["warnings"] = list(self.warnings)
        return payload


@dataclass(frozen=True)
class TigerExecutionRecord:
    execution_key: str
    order_id: Optional[int]
    account: str
    trade_date: str
    signal_bar_date: str
    status: str
    filled_quantity: float
    avg_fill_price: Optional[float]
    last_checked_at: str

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TigerExecutionRecord":
        return cls(
            execution_key=str(payload.get("execution_key") or ""),
            order_id=_coerce_int(payload.get("order_id")),
            account=str(payload.get("account") or ""),
            trade_date=str(payload.get("trade_date") or ""),
            signal_bar_date=str(payload.get("signal_bar_date") or ""),
            status=str(payload.get("status") or ""),
            filled_quantity=float(payload.get("filled_quantity") or 0.0),
            avg_fill_price=_coerce_float(payload.get("avg_fill_price")),
            last_checked_at=str(payload.get("last_checked_at") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TigerExecutionLedger:
    path: Path
    records: Dict[str, TigerExecutionRecord]

    @classmethod
    def load(cls, path: Path) -> "TigerExecutionLedger":
        path = Path(path)
        if not path.exists():
            return cls(path=path, records={})
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = {}
        for key, value in (payload.get("records") or {}).items():
            records[str(key)] = TigerExecutionRecord.from_dict(value)
        return cls(path=path, records=records)

    def get(self, execution_key: str) -> Optional[TigerExecutionRecord]:
        return self.records.get(execution_key)

    def upsert(self, record: TigerExecutionRecord) -> None:
        self.records[record.execution_key] = record

    def persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "records": dict((key, value.to_dict()) for key, value in sorted(self.records.items())),
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True)
class TigerExecutionGuard:
    decision: str
    reason: str
    matched_order: Optional[Dict[str, Any]] = None
    ledger_record: Optional[TigerExecutionRecord] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "matched_order": self.matched_order,
            "ledger_record": self.ledger_record.to_dict() if self.ledger_record else None,
        }


def ensure_tiger_sdk() -> Dict[str, Any]:
    try:
        from tigeropen.common.consts import BarPeriod, Market, OrderStatus, QuoteRight, SecurityType
        from tigeropen.common.util.contract_utils import stock_contract
        from tigeropen.common.util.order_utils import limit_order, market_order
        from tigeropen.quote.quote_client import QuoteClient
        from tigeropen.tiger_open_config import TigerOpenClientConfig
        from tigeropen.trade.trade_client import TradeClient
    except ImportError as exc:
        raise RuntimeError("tigeropen is required for Tiger paper trading.") from exc

    return {
        "BarPeriod": BarPeriod,
        "Market": Market,
        "OrderStatus": OrderStatus,
        "QuoteClient": QuoteClient,
        "QuoteRight": QuoteRight,
        "SecurityType": SecurityType,
        "TigerOpenClientConfig": TigerOpenClientConfig,
        "TradeClient": TradeClient,
        "limit_order": limit_order,
        "market_order": market_order,
        "stock_contract": stock_contract,
    }


def resolve_tiger_config_path(path_or_dir: str) -> Path:
    candidate = Path(path_or_dir).expanduser().resolve()
    if candidate.is_file():
        return candidate
    if candidate.is_dir():
        props = candidate / "tiger_openapi_config.properties"
        if props.exists():
            return props
    raise FileNotFoundError("Tiger config not found: %s" % candidate)


def connect_tiger_clients(tiger_config_path: str) -> Tuple[Dict[str, Any], Any, Any, Any]:
    tiger_namespace = ensure_tiger_sdk()
    resolved_path = resolve_tiger_config_path(tiger_config_path)
    config_obj = tiger_namespace["TigerOpenClientConfig"](props_path=str(resolved_path))
    quote_client = tiger_namespace["QuoteClient"](config_obj)
    trade_client = tiger_namespace["TradeClient"](config_obj)
    return tiger_namespace, config_obj, quote_client, trade_client


def mask_account(account: Optional[str]) -> str:
    if not account:
        return ""
    text = str(account)
    if len(text) <= 4:
        return text
    return "%s%s" % ("*" * max(0, len(text) - 4), text[-4:])


def find_latest_run_dir(runs_root: str) -> Path:
    root = Path(runs_root).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError("Run root does not exist: %s" % root)
    candidates = [item for item in root.iterdir() if item.is_dir() and (item / "winner_strategy.py").exists()]
    if not candidates:
        raise FileNotFoundError("No frozen winner run was found under %s." % root)
    return sorted(candidates)[-1]


def _parse_frozen_config(run_dir: Path) -> Dict[str, Any]:
    runner_path = run_dir / "run_winner_backtest.py"
    if not runner_path.exists():
        raise FileNotFoundError("Frozen runner is missing: %s" % runner_path)
    text = runner_path.read_text(encoding="utf-8")
    match = re.search(r"FROZEN_CONFIG\s*=\s*json\.loads\((?P<payload>.+?)\)\n", text, re.DOTALL)
    if not match:
        raise RuntimeError("Could not locate FROZEN_CONFIG in %s." % runner_path)
    payload = ast.literal_eval(match.group("payload"))
    return json.loads(payload)


def load_frozen_strategy_context(
    run_dir: Path,
    provider_uri: Optional[str] = None,
    signal_start_date: Optional[str] = None,
    signal_end_date: Optional[str] = None,
    auto_download_us_data: Optional[bool] = None,
) -> FrozenStrategyContext:
    run_dir = Path(run_dir).expanduser().resolve()
    frozen_config = _parse_frozen_config(run_dir)
    summary_path = run_dir / "winner_summary.json"
    if not summary_path.exists():
        raise FileNotFoundError("Frozen winner summary is missing: %s" % summary_path)
    summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
    spec_payload = json.loads(summary_payload["spec_json"])
    benchmark_symbol = frozen_config.get("benchmark_symbol")
    research_config = ResearchConfig(
        symbol=str(frozen_config["symbol"]).upper(),
        region=str(frozen_config["region"]),
        provider_uri=str(provider_uri or frozen_config.get("provider_uri") or ""),
        start_date=str(signal_start_date or frozen_config["start_date"]),
        end_date=str(signal_end_date or frozen_config["end_date"]),
        benchmark_symbol=benchmark_symbol,
        initial_cash=float(frozen_config["initial_cash"]),
        auto_download_us_data=bool(
            frozen_config["auto_download_us_data"] if auto_download_us_data is None else auto_download_us_data
        ),
        output_dir=str(run_dir),
    )
    winner_spec = CandidateStrategySpec.from_payload(spec_payload, default_benchmark_symbol=benchmark_symbol)
    return FrozenStrategyContext(
        run_dir=run_dir,
        research_config=research_config,
        winner_spec=winner_spec,
        symbol=research_config.symbol.upper(),
        region=research_config.region,
        benchmark_symbol=research_config.resolved_benchmark_symbol(),
        strategy_name=winner_spec.name,
        signal_start_date=research_config.start_date,
        signal_end_date=str(signal_end_date or frozen_config["end_date"]),
        initial_cash=research_config.initial_cash,
    )


def _required_history_bars(spec: CandidateStrategySpec) -> int:
    return int(
        max(
            spec.momentum_window,
            spec.short_momentum_window,
            spec.ema_slow,
            spec.rsi_period + 1,
            spec.macd_slow + spec.macd_signal,
            max(spec.bb_window * 3, 60),
            spec.volume_window,
            spec.market_ma_window,
            spec.atr_window + 1,
        )
    )


def _business_day_backoff(date_text: str, bars: int) -> str:
    return (pd.Timestamp(date_text) - BDay(bars)).strftime("%Y-%m-%d")


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(coerced):
        return None
    return coerced


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=US_MARKET_TZ)
        return value
    if isinstance(value, (int, np.integer, float, np.floating)):
        timestamp = float(value)
        if timestamp <= 0:
            return None
        if timestamp > 1e12:
            return datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for converter in (
            lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")),
            lambda raw: pd.Timestamp(raw).to_pydatetime(),
        ):
            try:
                converted = converter(text)
                if converted.tzinfo is None:
                    return converted.replace(tzinfo=US_MARKET_TZ)
                return converted
            except Exception:
                continue
    return None


def _to_iso_timestamp(value: Any) -> Optional[str]:
    dt = _coerce_datetime(value)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _json_safe(value: Any, _seen: Optional[set[int]] = None) -> Any:
    if _seen is None:
        _seen = set()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and type(value).__module__.startswith("enum"):
        return _json_safe(getattr(value, "value"), _seen)
    if isinstance(value, dict):
        return dict((str(key), _json_safe(item, _seen)) for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, _seen) for item in value]
    if hasattr(value, "__dict__"):
        value_id = id(value)
        if value_id in _seen:
            return str(value)
        _seen.add(value_id)
        return _json_safe(dict(value.__dict__), _seen)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def _normalize_tiger_bars(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ValueError("Tiger returned no daily bars for %s." % symbol)
    required = {"time", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError("Tiger daily bars for %s are missing columns: %s" % (symbol, ", ".join(missing)))
    normalized = frame.copy()
    normalized["datetime"] = (
        pd.to_datetime(normalized["time"], unit="ms", utc=True)
        .dt.tz_convert(US_MARKET_TZ)
        .dt.tz_localize(None)
        .dt.normalize()
    )
    normalized = normalized.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")
    normalized["open_adj"] = normalized["open"].astype(float)
    normalized["high_adj"] = normalized["high"].astype(float)
    normalized["low_adj"] = normalized["low"].astype(float)
    normalized["close_adj"] = normalized["close"].astype(float)
    normalized["open_raw"] = normalized["open_adj"]
    normalized["high_raw"] = normalized["high_adj"]
    normalized["low_raw"] = normalized["low_adj"]
    normalized["close_raw"] = normalized["close_adj"]
    normalized["factor"] = 1.0
    normalized["volume"] = normalized["volume"].astype(float).fillna(0.0)
    return normalized.set_index("datetime")[
        [
            "open_adj",
            "high_adj",
            "low_adj",
            "close_adj",
            "factor",
            "volume",
            "open_raw",
            "high_raw",
            "low_raw",
            "close_raw",
        ]
    ]


def fetch_tiger_daily_bar_bundle(
    *,
    context: FrozenStrategyContext,
    tiger_namespace: Dict[str, Any],
    quote_client: Any,
    executable_trade_date: Optional[str] = None,
    bars_lookback_buffer: int = 30,
) -> TigerDailyBarBundle:
    if context.region.lower() != "us":
        raise RuntimeError("Tiger-only live execution currently supports region=us only.")
    trade_date = str(executable_trade_date or context.signal_end_date)
    warmup_backoff = _required_history_bars(context.winner_spec) + max(0, int(bars_lookback_buffer))
    warmup_start = _business_day_backoff(context.signal_start_date, warmup_backoff)
    right = tiger_namespace["QuoteRight"].BR
    period = tiger_namespace["BarPeriod"].DAY
    price_frame = quote_client.get_bars_by_page(
        context.symbol,
        period=period,
        begin_time=warmup_start,
        end_time=trade_date,
        total=10000,
        page_size=200,
        right=right,
    )
    benchmark_frame = None
    if context.benchmark_symbol:
        benchmark_frame = quote_client.get_bars_by_page(
            context.benchmark_symbol,
            period=period,
            begin_time=warmup_start,
            end_time=trade_date,
            total=10000,
            page_size=200,
            right=right,
        )
    return TigerDailyBarBundle(
        symbol=context.symbol,
        price_frame=_normalize_tiger_bars(price_frame, context.symbol),
        benchmark_symbol=context.benchmark_symbol,
        benchmark_frame=_normalize_tiger_bars(benchmark_frame, context.benchmark_symbol) if benchmark_frame is not None else None,
        trade_start_date=context.signal_start_date,
        trade_date=trade_date,
        warmup_start_date=warmup_start,
    )


def _serialize_signal_state(value: Any) -> Optional[bool]:
    if pd.isna(value):
        return None
    return bool(value)


def _compute_vote_ratio(row: pd.Series, spec: CandidateStrategySpec) -> Tuple[bool, float, Dict[str, Optional[bool]]]:
    signal_states = {}
    required_columns = ["%s_signal" % signal_name for signal_name in spec.enabled_signals]
    for signal_name in spec.enabled_signals:
        value = row.get("%s_signal" % signal_name)
        signal_states[signal_name] = None if pd.isna(value) else bool(value)
    ready = pd.notna(row.get("next_trade_date")) and all(pd.notna(row.get(column)) for column in required_columns)
    if not ready:
        return False, 0.0, signal_states
    vote_count = sum(1 for value in signal_states.values() if value)
    vote_ratio = float(vote_count) / float(len(required_columns))
    return True, vote_ratio, signal_states


def _compute_live_snapshot_from_bundle(
    *,
    context: FrozenStrategyContext,
    bundle: TigerDailyBarBundle,
    output_dir: Path,
) -> LiveStrategySnapshot:
    trade_date_ts = pd.Timestamp(bundle.trade_date)
    price_frame = bundle.price_frame.loc[bundle.price_frame.index < trade_date_ts].copy()
    benchmark_frame = bundle.benchmark_frame
    if benchmark_frame is not None:
        benchmark_frame = benchmark_frame.loc[benchmark_frame.index < trade_date_ts].copy()
    if price_frame.empty:
        raise RuntimeError("Tiger daily bars do not contain a completed bar before trade_date=%s." % bundle.trade_date)
    feature_frame = build_feature_frame(price_frame, benchmark_frame, context.winner_spec)
    if feature_frame.empty:
        raise RuntimeError("Feature frame is empty after building Tiger-only daily signals.")
    feature_frame = feature_frame.sort_index()
    feature_frame.loc[feature_frame.index[-1], "next_trade_date"] = trade_date_ts
    trade_start_ts = pd.Timestamp(bundle.trade_start_date)

    in_position = False
    position_amount = 0.0
    pending_action: Optional[str] = None
    cooldown_remaining = 0
    bars_in_position = 0
    highest_close_since_entry: Optional[float] = None
    signal_rows: List[Dict[str, Any]] = []
    daily_rows: List[Dict[str, Any]] = []

    for date, row in feature_frame.iterrows():
        executed_action = ""
        cooldown_armed_today = False
        trade_enabled = date >= trade_start_ts

        if trade_enabled and pending_action == "buy" and not in_position:
            in_position = True
            position_amount = 1.0
            bars_in_position = 0
            highest_close_since_entry = float(row["close_adj"])
            executed_action = "buy"
        elif trade_enabled and pending_action == "sell" and in_position:
            in_position = False
            position_amount = 0.0
            bars_in_position = 0
            highest_close_since_entry = None
            cooldown_remaining = int(context.winner_spec.cooldown_days)
            cooldown_armed_today = True
            executed_action = "sell"

        pending_action = None
        close_adj = float(row["close_adj"])
        if in_position:
            highest_close_since_entry = close_adj if highest_close_since_entry is None else max(highest_close_since_entry, close_adj)
            bars_in_position += 1

        ready, vote_ratio, signal_states = _compute_vote_ratio(row, context.winner_spec)
        exit_reasons: List[str] = []
        if in_position:
            if "ema_trend" in context.winner_spec.enabled_signals and pd.notna(row.get("ema_slow_value")) and close_adj < float(row["ema_slow_value"]):
                exit_reasons.append("ma_breakdown")
            if context.winner_spec.rsi_take_profit <= 100.0 and pd.notna(row.get("rsi_value")) and float(row["rsi_value"]) >= context.winner_spec.rsi_take_profit:
                exit_reasons.append("rsi_take_profit")
            if context.winner_spec.atr_stop_mult > 0 and pd.notna(row.get("atr_value")) and highest_close_since_entry is not None:
                trailing_level = highest_close_since_entry - context.winner_spec.atr_stop_mult * float(row["atr_value"])
                if close_adj <= trailing_level:
                    exit_reasons.append("atr_trailing_stop")
            if context.winner_spec.max_hold_days > 0 and bars_in_position >= context.winner_spec.max_hold_days:
                exit_reasons.append("max_hold")

        planned_action = ""
        if trade_enabled:
            if in_position:
                if exit_reasons:
                    planned_action = "sell"
                    pending_action = "sell"
            else:
                if not cooldown_armed_today and cooldown_remaining > 0:
                    cooldown_remaining -= 1
                if ready and cooldown_remaining <= 0 and vote_ratio >= context.winner_spec.vote_threshold:
                    planned_action = "buy"
                    pending_action = "buy"

        signal_row = {
            "date": date.strftime("%Y-%m-%d"),
            "trade_date": row["next_trade_date"].strftime("%Y-%m-%d") if pd.notna(row["next_trade_date"]) else None,
            "signal_ready": bool(ready),
            "vote_ratio": float(vote_ratio),
            "planned_action": planned_action,
            "executed_action": executed_action,
            "in_position": bool(in_position),
            "cooldown_remaining": int(cooldown_remaining),
            "bars_in_position": int(bars_in_position),
            "exit_reasons": ",".join(exit_reasons),
            "close_adj": close_adj,
        }
        for signal_name in context.winner_spec.enabled_signals:
            signal_row["%s_signal" % signal_name] = _serialize_signal_state(signal_states.get(signal_name))
        signal_rows.append(signal_row)
        daily_rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "in_position": bool(in_position),
                "position_amount": float(position_amount),
                "cooldown_remaining": int(cooldown_remaining),
                "bars_in_position": int(bars_in_position),
                "close_adj": close_adj,
                "executed_action": executed_action,
                "planned_action": planned_action,
            }
        )

    signal_frame = pd.DataFrame(signal_rows)
    daily_state = pd.DataFrame(daily_rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    signal_path = output_dir / "tiger_live_signal_frame.csv"
    daily_path = output_dir / "tiger_live_daily_state.csv"
    signal_frame.to_csv(signal_path, index=False)
    daily_state.to_csv(daily_path, index=False)

    last_row = signal_frame.iloc[-1]
    signal_states = {}
    for signal_name in context.winner_spec.enabled_signals:
        signal_states[signal_name] = _serialize_signal_state(last_row.get("%s_signal" % signal_name))
    action_reasons = tuple(filter(None, str(last_row.get("exit_reasons") or "").split(",")))
    signal_bar_date = str(last_row["date"])
    next_trade_date = str(last_row["trade_date"] or bundle.trade_date)

    return LiveStrategySnapshot(
        symbol=context.symbol,
        strategy_name=context.strategy_name,
        as_of_date=signal_bar_date,
        signal_bar_date=signal_bar_date,
        next_trade_date=next_trade_date,
        latest_close=float(last_row["close_adj"]),
        signal_ready=bool(last_row["signal_ready"]),
        vote_ratio=float(last_row["vote_ratio"]),
        signal_states=signal_states,
        current_position_amount=float(daily_state.iloc[-1]["position_amount"]),
        in_position=bool(last_row["in_position"]),
        cooldown_remaining=int(last_row["cooldown_remaining"]),
        bars_in_position=int(last_row["bars_in_position"]),
        next_session_action=str(last_row["planned_action"] or ""),
        action_reasons=action_reasons,
        target_position_pct=float(context.winner_spec.position_size_pct),
        benchmark_symbol=context.benchmark_symbol,
        data_source=bundle.source,
        signal_frame_path=str(signal_path),
        daily_state_path=str(daily_path),
    )


def build_live_strategy_snapshot(
    *,
    context: FrozenStrategyContext,
    output_dir: Path,
    executable_trade_date: Optional[str] = None,
    tiger_namespace: Optional[Dict[str, Any]] = None,
    quote_client: Any = None,
    bar_bundle: Optional[TigerDailyBarBundle] = None,
    bars_lookback_buffer: int = 30,
) -> LiveStrategySnapshot:
    if bar_bundle is None:
        if tiger_namespace is None or quote_client is None:
            raise ValueError("Tiger namespace and quote client are required when bar_bundle is not provided.")
        bar_bundle = fetch_tiger_daily_bar_bundle(
            context=context,
            tiger_namespace=tiger_namespace,
            quote_client=quote_client,
            executable_trade_date=executable_trade_date,
            bars_lookback_buffer=bars_lookback_buffer,
        )
    return _compute_live_snapshot_from_bundle(context=context, bundle=bar_bundle, output_dir=Path(output_dir))


def _normalize_order_status(status: Any) -> str:
    if status is None:
        return ""
    value = getattr(status, "value", status)
    text = str(value)
    if text.startswith("OrderStatus."):
        text = text.split(".", 1)[1]
    mapping = {
        "PENDING_NEW": "PendingNew",
        "NEW": "Initial",
        "HELD": "Submitted",
        "PARTIALLY_FILLED": "PartiallyFilled",
        "FILLED": "Filled",
        "CANCELLED": "Cancelled",
        "PENDING_CANCEL": "PendingCancel",
        "REJECTED": "Inactive",
        "EXPIRED": "Invalid",
    }
    return mapping.get(text, text)


def normalize_tiger_order(order: Any) -> Dict[str, Any]:
    contract = getattr(order, "contract", None)
    symbol = str(
        getattr(order, "symbol", None)
        or getattr(contract, "symbol", None)
        or getattr(contract, "local_symbol", None)
        or ""
    ).upper()
    status = _normalize_order_status(getattr(order, "status", None))
    filled_quantity = _coerce_float(getattr(order, "filled", None)) or 0.0
    return {
        "order_id": _coerce_int(getattr(order, "order_id", None) or getattr(order, "id", None)),
        "action": str(getattr(order, "action", "") or "").upper(),
        "quantity": _coerce_float(getattr(order, "quantity", None)) or 0.0,
        "filled_quantity": filled_quantity,
        "avg_fill_price": _coerce_float(getattr(order, "avg_fill_price", None)),
        "status": status,
        "user_mark": getattr(order, "user_mark", None),
        "external_id": getattr(order, "external_id", None),
        "symbol": symbol,
        "limit_price": _coerce_float(getattr(order, "limit_price", None)),
        "is_open": status in OPEN_ORDER_STATUSES,
        "is_terminal": status in TERMINAL_ORDER_STATUSES,
        "reason": getattr(order, "reason", None),
        "trade_time": _to_iso_timestamp(getattr(order, "trade_time", None)),
        "update_time": _to_iso_timestamp(getattr(order, "update_time", None)),
    }


def select_paper_account(
    *,
    config_account: Optional[str],
    managed_accounts: Sequence[Any],
    requested_paper_account: Optional[str] = None,
) -> Tuple[str, str]:
    normalized = []
    for item in managed_accounts:
        normalized.append(
            (
                str(getattr(item, "account", "") or ""),
                str(getattr(item, "account_type", "") or "").upper(),
            )
        )
    if requested_paper_account:
        requested = str(requested_paper_account)
        for account, account_type in normalized:
            if account == requested and account_type == "PAPER":
                return account, account_type
        raise ValueError("Requested Tiger paper account was not found or is not PAPER: %s" % requested)
    if config_account:
        config_text = str(config_account)
        for account, account_type in normalized:
            if account == config_text and account_type == "PAPER":
                return account, account_type
    for account, account_type in normalized:
        if account_type == "PAPER":
            return account, account_type
    raise ValueError("No Tiger PAPER account is available in the managed account list.")


def _extract_available_cash(assets: Any) -> Tuple[float, str, Optional[float]]:
    if not assets:
        return 0.0, "USD", None
    asset = assets[0] if isinstance(assets, list) else assets
    summary = getattr(asset, "summary", None)
    segments = getattr(asset, "segments", None) or {}
    security_segment = segments.get("S") if hasattr(segments, "get") else None
    currency = str(getattr(summary, "currency", None) or getattr(security_segment, "currency", None) or "USD")
    available_cash = _coerce_float(getattr(security_segment, "available_funds", None))
    if available_cash is None:
        available_cash = _coerce_float(getattr(summary, "available_funds", None))
    if available_cash is None:
        available_cash = _coerce_float(getattr(security_segment, "cash", None))
    if available_cash is None:
        available_cash = _coerce_float(getattr(summary, "cash", None))
    net_liquidation = _coerce_float(getattr(security_segment, "net_liquidation", None))
    if net_liquidation is None:
        net_liquidation = _coerce_float(getattr(summary, "net_liquidation", None))
    return float(available_cash or 0.0), currency, net_liquidation


def _extract_position_quantities(positions: Sequence[Any], symbol: str) -> Tuple[float, float]:
    total_quantity = 0.0
    total_salable = 0.0
    for position in positions or []:
        contract = getattr(position, "contract", None)
        position_symbol = str(getattr(contract, "symbol", None) or getattr(position, "symbol", None) or "").upper()
        if position_symbol != str(symbol).upper():
            continue
        total_quantity += float(
            _coerce_float(getattr(position, "quantity", None))
            or _coerce_float(getattr(position, "position_qty", None))
            or 0.0
        )
        total_salable += float(
            _coerce_float(getattr(position, "salable_qty", None))
            or _coerce_float(getattr(position, "saleable", None))
            or _coerce_float(getattr(position, "salable", None))
            or _coerce_float(getattr(position, "available_quantity", None))
            or _coerce_float(getattr(position, "quantity", None))
            or 0.0
        )
    return total_quantity, total_salable


def normalize_tiger_position(position: Any) -> TigerPositionSnapshot:
    contract = getattr(position, "contract", None)
    symbol = str(getattr(contract, "symbol", None) or getattr(position, "symbol", None) or "").upper()
    quantity = float(
        _coerce_float(getattr(position, "quantity", None))
        or _coerce_float(getattr(position, "position_qty", None))
        or 0.0
    )
    salable_quantity = float(
        _coerce_float(getattr(position, "salable_qty", None))
        or _coerce_float(getattr(position, "saleable", None))
        or _coerce_float(getattr(position, "salable", None))
        or _coerce_float(getattr(position, "available_quantity", None))
        or _coerce_float(getattr(position, "quantity", None))
        or 0.0
    )
    market_value = (
        _coerce_float(getattr(position, "market_value", None))
        or _coerce_float(getattr(position, "market_val", None))
        or _coerce_float(getattr(position, "position_market_value", None))
    )
    average_cost = (
        _coerce_float(getattr(position, "average_cost", None))
        or _coerce_float(getattr(position, "avg_cost", None))
        or _coerce_float(getattr(position, "cost_price", None))
        or _coerce_float(getattr(position, "average_price", None))
    )
    currency = str(getattr(position, "currency", None) or getattr(contract, "currency", None) or "USD")
    return TigerPositionSnapshot(
        symbol=symbol,
        quantity=quantity,
        salable_quantity=salable_quantity,
        market_value=market_value,
        average_cost=average_cost,
        currency=currency,
    )


def build_tiger_portfolio_snapshot(
    *,
    trade_client: Any,
    config_account: Optional[str],
    requested_paper_account: Optional[str] = None,
) -> TigerPortfolioSnapshot:
    managed_accounts = trade_client.get_managed_accounts() or []
    selected_account, selected_account_type = select_paper_account(
        config_account=config_account,
        managed_accounts=managed_accounts,
        requested_paper_account=requested_paper_account,
    )
    positions = trade_client.get_positions(account=selected_account, sec_type="STK") or []
    assets = trade_client.get_assets(account=selected_account)
    open_orders = trade_client.get_open_orders(account=selected_account, sec_type="STK") or []
    available_cash, base_currency, net_liquidation = _extract_available_cash(assets)
    normalized_positions = tuple(
        sorted(
            (normalize_tiger_position(item) for item in positions),
            key=lambda item: (-(item.market_value or 0.0), item.symbol),
        )
    )
    normalized_orders = tuple(
        sorted(
            (normalize_tiger_order(item) for item in open_orders),
            key=lambda item: (str(item.get("symbol") or ""), int(item.get("order_id") or 0)),
        )
    )
    return TigerPortfolioSnapshot(
        selected_account=selected_account,
        selected_account_type=selected_account_type,
        available_cash=available_cash,
        base_currency=base_currency,
        positions=normalized_positions,
        open_orders=normalized_orders,
        config_account=str(config_account) if config_account else None,
        net_liquidation=net_liquidation,
    )


def build_tiger_account_summary(
    *,
    trade_client: Any,
    config_account: Optional[str],
    symbol: str,
    region: str,
    requested_paper_account: Optional[str] = None,
    tiger_namespace: Optional[Dict[str, Any]] = None,
) -> TigerAccountSummary:
    managed_accounts = trade_client.get_managed_accounts() or []
    selected_account, selected_account_type = select_paper_account(
        config_account=config_account,
        managed_accounts=managed_accounts,
        requested_paper_account=requested_paper_account,
    )
    market = "US" if region.lower() == "us" else region.upper()
    positions = trade_client.get_positions(account=selected_account, sec_type="STK") or []
    assets = trade_client.get_assets(account=selected_account)
    open_orders = trade_client.get_open_orders(account=selected_account, sec_type="STK", market=market, symbol=symbol) or []
    current_quantity, salable_quantity = _extract_position_quantities(positions, symbol)
    available_cash, base_currency, net_liquidation = _extract_available_cash(assets)
    normalized_orders = tuple(normalize_tiger_order(item) for item in open_orders)
    return TigerAccountSummary(
        selected_account=selected_account,
        selected_account_type=selected_account_type,
        available_cash=available_cash,
        base_currency=base_currency,
        current_quantity=current_quantity,
        salable_quantity=salable_quantity,
        open_orders=normalized_orders,
        config_account=str(config_account) if config_account else None,
        net_liquidation=net_liquidation,
    )


def fetch_tiger_account_summary(
    *,
    tiger_config_path: str,
    symbol: str,
    region: str,
    requested_paper_account: Optional[str] = None,
) -> Tuple[Dict[str, Any], Any, TigerAccountSummary]:
    tiger_namespace, config_obj, _quote_client, trade_client = connect_tiger_clients(tiger_config_path)
    summary = build_tiger_account_summary(
        trade_client=trade_client,
        config_account=getattr(config_obj, "account", None),
        symbol=symbol,
        region=region,
        requested_paper_account=requested_paper_account,
        tiger_namespace=tiger_namespace,
    )
    return tiger_namespace, trade_client, summary


def fetch_tiger_portfolio_snapshot(
    *,
    tiger_config_path: str,
    requested_paper_account: Optional[str] = None,
) -> Tuple[Dict[str, Any], Any, TigerPortfolioSnapshot]:
    tiger_namespace, config_obj, _quote_client, trade_client = connect_tiger_clients(tiger_config_path)
    summary = build_tiger_portfolio_snapshot(
        trade_client=trade_client,
        config_account=getattr(config_obj, "account", None),
        requested_paper_account=requested_paper_account,
    )
    return tiger_namespace, trade_client, summary


def fetch_tiger_quote_snapshot(
    *,
    quote_client: Any,
    symbol: str,
    allow_delayed_preview: bool = True,
) -> TigerQuoteSnapshot:
    now_utc = datetime.now(timezone.utc)
    try:
        briefs = quote_client.get_briefs([symbol], include_hour_trading=True, include_ask_bid=True) or []
        brief = briefs[0] if briefs else None
        if brief is None:
            raise RuntimeError("Tiger returned no live quote brief for %s." % symbol)
        quote_time = _coerce_datetime(getattr(brief, "latest_time", None))
        quote_age = (now_utc - quote_time.astimezone(timezone.utc)).total_seconds() if quote_time is not None else None
        return TigerQuoteSnapshot(
            symbol=str(getattr(brief, "symbol", symbol) or symbol).upper(),
            source="tiger_brief",
            is_realtime=True,
            quote_timestamp=_to_iso_timestamp(quote_time),
            quote_age_seconds=quote_age,
            latest_price=_coerce_float(getattr(brief, "latest_price", None)),
            ask_price=_coerce_float(getattr(brief, "ask_price", None)),
            bid_price=_coerce_float(getattr(brief, "bid_price", None)),
            open_price=_coerce_float(getattr(brief, "open_price", None)),
            prev_close=_coerce_float(getattr(brief, "prev_close", None)),
            volume=_coerce_float(getattr(brief, "volume", None)),
            raw_payload=dict(getattr(brief, "__dict__", {}) or {}),
        )
    except Exception as exc:
        if not allow_delayed_preview:
            raise
        delayed = quote_client.get_stock_delay_briefs([symbol])
        if delayed is None or delayed.empty:
            raise RuntimeError("Tiger live quote failed and no delayed quote is available for %s: %s" % (symbol, exc)) from exc
        row = delayed.iloc[0]
        quote_time = _coerce_datetime(row.get("time"))
        quote_age = (now_utc - quote_time.astimezone(timezone.utc)).total_seconds() if quote_time is not None else None
        raw_payload = {}
        for key, value in row.to_dict().items():
            raw_payload[str(key)] = value.item() if hasattr(value, "item") else value
        raw_payload["live_quote_error"] = str(exc)
        return TigerQuoteSnapshot(
            symbol=str(row.get("symbol") or symbol).upper(),
            source="tiger_delay_brief",
            is_realtime=False,
            quote_timestamp=_to_iso_timestamp(quote_time),
            quote_age_seconds=quote_age,
            latest_price=_coerce_float(row.get("close")),
            ask_price=None,
            bid_price=None,
            open_price=_coerce_float(row.get("open")),
            prev_close=_coerce_float(row.get("pre_close")),
            volume=_coerce_float(row.get("volume")),
            raw_payload=raw_payload,
        )


def build_tiger_trade_plan(
    *,
    snapshot: LiveStrategySnapshot,
    account_summary: TigerAccountSummary,
    order_type: str,
    quote_snapshot: Optional[TigerQuoteSnapshot] = None,
    cash_buffer_pct: float = 0.98,
    limit_price: Optional[float] = None,
    max_quote_staleness_sec: Optional[int] = None,
    require_realtime_quote: bool = True,
) -> TigerTradePlan:
    action = str(snapshot.next_session_action or "").lower()
    warnings: List[str] = []
    if action not in {"buy", "sell"}:
        return TigerTradePlan(
            action="hold",
            quantity=0,
            order_type=order_type,
            can_submit=False,
            reason="no_executable_signal",
            target_position_pct=float(snapshot.target_position_pct),
        )
    if not snapshot.signal_ready:
        return TigerTradePlan(
            action="hold",
            quantity=0,
            order_type=order_type,
            can_submit=False,
            reason="signal_not_ready",
            target_position_pct=float(snapshot.target_position_pct),
        )
    if quote_snapshot is None:
        return TigerTradePlan(
            action=action,
            quantity=0,
            order_type=order_type,
            can_submit=False,
            reason="quote_unavailable",
            target_position_pct=float(snapshot.target_position_pct),
        )
    if require_realtime_quote and not quote_snapshot.is_realtime:
        warnings.append("realtime_quote_unavailable")
        return TigerTradePlan(
            action=action,
            quantity=0,
            order_type=order_type,
            can_submit=False,
            reason="realtime_quote_required",
            reference_price=quote_snapshot.reference_price(action),
            cash_buffer_pct=float(cash_buffer_pct),
            target_position_pct=float(snapshot.target_position_pct),
            quote_is_realtime=quote_snapshot.is_realtime,
            quote_source=quote_snapshot.source,
            warnings=tuple(warnings),
        )
    if max_quote_staleness_sec is not None and quote_snapshot.quote_age_seconds is not None:
        if quote_snapshot.quote_age_seconds > float(max_quote_staleness_sec):
            warnings.append("quote_stale")
            return TigerTradePlan(
                action=action,
                quantity=0,
                order_type=order_type,
                can_submit=False,
                reason="quote_stale",
                reference_price=quote_snapshot.reference_price(action),
                cash_buffer_pct=float(cash_buffer_pct),
                target_position_pct=float(snapshot.target_position_pct),
                quote_is_realtime=quote_snapshot.is_realtime,
                quote_source=quote_snapshot.source,
                warnings=tuple(warnings),
            )

    reference_price = quote_snapshot.reference_price(action)
    if reference_price is None or reference_price <= 0:
        return TigerTradePlan(
            action=action,
            quantity=0,
            order_type=order_type,
            can_submit=False,
            reason="invalid_quote_reference_price",
            cash_buffer_pct=float(cash_buffer_pct),
            target_position_pct=float(snapshot.target_position_pct),
            quote_is_realtime=quote_snapshot.is_realtime,
            quote_source=quote_snapshot.source,
        )

    if action == "buy":
        if account_summary.current_quantity > 0:
            return TigerTradePlan(
                action="hold",
                quantity=0,
                order_type=order_type,
                can_submit=False,
                reason="existing_long_position",
                reference_price=reference_price,
                cash_buffer_pct=float(cash_buffer_pct),
                target_position_pct=float(snapshot.target_position_pct),
                quote_is_realtime=quote_snapshot.is_realtime,
                quote_source=quote_snapshot.source,
            )
        budget = max(0.0, float(account_summary.available_cash) * float(snapshot.target_position_pct) * float(cash_buffer_pct))
        quantity = int(budget // reference_price)
        if quantity < 1:
            return TigerTradePlan(
                action="buy",
                quantity=0,
                order_type=order_type,
                can_submit=False,
                reason="insufficient_cash",
                reference_price=reference_price,
                cash_buffer_pct=float(cash_buffer_pct),
                target_position_pct=float(snapshot.target_position_pct),
                quote_is_realtime=quote_snapshot.is_realtime,
                quote_source=quote_snapshot.source,
            )
    else:
        quantity = int(min(float(account_summary.current_quantity), float(account_summary.salable_quantity)))
        if quantity < 1:
            return TigerTradePlan(
                action="sell",
                quantity=0,
                order_type=order_type,
                can_submit=False,
                reason="no_salable_quantity",
                reference_price=reference_price,
                cash_buffer_pct=float(cash_buffer_pct),
                target_position_pct=float(snapshot.target_position_pct),
                quote_is_realtime=quote_snapshot.is_realtime,
                quote_source=quote_snapshot.source,
            )

    resolved_limit = None
    if order_type == "limit":
        resolved_limit = _coerce_float(limit_price)
        if resolved_limit is None or resolved_limit <= 0:
            return TigerTradePlan(
                action=action,
                quantity=0,
                order_type=order_type,
                can_submit=False,
                reason="limit_price_required",
                reference_price=reference_price,
                cash_buffer_pct=float(cash_buffer_pct),
                target_position_pct=float(snapshot.target_position_pct),
                quote_is_realtime=quote_snapshot.is_realtime,
                quote_source=quote_snapshot.source,
            )

    return TigerTradePlan(
        action=action,
        quantity=quantity,
        order_type=order_type,
        can_submit=True,
        reason="ready",
        limit_price=resolved_limit,
        reference_price=reference_price,
        cash_buffer_pct=float(cash_buffer_pct),
        target_position_pct=float(snapshot.target_position_pct),
        quote_is_realtime=quote_snapshot.is_realtime,
        quote_source=quote_snapshot.source,
        warnings=tuple(warnings),
    )


def build_execution_key(
    *,
    paper_account: str,
    symbol: str,
    strategy_name: str,
    trade_date: str,
    signal_bar_date: str,
    action: str,
) -> str:
    return "%s:%s:%s:%s:%s:%s" % (
        str(paper_account),
        str(symbol).upper(),
        str(strategy_name),
        str(trade_date),
        str(signal_bar_date),
        str(action).lower(),
    )


def _execution_key_from_order(order_payload: Dict[str, Any]) -> Optional[str]:
    for field_name in ("external_id", "user_mark"):
        value = order_payload.get(field_name)
        if value:
            return str(value)
    return None


def assess_execution_guard(
    *,
    execution_key: str,
    symbol: str,
    action: str,
    open_orders: Sequence[Dict[str, Any]],
    historical_orders: Sequence[Dict[str, Any]],
    ledger_record: Optional[TigerExecutionRecord],
) -> TigerExecutionGuard:
    symbol_upper = str(symbol).upper()
    relevant_open_orders = [order for order in open_orders if str(order.get("symbol") or "").upper() == symbol_upper]
    relevant_history = [order for order in historical_orders if str(order.get("symbol") or "").upper() == symbol_upper]
    same_key_open = [order for order in relevant_open_orders if _execution_key_from_order(order) == execution_key]
    if same_key_open:
        chosen = sorted(same_key_open, key=lambda item: int(item.get("order_id") or 0))[-1]
        return TigerExecutionGuard(
            decision="adopt_existing_order",
            reason="same_execution_key_open_order",
            matched_order=chosen,
            ledger_record=ledger_record,
        )

    same_key_history = [order for order in relevant_history if _execution_key_from_order(order) == execution_key]
    for order in same_key_history:
        filled_quantity = float(order.get("filled_quantity") or 0.0)
        status = str(order.get("status") or "")
        if status == "Filled" or (status in {"Cancelled", "Invalid", "Inactive"} and filled_quantity > 0):
            return TigerExecutionGuard(
                decision="already_completed",
                reason="same_execution_key_terminal_order",
                matched_order=order,
                ledger_record=ledger_record,
            )
        if status in TERMINAL_ORDER_STATUSES:
            return TigerExecutionGuard(
                decision="already_processed",
                reason="same_execution_key_terminal_order",
                matched_order=order,
                ledger_record=ledger_record,
            )

    if ledger_record is not None:
        if ledger_record.status in {"Filled", "PartialFillCancelled"}:
            return TigerExecutionGuard(decision="already_completed", reason="same_execution_key_in_ledger", ledger_record=ledger_record)
        if ledger_record.status in COMPLETED_EXECUTION_STATUSES:
            return TigerExecutionGuard(decision="already_processed", reason="same_execution_key_in_ledger", ledger_record=ledger_record)
        if ledger_record.order_id:
            return TigerExecutionGuard(decision="skip", reason="ledger_pending_manual_review", ledger_record=ledger_record)

    for order in relevant_open_orders:
        if _execution_key_from_order(order) == execution_key:
            continue
        order_action = str(order.get("action") or "").upper()
        if order_action == str(action).upper():
            return TigerExecutionGuard(decision="skip", reason="foreign_same_side_open_order", matched_order=order, ledger_record=ledger_record)
        return TigerExecutionGuard(decision="skip", reason="foreign_open_order_present", matched_order=order, ledger_record=ledger_record)

    return TigerExecutionGuard(decision="submit_new_order", reason="no_conflicts", ledger_record=ledger_record)


@contextlib.contextmanager
def execution_ledger_lock(path: Path, timeout_seconds: float = 5.0, poll_seconds: float = 0.05) -> Iterator[TigerExecutionLedger]:
    path = Path(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for Tiger execution ledger lock: %s" % lock_path)
            time.sleep(poll_seconds)
    ledger = TigerExecutionLedger.load(path)
    try:
        yield ledger
        ledger.persist()
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def upsert_execution_record(
    *,
    ledger_path: Path,
    execution_key: str,
    order_id: Optional[int],
    account: str,
    trade_date: str,
    signal_bar_date: str,
    status: str,
    filled_quantity: float,
    avg_fill_price: Optional[float],
) -> TigerExecutionRecord:
    record = TigerExecutionRecord(
        execution_key=execution_key,
        order_id=_coerce_int(order_id),
        account=str(account),
        trade_date=str(trade_date),
        signal_bar_date=str(signal_bar_date),
        status=str(status),
        filled_quantity=float(filled_quantity),
        avg_fill_price=_coerce_float(avg_fill_price),
        last_checked_at=datetime.now(timezone.utc).isoformat(),
    )
    with execution_ledger_lock(ledger_path) as ledger:
        ledger.upsert(record)
    return record


def get_execution_record(ledger_path: Path, execution_key: str) -> Optional[TigerExecutionRecord]:
    with execution_ledger_lock(ledger_path) as ledger:
        return ledger.get(execution_key)


def create_tiger_order(
    *,
    tiger_namespace: Dict[str, Any],
    account: str,
    symbol: str,
    region: str,
    plan: TigerTradePlan,
    user_mark: str,
    external_id: Optional[str] = None,
) -> Any:
    if plan.order_type == "limit":
        order = tiger_namespace["limit_order"](
            account,
            tiger_namespace["stock_contract"](str(symbol).upper(), "USD"),
            plan.action.upper(),
            int(plan.quantity),
            float(plan.limit_price),
            time_in_force="DAY",
        )
    else:
        order = tiger_namespace["market_order"](
            account,
            tiger_namespace["stock_contract"](str(symbol).upper(), "USD"),
            plan.action.upper(),
            int(plan.quantity),
            time_in_force="DAY",
        )
    order.outside_rth = False
    order.user_mark = str(user_mark)
    order.external_id = str(external_id or user_mark)
    return order


def fetch_tiger_symbol_orders(
    *,
    trade_client: Any,
    account: str,
    symbol: str,
    region: str,
    trade_date: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    market = "US" if region.lower() == "us" else region.upper()
    open_orders = trade_client.get_open_orders(account=account, sec_type="STK", market=market, symbol=symbol) or []
    history_orders = trade_client.get_orders(
        account=account,
        sec_type="STK",
        market=market,
        symbol=symbol,
        start_time=trade_date,
        end_time=(pd.Timestamp(trade_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        limit=200,
    ) or []
    return [normalize_tiger_order(item) for item in open_orders], [normalize_tiger_order(item) for item in history_orders]


def _find_order_by_id_with_fallback(
    *,
    trade_client: Any,
    account: str,
    order_id: int,
    trade_date: Optional[str],
    execution_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    order_id_value = int(order_id)
    candidates: List[Any] = []
    with contextlib.suppress(Exception):
        candidates.extend(trade_client.get_open_orders(account=account) or [])
    if trade_date:
        with contextlib.suppress(Exception):
            candidates.extend(
                trade_client.get_orders(
                    account=account,
                    start_time=trade_date,
                    end_time=(pd.Timestamp(trade_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    limit=200,
                    is_brief=False,
                )
                or []
            )
    for item in candidates:
        normalized = normalize_tiger_order(item)
        candidate_execution_key = _execution_key_from_order(normalized)
        if int(normalized.get("order_id") or 0) == order_id_value:
            return normalized
        if execution_key and candidate_execution_key == execution_key:
            return normalized
    return None


def poll_tiger_order_to_terminal(
    *,
    trade_client: Any,
    account: str,
    order_id: int,
    deadline: datetime,
    poll_seconds: int,
    ledger_path: Optional[Path] = None,
    execution_key: Optional[str] = None,
    signal_bar_date: Optional[str] = None,
    trade_date: Optional[str] = None,
) -> Dict[str, Any]:
    cancel_requested = False
    cancel_deadline = deadline + timedelta(seconds=30)
    while True:
        now_utc = datetime.now(timezone.utc)
        normalized: Optional[Dict[str, Any]] = None
        try:
            order = trade_client.get_order(account=account, order_id=order_id, is_brief=False)
            if order is not None:
                normalized = normalize_tiger_order(order)
        except Exception:
            normalized = None
        if normalized is None:
            normalized = _find_order_by_id_with_fallback(
                trade_client=trade_client,
                account=account,
                order_id=order_id,
                trade_date=trade_date,
                execution_key=execution_key,
            )
        if normalized is None:
            if now_utc >= deadline and not cancel_requested:
                with contextlib.suppress(Exception):
                    trade_client.cancel_order(account=account, order_id=order_id)
                cancel_requested = True
            elif cancel_requested and now_utc >= cancel_deadline:
                return {
                    "order_id": order_id,
                    "final_order_status": "not_found_after_submission",
                    "filled_quantity": 0.0,
                    "avg_fill_price": None,
                    "reason": "order_not_visible",
                }
            time.sleep(max(1, int(poll_seconds)))
            continue
        final_status = normalized["status"]
        filled_quantity = float(normalized.get("filled_quantity") or 0.0)
        if final_status in TERMINAL_ORDER_STATUSES:
            if final_status == "Cancelled" and filled_quantity > 0:
                final_status = "PartialFillCancelled"
            result = {
                "order_id": normalized["order_id"],
                "final_order_status": final_status,
                "filled_quantity": filled_quantity,
                "avg_fill_price": normalized.get("avg_fill_price"),
                "reason": normalized.get("reason"),
            }
            if ledger_path is not None and execution_key and signal_bar_date and trade_date:
                upsert_execution_record(
                    ledger_path=ledger_path,
                    execution_key=execution_key,
                    order_id=normalized["order_id"],
                    account=account,
                    trade_date=trade_date,
                    signal_bar_date=signal_bar_date,
                    status=final_status,
                    filled_quantity=filled_quantity,
                    avg_fill_price=normalized.get("avg_fill_price"),
                )
            return result

        if ledger_path is not None and execution_key and signal_bar_date and trade_date:
            upsert_execution_record(
                ledger_path=ledger_path,
                execution_key=execution_key,
                order_id=normalized["order_id"],
                account=account,
                trade_date=trade_date,
                signal_bar_date=signal_bar_date,
                status=final_status,
                filled_quantity=filled_quantity,
                avg_fill_price=normalized.get("avg_fill_price"),
            )

        if now_utc >= deadline and not cancel_requested:
            trade_client.cancel_order(account=account, order_id=order_id)
            cancel_requested = True
        elif cancel_requested and now_utc >= cancel_deadline:
            result = {
                "order_id": normalized["order_id"],
                "final_order_status": "cancel_timeout",
                "filled_quantity": filled_quantity,
                "avg_fill_price": normalized.get("avg_fill_price"),
                "reason": "cancel_timeout",
            }
            if ledger_path is not None and execution_key and signal_bar_date and trade_date:
                upsert_execution_record(
                    ledger_path=ledger_path,
                    execution_key=execution_key,
                    order_id=normalized["order_id"],
                    account=account,
                    trade_date=trade_date,
                    signal_bar_date=signal_bar_date,
                    status="cancel_timeout",
                    filled_quantity=filled_quantity,
                    avg_fill_price=normalized.get("avg_fill_price"),
                )
            return result

        time.sleep(max(1, int(poll_seconds)))
