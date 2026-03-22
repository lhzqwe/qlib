from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

import qlib
from qlib.config import REG_CN, REG_US
from qlib.data import D
from qlib.tests.data import GetData
from qlib.utils import exists_qlib_data

from codex_client import CodexResponsesClient
from prompts import ALLOWED_SIGNALS, build_round_prompt, build_schema_prompt, build_system_prompt


OPEN_COST = 0.0005
CLOSE_COST = 0.0015
MIN_COST = 1.0
DEFAULT_MAX_ROUNDS = 8
DEFAULT_CANDIDATES_PER_ROUND = 6
DEFAULT_PATIENCE = 2
DEFAULT_TIME_BUDGET_SEC = 900
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent


@dataclass
class ResearchConfig:
    symbol: str
    region: str = "us"
    provider_uri: str = "~/.qlib/qlib_data/us_data"
    start_date: str = "2018-01-01"
    end_date: Optional[str] = None
    benchmark_symbol: Optional[str] = None
    initial_cash: float = 100000.0
    llm_model: str = "gpt-5.4"
    auth_profile_id: Optional[str] = None
    max_rounds: int = DEFAULT_MAX_ROUNDS
    candidates_per_round: int = DEFAULT_CANDIDATES_PER_ROUND
    patience: int = DEFAULT_PATIENCE
    time_budget_sec: int = DEFAULT_TIME_BUDGET_SEC
    output_dir: str = str(SCRIPT_DIR / "output")
    auto_download_us_data: bool = True
    state_dir: Optional[str] = None

    def resolved_benchmark_symbol(self) -> Optional[str]:
        if self.benchmark_symbol:
            return self.benchmark_symbol.upper()
        if self.region.lower() == "us":
            return "QQQ"
        return None

    def to_prompt_summary(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["benchmark_symbol"] = self.resolved_benchmark_symbol()
        return payload


@dataclass(frozen=True)
class CandidateStrategySpec:
    name: str
    benchmark_symbol: Optional[str] = None
    enabled_signals: Tuple[str, ...] = field(default_factory=lambda: ("momentum", "ema_trend"))
    momentum_window: int = 63
    short_momentum_window: int = 21
    ema_fast: int = 12
    ema_slow: int = 26
    rsi_period: int = 14
    rsi_entry_midline: float = 55.0
    rsi_take_profit: float = 101.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_window: int = 20
    bb_percentile: float = 0.35
    volume_window: int = 20
    volume_ratio_threshold: float = 1.05
    market_ma_window: int = 50
    atr_window: int = 14
    atr_stop_mult: float = 0.0
    cooldown_days: int = 0
    max_hold_days: int = 0
    position_size_pct: float = 0.95
    vote_threshold: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["enabled_signals"] = list(self.enabled_signals)
        return payload

    def fingerprint(self) -> str:
        payload = self.to_dict()
        payload.pop("name", None)
        normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_payload(cls, payload: Dict[str, Any], default_benchmark_symbol: Optional[str]) -> "CandidateStrategySpec":
        if not isinstance(payload, dict):
            raise ValueError("Candidate payload must be an object.")

        allowed_fields = {
            "name",
            "benchmark_symbol",
            "enabled_signals",
            "momentum_window",
            "short_momentum_window",
            "ema_fast",
            "ema_slow",
            "rsi_period",
            "rsi_entry_midline",
            "rsi_take_profit",
            "macd_fast",
            "macd_slow",
            "macd_signal",
            "bb_window",
            "bb_percentile",
            "volume_window",
            "volume_ratio_threshold",
            "market_ma_window",
            "atr_window",
            "atr_stop_mult",
            "cooldown_days",
            "max_hold_days",
            "position_size_pct",
            "vote_threshold",
        }
        unknown_fields = sorted(set(payload) - allowed_fields)
        if unknown_fields:
            raise ValueError("Unknown candidate fields: %s" % ", ".join(unknown_fields))

        raw_name = str(payload.get("name") or "").strip()
        if not raw_name:
            raise ValueError("Candidate name is required.")

        enabled_signals = payload.get("enabled_signals")
        if isinstance(enabled_signals, str):
            enabled_list = [item.strip() for item in enabled_signals.split(",") if item.strip()]
        elif isinstance(enabled_signals, (list, tuple)):
            enabled_list = [str(item).strip() for item in enabled_signals if str(item).strip()]
        else:
            raise ValueError("enabled_signals must be a non-empty list.")
        if not enabled_list:
            raise ValueError("enabled_signals must not be empty.")
        deduped_signals = []
        for signal_name in enabled_list:
            if signal_name not in ALLOWED_SIGNALS:
                raise ValueError("Unsupported signal: %s" % signal_name)
            if signal_name not in deduped_signals:
                deduped_signals.append(signal_name)

        def clamp_int(name: str, minimum: int, maximum: int, default: int) -> int:
            value = payload.get(name, default)
            try:
                coerced = int(value)
            except (TypeError, ValueError):
                coerced = default
            return max(minimum, min(maximum, coerced))

        def clamp_float(name: str, minimum: float, maximum: float, default: float) -> float:
            value = payload.get(name, default)
            try:
                coerced = float(value)
            except (TypeError, ValueError):
                coerced = default
            return max(minimum, min(maximum, coerced))

        momentum_window = clamp_int("momentum_window", 10, 252, 63)
        short_momentum_window = clamp_int("short_momentum_window", 2, 120, 21)
        if short_momentum_window >= momentum_window:
            short_momentum_window = max(2, momentum_window // 2)

        ema_fast = clamp_int("ema_fast", 2, 100, 12)
        ema_slow = clamp_int("ema_slow", 3, 200, 26)
        if ema_fast >= ema_slow:
            ema_fast = max(2, ema_slow - 1)

        macd_fast = clamp_int("macd_fast", 2, 60, 12)
        macd_slow = clamp_int("macd_slow", 3, 120, 26)
        if macd_fast >= macd_slow:
            macd_fast = max(2, macd_slow - 1)
        macd_signal = clamp_int("macd_signal", 2, 40, 9)

        benchmark_symbol = payload.get("benchmark_symbol")
        if benchmark_symbol is None:
            benchmark_symbol = default_benchmark_symbol
        elif not isinstance(benchmark_symbol, str):
            benchmark_symbol = default_benchmark_symbol
        else:
            benchmark_symbol = benchmark_symbol.upper()

        return cls(
            name=raw_name,
            benchmark_symbol=benchmark_symbol,
            enabled_signals=tuple(deduped_signals),
            momentum_window=momentum_window,
            short_momentum_window=short_momentum_window,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            rsi_period=clamp_int("rsi_period", 2, 60, 14),
            rsi_entry_midline=clamp_float("rsi_entry_midline", 30.0, 80.0, 55.0),
            rsi_take_profit=clamp_float("rsi_take_profit", 55.0, 101.0, 101.0),
            macd_fast=macd_fast,
            macd_slow=macd_slow,
            macd_signal=macd_signal,
            bb_window=clamp_int("bb_window", 5, 120, 20),
            bb_percentile=clamp_float("bb_percentile", 0.05, 0.95, 0.35),
            volume_window=clamp_int("volume_window", 5, 120, 20),
            volume_ratio_threshold=clamp_float("volume_ratio_threshold", 0.5, 3.0, 1.05),
            market_ma_window=clamp_int("market_ma_window", 5, 200, 50),
            atr_window=clamp_int("atr_window", 2, 60, 14),
            atr_stop_mult=clamp_float("atr_stop_mult", 0.0, 8.0, 0.0),
            cooldown_days=clamp_int("cooldown_days", 0, 60, 0),
            max_hold_days=clamp_int("max_hold_days", 0, 252, 0),
            position_size_pct=clamp_float("position_size_pct", 0.05, 1.0, 0.95),
            vote_threshold=clamp_float("vote_threshold", 0.2, 1.0, 0.5),
        )


@dataclass
class ResearchRoundResult:
    round_number: int
    round_name: str
    objective: str
    candidate_count: int
    winner_name: str
    winner_spec: Dict[str, Any]
    winner_metrics: Dict[str, Any]
    improved_best: bool
    leaderboard_path: str


@dataclass
class CodexProposalResponse:
    parsed_payload: Dict[str, Any]
    raw_text: str
    response_json: Dict[str, Any]


@dataclass
class ResearchDataBundle:
    price_frame: pd.DataFrame
    benchmark_frame: Optional[pd.DataFrame]
    provider_path: Optional[Path] = None
    provider_mode: str = "injected"
    symbol_source: str = "injected"
    benchmark_source: str = "injected"


@dataclass
class CandidateEvaluation:
    spec: CandidateStrategySpec
    round_number: int
    round_name: str
    origin: str
    orders_frame: pd.DataFrame
    signal_frame: pd.DataFrame
    daily_state: pd.DataFrame
    completed_trades: pd.DataFrame
    report_frame: pd.DataFrame
    metrics: Dict[str, Any]
    local_summary: Dict[str, Any]


def ensure_output_dir(output_root: str) -> Path:
    root = Path(output_root).expanduser()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = root / timestamp
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def region_to_qlib(region: str) -> str:
    region_lower = region.lower()
    if region_lower == "us":
        return REG_US
    if region_lower == "cn":
        return REG_CN
    return region_lower


def init_qlib(provider_path: Path, region: str) -> None:
    qlib.init(
        provider_uri=str(provider_path),
        region=region_to_qlib(region),
        expression_cache=None,
        dataset_cache=None,
        clear_mem_cache=True,
    )


def ensure_provider_data(provider_uri: str, region: str, auto_download_us_data: bool) -> Path:
    provider_path = Path(provider_uri).expanduser()
    if exists_qlib_data(provider_path):
        return provider_path
    if provider_path.exists() and any(provider_path.iterdir()):
        raise RuntimeError("%s exists but is not a valid Qlib dataset." % provider_path)
    if region.lower() != "us":
        raise FileNotFoundError("Provider data was not found at %s for region=%s." % (provider_path, region))
    if not auto_download_us_data:
        raise FileNotFoundError("US Qlib data was not found at %s." % provider_path)
    GetData().qlib_data(target_dir=str(provider_path), region=REG_US, exists_skip=True)
    if not exists_qlib_data(provider_path):
        raise RuntimeError("US Qlib data download did not produce a valid dataset at %s." % provider_path)
    return provider_path


def provider_has_data(symbol: str, start_time: str, end_time: Optional[str]) -> bool:
    frame = D.features([symbol], ["$close"], start_time=start_time, end_time=end_time, freq="day")
    return not frame.empty


def _normalize_provider_frame(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if frame.empty:
        raise ValueError("No daily data was found for %s." % symbol)
    normalized = frame.reset_index().copy()
    normalized["datetime"] = pd.to_datetime(normalized["datetime"])
    normalized = normalized.sort_values("datetime")
    normalized = normalized.rename(
        columns={
            "$open": "open_adj",
            "$high": "high_adj",
            "$low": "low_adj",
            "$close": "close_adj",
            "$factor": "factor",
            "$volume": "volume",
        }
    )
    normalized = normalized.drop(columns=["instrument"])
    normalized["factor"] = normalized["factor"].replace(0, np.nan)
    normalized = normalized.dropna(subset=["open_adj", "high_adj", "low_adj", "close_adj", "factor"])
    normalized["open_raw"] = normalized["open_adj"] / normalized["factor"]
    normalized["high_raw"] = normalized["high_adj"] / normalized["factor"]
    normalized["low_raw"] = normalized["low_adj"] / normalized["factor"]
    normalized["close_raw"] = normalized["close_adj"] / normalized["factor"]
    normalized["volume"] = normalized["volume"].fillna(0.0)
    normalized = normalized.set_index("datetime")
    if normalized.empty:
        raise ValueError("Daily data for %s became empty after normalization." % symbol)
    return normalized


def _download_yahoo_daily(symbol: str, start_time: str, end_time: Optional[str], cache_dir: Path) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / ("%s_daily.csv" % symbol.lower())

    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for the Yahoo fallback provider.") from exc

    yahoo_end = None if end_time is None else (pd.Timestamp(end_time) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    frame = yf.download(
        symbol,
        start=start_time,
        end=yahoo_end,
        interval="1d",
        auto_adjust=False,
        progress=False,
    )
    if frame.empty:
        raise ValueError("No Yahoo daily data was returned for %s." % symbol)
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame = frame.rename(columns={column: str(column).lower() for column in frame.columns})
    required = ["open", "high", "low", "close", "volume"]
    for column in required:
        if column not in frame.columns:
            raise ValueError("Yahoo daily data for %s is missing column %s." % (symbol, column))
    if "adj close" in frame.columns:
        adj_factor = (frame["adj close"] / frame["close"]).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    else:
        adj_factor = pd.Series(1.0, index=frame.index)
    adjusted = frame[required].dropna().copy()
    for column in ["open", "high", "low", "close"]:
        adjusted[column] = adjusted[column] * adj_factor
    adjusted.index = pd.to_datetime(adjusted.index).tz_localize(None)
    adjusted.index.name = "datetime"
    adjusted.to_csv(cache_path, index_label="datetime")

    normalized = adjusted.rename(
        columns={
            "open": "open_adj",
            "high": "high_adj",
            "low": "low_adj",
            "close": "close_adj",
        }
    )
    normalized["factor"] = 1.0
    normalized["open_raw"] = normalized["open_adj"]
    normalized["high_raw"] = normalized["high_adj"]
    normalized["low_raw"] = normalized["low_adj"]
    normalized["close_raw"] = normalized["close_adj"]
    return normalized


def _build_yahoo_source_frame(symbol: str, start_time: str, end_time: Optional[str], cache_dir: Path) -> pd.DataFrame:
    quote = _download_yahoo_daily(symbol, start_time=start_time, end_time=end_time, cache_dir=cache_dir)
    return pd.DataFrame(
        {
            "date": quote.index.strftime("%Y-%m-%d"),
            "symbol": symbol.upper(),
            "open": quote["open_adj"].astype(float),
            "high": quote["high_adj"].astype(float),
            "low": quote["low_adj"].astype(float),
            "close": quote["close_adj"].astype(float),
            "volume": quote["volume"].astype(float),
            "factor": 1.0,
            "change": quote["close_adj"].pct_change(fill_method=None).fillna(0.0),
        }
    )


def build_local_yahoo_daily_provider(
    symbols: Sequence[str],
    start_time: str,
    end_time: Optional[str],
    cache_root: Path,
) -> Tuple[Path, Dict[str, str]]:
    requested_end = end_time or pd.Timestamp.now().strftime("%Y-%m-%d")
    provider_root = cache_root / ("yahoo_provider_%s_%s" % (start_time.replace("-", ""), requested_end.replace("-", "")))
    raw_dir = provider_root / "raw_csv"
    source_dir = provider_root / "source_csv"
    provider_dir = provider_root / "qlib_day_data"

    if provider_root.exists():
        shutil.rmtree(provider_root)
    raw_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)

    symbol_sources = {}
    for symbol in symbols:
        source = _build_yahoo_source_frame(symbol, start_time=start_time, end_time=end_time, cache_dir=raw_dir)
        source.to_csv(source_dir / ("%s.csv" % symbol.lower()), index=False)
        symbol_sources[symbol] = "yahoo"

    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "dump_bin.py"),
            "dump_all",
            "--data_path",
            str(source_dir),
            "--qlib_dir",
            str(provider_dir),
            "--freq",
            "day",
            "--date_field_name",
            "date",
            "--symbol_field_name",
            "symbol",
            "--exclude_fields",
            "date,symbol",
            "--file_suffix",
            ".csv",
            "--max_workers",
            "1",
        ],
        check=True,
        cwd=str(REPO_ROOT),
    )
    return provider_dir, symbol_sources


def ensure_research_provider(config: ResearchConfig, cache_root: Path) -> Tuple[Path, Dict[str, Any]]:
    benchmark_symbol = config.resolved_benchmark_symbol()
    symbols = [config.symbol.upper()]
    if benchmark_symbol:
        symbols.append(benchmark_symbol)

    official_provider = ensure_provider_data(config.provider_uri, config.region, config.auto_download_us_data)
    init_qlib(official_provider, config.region)
    if all(provider_has_data(symbol, config.start_date, config.end_date) for symbol in symbols):
        return official_provider, {
            "provider_mode": "official_qlib",
            "symbol_sources": dict((symbol, "qlib") for symbol in symbols),
        }
    if config.region.lower() != "us":
        missing = [symbol for symbol in symbols if not provider_has_data(symbol, config.start_date, config.end_date)]
        raise RuntimeError("Provider %s does not cover symbols: %s" % (official_provider, ", ".join(missing)))

    local_provider, symbol_sources = build_local_yahoo_daily_provider(
        symbols=symbols,
        start_time=config.start_date,
        end_time=config.end_date,
        cache_root=cache_root,
    )
    init_qlib(local_provider, config.region)
    return local_provider, {"provider_mode": "local_yahoo_dump", "symbol_sources": symbol_sources}


def load_daily_quote_frame(
    symbol: str,
    start_time: str,
    end_time: Optional[str],
    fallback_cache_dir: Optional[Path] = None,
) -> Tuple[pd.DataFrame, str]:
    fields = ["$open", "$high", "$low", "$close", "$factor", "$volume"]
    provider_frame = D.features([symbol], fields, start_time=start_time, end_time=end_time, freq="day")
    if not provider_frame.empty:
        return _normalize_provider_frame(provider_frame, symbol), "qlib"
    if fallback_cache_dir is None:
        raise ValueError("No daily provider data was found for %s." % symbol)
    return _download_yahoo_daily(symbol, start_time=start_time, end_time=end_time, cache_dir=fallback_cache_dir), "yahoo"


def normalize_external_quote_frame(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.copy()
    normalized.index = pd.to_datetime(normalized.index)
    normalized = normalized.sort_index()
    required_prices = ["open_adj", "high_adj", "low_adj", "close_adj"]
    for column in required_prices:
        if column not in normalized.columns:
            if column == "high_adj" and "close_adj" in normalized.columns:
                normalized[column] = normalized["close_adj"]
            elif column == "low_adj" and "close_adj" in normalized.columns:
                normalized[column] = normalized["close_adj"]
            else:
                raise ValueError("Missing required column %s in injected data." % column)
    if "factor" not in normalized.columns:
        normalized["factor"] = 1.0
    normalized["factor"] = normalized["factor"].replace(0, np.nan).fillna(1.0)
    if "volume" not in normalized.columns:
        normalized["volume"] = 1.0
    if "open_raw" not in normalized.columns:
        normalized["open_raw"] = normalized["open_adj"] / normalized["factor"]
    if "high_raw" not in normalized.columns:
        normalized["high_raw"] = normalized["high_adj"] / normalized["factor"]
    if "low_raw" not in normalized.columns:
        normalized["low_raw"] = normalized["low_adj"] / normalized["factor"]
    if "close_raw" not in normalized.columns:
        normalized["close_raw"] = normalized["close_adj"] / normalized["factor"]
    return normalized


def prepare_data_bundle(
    config: ResearchConfig,
    run_dir: Path,
    data_bundle: Optional[ResearchDataBundle] = None,
) -> Tuple[ResearchDataBundle, Dict[str, Any]]:
    if data_bundle is not None:
        price_frame = normalize_external_quote_frame(data_bundle.price_frame)
        benchmark_frame = normalize_external_quote_frame(data_bundle.benchmark_frame) if data_bundle.benchmark_frame is not None else None
        bundle = ResearchDataBundle(
            price_frame=price_frame,
            benchmark_frame=benchmark_frame,
            provider_path=data_bundle.provider_path,
            provider_mode=data_bundle.provider_mode,
            symbol_source=data_bundle.symbol_source,
            benchmark_source=data_bundle.benchmark_source,
        )
    else:
        cache_root = SCRIPT_DIR / "cache"
        provider_path, provider_info = ensure_research_provider(config, cache_root=cache_root)
        fallback_cache_dir = cache_root / "raw_yahoo"
        symbol_frame, symbol_source = load_daily_quote_frame(config.symbol.upper(), config.start_date, config.end_date, fallback_cache_dir)
        benchmark_symbol = config.resolved_benchmark_symbol()
        benchmark_frame = None
        benchmark_source = "none"
        if benchmark_symbol:
            benchmark_frame, benchmark_source = load_daily_quote_frame(benchmark_symbol, config.start_date, config.end_date, fallback_cache_dir)
        bundle = ResearchDataBundle(
            price_frame=symbol_frame,
            benchmark_frame=benchmark_frame,
            provider_path=provider_path,
            provider_mode=provider_info["provider_mode"],
            symbol_source=provider_info["symbol_sources"].get(config.symbol.upper(), symbol_source),
            benchmark_source=provider_info["symbol_sources"].get(benchmark_symbol, benchmark_source) if benchmark_symbol else "none",
        )

    effective_benchmark = config.resolved_benchmark_symbol()
    if bundle.benchmark_frame is not None:
        aligned_dates = bundle.price_frame.index.intersection(bundle.benchmark_frame.index)
        bundle.price_frame = bundle.price_frame.loc[aligned_dates].copy()
        bundle.benchmark_frame = bundle.benchmark_frame.loc[aligned_dates].copy()
    if bundle.price_frame.empty:
        raise RuntimeError("The research price frame is empty after alignment.")

    market_summary = {
        "symbol": config.symbol.upper(),
        "benchmark_symbol": effective_benchmark,
        "provider_mode": bundle.provider_mode,
        "symbol_source": bundle.symbol_source,
        "benchmark_source": bundle.benchmark_source,
        "start_date": bundle.price_frame.index.min().strftime("%Y-%m-%d"),
        "end_date": bundle.price_frame.index.max().strftime("%Y-%m-%d"),
        "num_bars": int(len(bundle.price_frame)),
        "symbol_return_pct": float(bundle.price_frame["close_adj"].iloc[-1] / bundle.price_frame["close_adj"].iloc[0] - 1.0) * 100.0,
        "symbol_realized_vol_pct": float(bundle.price_frame["close_adj"].pct_change(fill_method=None).std(ddof=0) * np.sqrt(252) * 100.0),
    }
    if bundle.benchmark_frame is not None:
        market_summary["benchmark_return_pct"] = float(
            bundle.benchmark_frame["close_adj"].iloc[-1] / bundle.benchmark_frame["close_adj"].iloc[0] - 1.0
        ) * 100.0
    if bundle.provider_path is not None:
        market_summary["provider_path"] = str(bundle.provider_path)

    (run_dir / "market_summary.json").write_text(json.dumps(market_summary, indent=2), encoding="utf-8")
    return bundle, market_summary


def dataframe_to_markdown(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "_empty_"
    headers = [str(column) for column in frame.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for _, row in frame.iterrows():
        values = []
        for value in row.tolist():
            values.append("N/A" if pd.isna(value) else str(value))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def calc_trade_cost(trade_value: float, cost_rate: float, min_cost: float) -> float:
    return max(trade_value * cost_rate, min_cost) if trade_value > 0 else 0.0


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    return result.clip(lower=0.0, upper=100.0)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period, min_periods=period).mean()


def macd_histogram(series: pd.Series, fast: int, slow: int, signal: int) -> pd.Series:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd_line - signal_line


def build_feature_frame(
    price_frame: pd.DataFrame,
    benchmark_frame: Optional[pd.DataFrame],
    spec: CandidateStrategySpec,
) -> pd.DataFrame:
    def nan_bool(condition: pd.Series, ready_mask: pd.Series) -> pd.Series:
        values = np.where(ready_mask, condition.astype(bool), np.nan)
        return pd.Series(values, index=condition.index)

    daily = price_frame.copy()
    if benchmark_frame is not None:
        daily = daily.join(
            benchmark_frame[["close_adj"]].rename(columns={"close_adj": "benchmark_close_adj"}),
            how="inner",
        )
    else:
        daily["benchmark_close_adj"] = np.nan
    if daily.empty:
        raise ValueError("No aligned daily rows remain after symbol/benchmark alignment.")
    daily = daily.sort_index()
    next_trade_dates = pd.Series(daily.index, index=daily.index).shift(-1)
    daily["next_trade_date"] = pd.to_datetime(next_trade_dates)

    daily["momentum_value"] = daily["close_adj"].pct_change(spec.momentum_window, fill_method=None)
    daily["short_momentum_value"] = daily["close_adj"].pct_change(spec.short_momentum_window, fill_method=None)
    daily["ema_fast_value"] = ema(daily["close_adj"], spec.ema_fast)
    daily["ema_slow_value"] = ema(daily["close_adj"], spec.ema_slow)
    daily["rsi_value"] = rsi(daily["close_adj"], spec.rsi_period)
    daily["macd_histogram_value"] = macd_histogram(daily["close_adj"], spec.macd_fast, spec.macd_slow, spec.macd_signal)

    bb_mid = daily["close_adj"].rolling(spec.bb_window, min_periods=spec.bb_window).mean()
    bb_std = daily["close_adj"].rolling(spec.bb_window, min_periods=spec.bb_window).std(ddof=0)
    daily["bb_bandwidth"] = (4.0 * bb_std) / bb_mid.replace(0, np.nan)
    daily["bb_bandwidth_threshold"] = daily["bb_bandwidth"].rolling(max(spec.bb_window * 3, 60), min_periods=spec.bb_window).quantile(spec.bb_percentile)
    daily["volume_ratio"] = daily["volume"] / daily["volume"].rolling(spec.volume_window, min_periods=spec.volume_window).mean()
    daily["benchmark_ma"] = daily["benchmark_close_adj"].rolling(spec.market_ma_window, min_periods=spec.market_ma_window).mean()
    daily["atr_value"] = atr(daily["high_adj"], daily["low_adj"], daily["close_adj"], spec.atr_window)

    daily["momentum_signal"] = nan_bool(daily["momentum_value"] > 0, daily["momentum_value"].notna())
    daily["short_momentum_signal"] = nan_bool(daily["short_momentum_value"] > 0, daily["short_momentum_value"].notna())
    daily["ema_trend_signal"] = nan_bool(
        daily["ema_fast_value"] > daily["ema_slow_value"],
        daily["ema_fast_value"].notna() & daily["ema_slow_value"].notna(),
    )
    daily["rsi_signal"] = nan_bool(daily["rsi_value"] >= spec.rsi_entry_midline, daily["rsi_value"].notna())
    daily["macd_histogram_signal"] = nan_bool(daily["macd_histogram_value"] > 0, daily["macd_histogram_value"].notna())
    daily["bollinger_compression_signal"] = nan_bool(
        daily["bb_bandwidth"] <= daily["bb_bandwidth_threshold"],
        daily["bb_bandwidth"].notna() & daily["bb_bandwidth_threshold"].notna(),
    )
    daily["volume_confirmation_signal"] = nan_bool(daily["volume_ratio"] >= spec.volume_ratio_threshold, daily["volume_ratio"].notna())
    daily["benchmark_regime_signal"] = nan_bool(
        daily["benchmark_close_adj"] > daily["benchmark_ma"],
        daily["benchmark_close_adj"].notna() & daily["benchmark_ma"].notna(),
    )
    return daily


def _signal_columns_for_spec(spec: CandidateStrategySpec) -> List[str]:
    return ["%s_signal" % signal_name for signal_name in spec.enabled_signals]


def _compute_vote_ratio(row: pd.Series, spec: CandidateStrategySpec) -> Tuple[bool, float, Dict[str, Optional[bool]]]:
    signal_states = {}
    required_columns = _signal_columns_for_spec(spec)
    for signal_name in spec.enabled_signals:
        column = "%s_signal" % signal_name
        value = row.get(column)
        if pd.isna(value):
            signal_states[signal_name] = None
        else:
            signal_states[signal_name] = bool(value)
    ready = pd.notna(row.get("next_trade_date")) and all(pd.notna(row.get(column)) for column in required_columns)
    if not ready:
        return False, 0.0, signal_states
    vote_count = sum(1 for value in signal_states.values() if value)
    vote_ratio = float(vote_count) / float(len(required_columns))
    return True, vote_ratio, signal_states


def _max_affordable_raw_shares(
    *,
    cash: float,
    raw_open: float,
    factor: float,
    open_adj: float,
    target_weight: float,
    account_value: float,
) -> int:
    if cash <= 0 or raw_open <= 0 or factor <= 0 or target_weight <= 0 or account_value <= 0:
        return 0
    target_value = account_value * target_weight
    raw_shares = int(target_value // raw_open)
    while raw_shares > 0:
        amount = raw_shares / factor
        trade_value = amount * open_adj
        trade_cost = calc_trade_cost(trade_value, OPEN_COST, MIN_COST)
        if trade_value + trade_cost <= cash + 1e-9:
            return raw_shares
        raw_shares -= 1
    return 0


def simulate_candidate_orders(
    spec: CandidateStrategySpec,
    price_frame: pd.DataFrame,
    benchmark_frame: Optional[pd.DataFrame],
    symbol: str,
    initial_cash: float,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, Any], str, str]:
    feature_frame = build_feature_frame(price_frame, benchmark_frame, spec)
    if feature_frame.empty:
        raise ValueError("Feature frame is empty for %s." % spec.name)

    cash = float(initial_cash)
    position_amount = 0.0
    pending_action = None
    pending_signal_date = None
    pending_reasons = []
    cooldown_remaining = 0
    entry_date = None
    entry_signal_date = None
    highest_close_since_entry = None
    bars_in_position = 0
    entry_cash_outlay = 0.0
    trade_cash_flow = 0.0

    signal_rows = []
    daily_rows = []
    completed_trades = []
    orders = []

    ready_trade_dates = feature_frame.loc[feature_frame["next_trade_date"].notna(), "next_trade_date"]
    safe_trade_dates = ready_trade_dates.iloc[:-1] if len(ready_trade_dates) > 1 else ready_trade_dates.iloc[0:0]
    backtest_start = pd.to_datetime(safe_trade_dates.iloc[0]) if not safe_trade_dates.empty else feature_frame.index[0]
    if not safe_trade_dates.empty:
        backtest_end = pd.to_datetime(safe_trade_dates.iloc[-1])
    elif len(feature_frame.index) > 1:
        backtest_end = feature_frame.index[-2]
    else:
        backtest_end = feature_frame.index[-1]

    dates = list(feature_frame.index)
    for idx, date in enumerate(dates):
        row = feature_frame.loc[date]
        executed_action = ""
        executed_value = 0.0
        executed_cost = 0.0
        cooldown_armed_today = False

        if pending_action == "buy" and position_amount <= 1e-12:
            account_open = cash + position_amount * float(row["open_adj"])
            raw_shares = _max_affordable_raw_shares(
                cash=cash,
                raw_open=float(row["open_raw"]),
                factor=float(row["factor"]),
                open_adj=float(row["open_adj"]),
                target_weight=float(spec.position_size_pct),
                account_value=account_open,
            )
            if raw_shares > 0:
                amount = raw_shares / float(row["factor"])
                trade_value = amount * float(row["open_adj"])
                trade_cost = calc_trade_cost(trade_value, OPEN_COST, MIN_COST)
                cash -= trade_value + trade_cost
                position_amount += amount
                entry_date = date
                entry_signal_date = pending_signal_date
                highest_close_since_entry = float(row["close_adj"])
                bars_in_position = 0
                entry_cash_outlay = trade_value + trade_cost
                trade_cash_flow = -(trade_value + trade_cost)
                executed_action = "buy"
                executed_value = trade_value
                executed_cost = trade_cost
                orders.append([date.strftime("%Y-%m-%d"), symbol, amount, "buy"])

        elif pending_action == "sell" and position_amount > 1e-12:
            trade_value = position_amount * float(row["open_adj"])
            trade_cost = calc_trade_cost(trade_value, CLOSE_COST, MIN_COST)
            cash += trade_value - trade_cost
            trade_cash_flow += trade_value - trade_cost
            orders.append([date.strftime("%Y-%m-%d"), symbol, position_amount, "sell"])
            completed_trades.append(
                {
                    "entry_date": entry_date.strftime("%Y-%m-%d") if entry_date is not None else None,
                    "exit_date": date.strftime("%Y-%m-%d"),
                    "entry_signal_date": entry_signal_date.strftime("%Y-%m-%d") if entry_signal_date is not None else None,
                    "exit_signal_date": pending_signal_date.strftime("%Y-%m-%d") if pending_signal_date is not None else None,
                    "pnl": trade_cash_flow,
                    "bars_held": bars_in_position,
                    "return_pct": (trade_cash_flow / entry_cash_outlay * 100.0) if entry_cash_outlay > 0 else np.nan,
                }
            )
            executed_action = "sell"
            executed_value = trade_value
            executed_cost = trade_cost
            position_amount = 0.0
            entry_date = None
            entry_signal_date = None
            highest_close_since_entry = None
            bars_in_position = 0
            entry_cash_outlay = 0.0
            trade_cash_flow = 0.0
            cooldown_remaining = int(spec.cooldown_days)
            cooldown_armed_today = True

        pending_action = None
        pending_signal_date = None
        pending_reasons = []

        close_adj = float(row["close_adj"])
        if position_amount > 1e-12:
            highest_close_since_entry = close_adj if highest_close_since_entry is None else max(highest_close_since_entry, close_adj)
            bars_in_position += 1

        account_close = cash + position_amount * close_adj
        daily_rows.append(
            {
                "date": date,
                "account": account_close,
                "cash": cash,
                "position_amount": position_amount,
                "close_adj": close_adj,
                "executed_action": executed_action,
                "executed_value": executed_value,
                "executed_cost": executed_cost,
            }
        )

        ready, vote_ratio, signal_states = _compute_vote_ratio(row, spec)
        exit_reasons = []
        if position_amount > 1e-12:
            if "ema_trend" in spec.enabled_signals and pd.notna(row.get("ema_slow_value")) and close_adj < float(row["ema_slow_value"]):
                exit_reasons.append("ma_breakdown")
            if spec.rsi_take_profit <= 100.0 and pd.notna(row.get("rsi_value")) and float(row["rsi_value"]) >= spec.rsi_take_profit:
                exit_reasons.append("rsi_take_profit")
            if spec.atr_stop_mult > 0 and pd.notna(row.get("atr_value")) and highest_close_since_entry is not None:
                trailing_level = highest_close_since_entry - spec.atr_stop_mult * float(row["atr_value"])
                if close_adj <= trailing_level:
                    exit_reasons.append("atr_trailing_stop")
            if spec.max_hold_days > 0 and bars_in_position >= spec.max_hold_days:
                exit_reasons.append("max_hold")

        planned_action = ""
        if idx < len(dates) - 2:
            if position_amount > 1e-12:
                if exit_reasons:
                    planned_action = "sell"
                    pending_action = "sell"
                    pending_signal_date = date
                    pending_reasons = exit_reasons
            else:
                if not cooldown_armed_today and cooldown_remaining > 0:
                    cooldown_remaining -= 1
                if ready and cooldown_remaining <= 0 and vote_ratio >= spec.vote_threshold:
                    planned_action = "buy"
                    pending_action = "buy"
                    pending_signal_date = date

        signal_row = {
            "date": date.strftime("%Y-%m-%d"),
            "trade_date": row["next_trade_date"].strftime("%Y-%m-%d") if pd.notna(row["next_trade_date"]) else None,
            "signal_ready": ready,
            "vote_ratio": vote_ratio,
            "planned_action": planned_action,
            "executed_action": executed_action,
            "in_position": position_amount > 1e-12,
            "cooldown_remaining": cooldown_remaining,
            "exit_reasons": ",".join(exit_reasons),
            "pending_reasons": ",".join(pending_reasons),
            "close_adj": close_adj,
            "momentum_value": row.get("momentum_value"),
            "short_momentum_value": row.get("short_momentum_value"),
            "rsi_value": row.get("rsi_value"),
            "macd_histogram_value": row.get("macd_histogram_value"),
            "bb_bandwidth": row.get("bb_bandwidth"),
            "volume_ratio": row.get("volume_ratio"),
            "benchmark_close_adj": row.get("benchmark_close_adj"),
        }
        for signal_name, state in signal_states.items():
            signal_row["%s_signal" % signal_name] = state
        signal_rows.append(signal_row)

    orders_frame = pd.DataFrame(orders, columns=["datetime", "instrument", "amount", "direction"])
    signal_frame = pd.DataFrame(signal_rows)
    daily_state = pd.DataFrame(daily_rows)
    if not daily_state.empty:
        daily_state["date"] = pd.to_datetime(daily_state["date"])
        daily_state["net_return"] = daily_state["account"].pct_change(fill_method=None).fillna(0.0)
        daily_state["peak"] = daily_state["account"].cummax()
        daily_state["drawdown"] = daily_state["account"] / daily_state["peak"] - 1.0
    completed_trade_frame = pd.DataFrame(completed_trades)
    local_summary = {
        "final_value": float(daily_state["account"].iloc[-1]) if not daily_state.empty else initial_cash,
        "max_drawdown_pct": float(daily_state["drawdown"].min() * 100.0) if not daily_state.empty else 0.0,
        "buy_orders": int((orders_frame["direction"] == "buy").sum()) if not orders_frame.empty else 0,
        "sell_orders": int((orders_frame["direction"] == "sell").sum()) if not orders_frame.empty else 0,
        "completed_trades": int(len(completed_trade_frame)),
        "win_rate": float((completed_trade_frame["pnl"] > 0).mean()) if not completed_trade_frame.empty else np.nan,
        "profit_factor": float(
            completed_trade_frame.loc[completed_trade_frame["pnl"] > 0, "pnl"].sum()
            / abs(completed_trade_frame.loc[completed_trade_frame["pnl"] < 0, "pnl"].sum())
        )
        if not completed_trade_frame.empty and completed_trade_frame.loc[completed_trade_frame["pnl"] < 0, "pnl"].sum() < 0
        else np.nan,
    }
    return orders_frame, signal_frame, daily_state, completed_trade_frame, local_summary, backtest_start.strftime("%Y-%m-%d"), backtest_end.strftime("%Y-%m-%d")


def build_zero_benchmark(backtest_dates: pd.Index) -> pd.Series:
    benchmark_index = pd.DatetimeIndex(pd.to_datetime(backtest_dates).unique()).sort_values()
    return pd.Series(0.0, index=benchmark_index, name="zero_benchmark")


def run_qlib_orders_backtest(
    symbol: str,
    orders_frame: pd.DataFrame,
    start_time: str,
    end_time: str,
    initial_cash: float,
    benchmark_series: pd.Series,
) -> pd.DataFrame:
    from qlib.backtest import backtest as qlib_backtest

    strategy_config = {
        "class": "FileOrderStrategy",
        "module_path": "qlib.contrib.strategy.rule_strategy",
        "kwargs": {"file": orders_frame},
    }
    executor_config = {
        "class": "SimulatorExecutor",
        "module_path": "qlib.backtest.executor",
        "kwargs": {
            "time_per_step": "day",
            "generate_portfolio_metrics": True,
            "indicator_config": {"show_indicator": False},
        },
    }
    portfolio_metric_dict, _indicator_dict = qlib_backtest(
        start_time=start_time,
        end_time=end_time,
        strategy=strategy_config,
        executor=executor_config,
        benchmark=benchmark_series,
        account=initial_cash,
        exchange_kwargs={
            "freq": "day",
            "codes": [symbol],
            "deal_price": "open",
            "trade_unit": 1,
            "limit_threshold": None,
            "open_cost": OPEN_COST,
            "close_cost": CLOSE_COST,
            "min_cost": MIN_COST,
        },
    )
    report_frame, _positions = portfolio_metric_dict["1day"]
    report_frame.index = pd.to_datetime(report_frame.index)
    return report_frame


def compute_research_score(
    *,
    num_bars: int,
    completed_trades: int,
    sharpe: float,
    max_drawdown_pct: float,
    annual_turnover: float,
    initial_cash: float,
) -> Dict[str, float]:
    import math

    sample_years = max(float(num_bars) / 252.0, 0.25)
    completed_trades_per_year = float(completed_trades) / sample_years
    trade_factor = min(completed_trades_per_year / 6.0, 1.0)
    drawdown_penalty = max(0.0, abs(max_drawdown_pct) - 12.0) * 0.08
    turnover_ratio = annual_turnover / initial_cash if initial_cash > 0 else 0.0
    turnover_penalty = max(0.0, turnover_ratio - 25.0) * 0.01
    safe_sharpe = 0.0 if pd.isna(sharpe) else float(sharpe)
    research_score = safe_sharpe * math.sqrt(trade_factor) - drawdown_penalty - turnover_penalty
    return {
        "sample_years": sample_years,
        "completed_trades_per_year": completed_trades_per_year,
        "trade_factor": trade_factor,
        "drawdown_penalty": drawdown_penalty,
        "turnover_ratio": turnover_ratio,
        "turnover_penalty": turnover_penalty,
        "research_score": research_score,
    }


def summarize_report_frame(
    report_frame: pd.DataFrame,
    initial_cash: float,
    completed_trades: int,
    num_orders: int,
) -> Dict[str, Any]:
    if report_frame.empty:
        raise ValueError("Qlib report frame is empty.")
    account = report_frame["account"].astype(float)
    net_returns = (report_frame["return"] - report_frame["cost"]).fillna(0.0)
    cumulative_return = float(account.iloc[-1] / initial_cash - 1.0)
    sample_years = max(float(len(report_frame)) / 252.0, 0.25)
    annualized_return = float((1.0 + cumulative_return) ** (1.0 / sample_years) - 1.0)
    drawdown = account / account.cummax() - 1.0
    std = float(net_returns.std(ddof=1))
    sharpe = 0.0 if std == 0.0 or np.isnan(std) else float(net_returns.mean() / std * np.sqrt(252.0))
    max_drawdown_pct = float(drawdown.min() * 100.0)
    total_turnover = float(report_frame["total_turnover"].iloc[-1]) if "total_turnover" in report_frame.columns else 0.0
    annual_turnover = total_turnover / sample_years
    score_parts = compute_research_score(
        num_bars=int(len(report_frame)),
        completed_trades=completed_trades,
        sharpe=sharpe,
        max_drawdown_pct=max_drawdown_pct,
        annual_turnover=annual_turnover,
        initial_cash=initial_cash,
    )
    rejection_reasons = []
    if num_orders <= 0:
        rejection_reasons.append("no_orders")
    if score_parts["completed_trades_per_year"] < 1.0:
        rejection_reasons.append("trade_frequency_too_low")
    if max_drawdown_pct < -35.0:
        rejection_reasons.append("drawdown_too_large")
    if float(account.iloc[-1]) < 0.8 * initial_cash:
        rejection_reasons.append("final_value_too_low")
    return {
        "num_bars": int(len(report_frame)),
        "cumulative_return": cumulative_return,
        "annualized_return": annualized_return,
        "sharpe": sharpe,
        "max_drawdown_pct": max_drawdown_pct,
        "final_value": float(account.iloc[-1]),
        "total_turnover": total_turnover,
        "annual_turnover": annual_turnover,
        "accepted": len(rejection_reasons) == 0,
        "rejection_reasons": rejection_reasons,
        **score_parts,
    }


def sort_leaderboard_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame.sort_values(
        by=[
            "accepted",
            "research_score",
            "sharpe",
            "max_drawdown_pct",
            "completed_trades",
            "cumulative_return",
            "candidate_name",
        ],
        ascending=[False, False, False, False, False, False, True],
    ).reset_index(drop=True)


def evaluate_candidate_spec(
    spec: CandidateStrategySpec,
    config: ResearchConfig,
    bundle: ResearchDataBundle,
    round_number: int,
    round_name: str,
    origin: str,
) -> CandidateEvaluation:
    orders_frame, signal_frame, daily_state, completed_trades, local_summary, start_time, end_time = simulate_candidate_orders(
        spec=spec,
        price_frame=bundle.price_frame,
        benchmark_frame=bundle.benchmark_frame,
        symbol=config.symbol.upper(),
        initial_cash=config.initial_cash,
    )

    if orders_frame.empty:
        report_frame = pd.DataFrame(
            {
                "account": [config.initial_cash],
                "return": [0.0],
                "cost": [0.0],
                "total_turnover": [0.0],
            },
            index=[pd.Timestamp(start_time)],
        )
    else:
        benchmark_series = build_zero_benchmark(bundle.price_frame.loc[start_time:end_time].index)
        report_frame = run_qlib_orders_backtest(
            symbol=config.symbol.upper(),
            orders_frame=orders_frame,
            start_time=start_time,
            end_time=end_time,
            initial_cash=config.initial_cash,
            benchmark_series=benchmark_series,
        )

    report_metrics = summarize_report_frame(
        report_frame=report_frame,
        initial_cash=config.initial_cash,
        completed_trades=int(len(completed_trades)),
        num_orders=int(len(orders_frame)),
    )
    metrics = {
        "round_number": round_number,
        "round_name": round_name,
        "origin": origin,
        "candidate_name": spec.name,
        "fingerprint": spec.fingerprint(),
        "completed_trades": int(len(completed_trades)),
        "buy_orders": int((orders_frame["direction"] == "buy").sum()) if not orders_frame.empty else 0,
        "sell_orders": int((orders_frame["direction"] == "sell").sum()) if not orders_frame.empty else 0,
        "win_rate": local_summary["win_rate"],
        "profit_factor": local_summary["profit_factor"],
        "spec_json": json.dumps(spec.to_dict(), sort_keys=True),
        **report_metrics,
    }
    return CandidateEvaluation(
        spec=spec,
        round_number=round_number,
        round_name=round_name,
        origin=origin,
        orders_frame=orders_frame,
        signal_frame=signal_frame,
        daily_state=daily_state,
        completed_trades=completed_trades,
        report_frame=report_frame,
        metrics=metrics,
        local_summary=local_summary,
    )


def evaluate_candidate_batch(
    specs: Sequence[CandidateStrategySpec],
    config: ResearchConfig,
    bundle: ResearchDataBundle,
    round_number: int,
    round_name: str,
    origin: str,
) -> List[CandidateEvaluation]:
    evaluations = []
    for spec in specs:
        evaluations.append(
            evaluate_candidate_spec(
                spec=spec,
                config=config,
                bundle=bundle,
                round_number=round_number,
                round_name=round_name,
                origin=origin,
            )
        )
    return sorted(
        evaluations,
        key=lambda item: (
            bool(item.metrics["accepted"]),
            float(item.metrics["research_score"]),
            float(item.metrics["sharpe"]),
            float(item.metrics["max_drawdown_pct"]),
            int(item.metrics["completed_trades"]),
            float(item.metrics["cumulative_return"]),
            item.spec.name,
        ),
        reverse=True,
    )


def normalize_candidate_payloads(
    raw_payloads: Sequence[Dict[str, Any]],
    default_benchmark_symbol: Optional[str],
    seen_fingerprints: Optional[Iterable[str]] = None,
) -> Tuple[List[CandidateStrategySpec], List[str]]:
    seen = set(seen_fingerprints or [])
    specs = []
    rejections = []
    for raw_payload in raw_payloads:
        try:
            spec = CandidateStrategySpec.from_payload(raw_payload, default_benchmark_symbol=default_benchmark_symbol)
        except Exception as exc:
            rejections.append("invalid_candidate:%s" % exc)
            continue
        fingerprint = spec.fingerprint()
        if fingerprint in seen:
            rejections.append("duplicate_candidate:%s" % spec.name)
            continue
        seen.add(fingerprint)
        specs.append(spec)
    return specs, rejections


def build_baseline_specs(default_benchmark_symbol: Optional[str]) -> List[CandidateStrategySpec]:
    baseline_payloads = [
        {
            "name": "momentum_only",
            "benchmark_symbol": default_benchmark_symbol,
            "enabled_signals": ["momentum"],
            "momentum_window": 63,
            "position_size_pct": 0.95,
            "vote_threshold": 1.0,
            "rsi_take_profit": 101.0,
        },
        {
            "name": "momentum_plus_trend",
            "benchmark_symbol": default_benchmark_symbol,
            "enabled_signals": ["momentum", "ema_trend"],
            "momentum_window": 84,
            "ema_fast": 15,
            "ema_slow": 55,
            "vote_threshold": 0.5,
        },
        {
            "name": "momentum_plus_market_filter",
            "benchmark_symbol": default_benchmark_symbol,
            "enabled_signals": ["momentum", "benchmark_regime"],
            "momentum_window": 84,
            "market_ma_window": 50,
            "vote_threshold": 1.0,
        },
        {
            "name": "ensemble_core",
            "benchmark_symbol": default_benchmark_symbol,
            "enabled_signals": ["momentum", "ema_trend", "rsi", "macd_histogram"],
            "momentum_window": 84,
            "ema_fast": 12,
            "ema_slow": 40,
            "rsi_entry_midline": 55.0,
            "vote_threshold": 0.5,
        },
        {
            "name": "ensemble_plus_bb",
            "benchmark_symbol": default_benchmark_symbol,
            "enabled_signals": ["momentum", "ema_trend", "rsi", "macd_histogram", "bollinger_compression"],
            "momentum_window": 84,
            "ema_fast": 12,
            "ema_slow": 40,
            "rsi_entry_midline": 54.0,
            "bb_window": 20,
            "bb_percentile": 0.35,
            "vote_threshold": 0.6,
        },
        {
            "name": "ensemble_plus_volume",
            "benchmark_symbol": default_benchmark_symbol,
            "enabled_signals": ["momentum", "ema_trend", "rsi", "macd_histogram", "volume_confirmation"],
            "momentum_window": 84,
            "ema_fast": 12,
            "ema_slow": 40,
            "volume_ratio_threshold": 1.1,
            "vote_threshold": 0.6,
        },
        {
            "name": "ensemble_with_risk_overlay",
            "benchmark_symbol": default_benchmark_symbol,
            "enabled_signals": ["momentum", "ema_trend", "rsi", "macd_histogram"],
            "momentum_window": 63,
            "ema_fast": 12,
            "ema_slow": 40,
            "rsi_entry_midline": 55.0,
            "atr_stop_mult": 2.5,
            "cooldown_days": 5,
            "max_hold_days": 60,
            "vote_threshold": 0.5,
        },
    ]
    specs = []
    for payload in baseline_payloads:
        specs.append(CandidateStrategySpec.from_payload(payload, default_benchmark_symbol=default_benchmark_symbol))
    return specs


def leaderboard_frame_from_evaluations(evaluations: Sequence[CandidateEvaluation]) -> pd.DataFrame:
    rows = []
    for evaluation in evaluations:
        row = dict(evaluation.metrics)
        row["rejection_reasons"] = ",".join(row.get("rejection_reasons", []))
        rows.append(row)
    return sort_leaderboard_frame(pd.DataFrame(rows))


def persist_round_artifacts(round_dir: Path, evaluations: Sequence[CandidateEvaluation], winner_fingerprint: str) -> Path:
    round_dir.mkdir(parents=True, exist_ok=True)
    leaderboard = leaderboard_frame_from_evaluations(evaluations)
    leaderboard["is_round_winner"] = leaderboard["fingerprint"] == winner_fingerprint
    leaderboard_path = round_dir / "round_leaderboard.csv"
    leaderboard.to_csv(leaderboard_path, index=False)

    winner = next(item for item in evaluations if item.spec.fingerprint() == winner_fingerprint)
    winner.orders_frame.to_csv(round_dir / "orders.csv", index=False)
    winner.signal_frame.to_csv(round_dir / "signal_diagnostics.csv", index=False)
    winner.daily_state.to_csv(round_dir / "daily_state.csv", index=False)
    winner.completed_trades.to_csv(round_dir / "completed_trades.csv", index=False)
    winner.report_frame.to_csv(round_dir / "report_1day.csv")
    (round_dir / "winner_spec.json").write_text(
        json.dumps(winner.spec.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return leaderboard_path


def write_trace_files(
    trace_dir: Path,
    prefix: str,
    system_prompt: str,
    user_prompt: str,
    response: CodexProposalResponse,
) -> None:
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / ("%s_system.txt" % prefix)).write_text(system_prompt, encoding="utf-8")
    (trace_dir / ("%s_user.txt" % prefix)).write_text(user_prompt, encoding="utf-8")
    (trace_dir / ("%s_response.txt" % prefix)).write_text(response.raw_text, encoding="utf-8")
    (trace_dir / ("%s_response.json" % prefix)).write_text(
        json.dumps(response.response_json, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def build_round_summary_rows(leaderboard: pd.DataFrame, limit: int = 8) -> List[Dict[str, Any]]:
    if leaderboard.empty:
        return []
    subset = leaderboard.head(limit).copy()
    keep_columns = [
        "candidate_name",
        "research_score",
        "sharpe",
        "max_drawdown_pct",
        "completed_trades",
        "cumulative_return",
        "accepted",
    ]
    available = [column for column in keep_columns if column in subset.columns]
    return subset[available].to_dict("records")


def write_experiment_log(path: Path, payloads: Sequence[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, sort_keys=True, default=str))
            handle.write("\n")


def write_winner_strategy_script(run_dir: Path, winner_spec: CandidateStrategySpec) -> None:
    spec_json = json.dumps(winner_spec.to_dict(), indent=2, sort_keys=True)
    script = """from __future__ import annotations

import json
import sys
from pathlib import Path

EXAMPLE_DIR = Path(r'''%s''')
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from research_core import CandidateStrategySpec, simulate_candidate_orders


WINNER_SPEC = CandidateStrategySpec.from_payload(
    json.loads(%r),
    default_benchmark_symbol=%r,
)


def get_spec() -> CandidateStrategySpec:
    return WINNER_SPEC


def build_orders(price_frame, benchmark_frame, symbol: str, initial_cash: float):
    return simulate_candidate_orders(
        spec=WINNER_SPEC,
        price_frame=price_frame,
        benchmark_frame=benchmark_frame,
        symbol=symbol,
        initial_cash=initial_cash,
    )
""" % (str(SCRIPT_DIR), spec_json, winner_spec.benchmark_symbol)
    (run_dir / "winner_strategy.py").write_text(script, encoding="utf-8")


def write_winner_runner_script(run_dir: Path, config: ResearchConfig) -> None:
    config_payload = {
        "symbol": config.symbol.upper(),
        "region": config.region,
        "provider_uri": config.provider_uri,
        "start_date": config.start_date,
        "end_date": config.end_date,
        "benchmark_symbol": config.resolved_benchmark_symbol(),
        "initial_cash": config.initial_cash,
        "auto_download_us_data": config.auto_download_us_data,
    }
    script = """from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent
EXAMPLE_DIR = Path(r'''%s''')
for candidate in [RUN_DIR, EXAMPLE_DIR]:
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import winner_strategy
from research_core import ResearchConfig, run_frozen_winner_backtest


FROZEN_CONFIG = json.loads(%r)


def parse_args():
    parser = argparse.ArgumentParser(description="Replay the exported Codex daily autoresearch winner with Qlib.")
    parser.add_argument("--provider-uri", default=FROZEN_CONFIG["provider_uri"])
    parser.add_argument("--output-dir", default=str(RUN_DIR / "rerun"))
    parser.add_argument("--auto-download-us-data", dest="auto_download_us_data", action="store_true")
    parser.add_argument("--no-auto-download-us-data", dest="auto_download_us_data", action="store_false")
    parser.set_defaults(auto_download_us_data=bool(FROZEN_CONFIG["auto_download_us_data"]))
    return parser.parse_args()


def main():
    args = parse_args()
    config = ResearchConfig(
        symbol=FROZEN_CONFIG["symbol"],
        region=FROZEN_CONFIG["region"],
        provider_uri=args.provider_uri,
        start_date=FROZEN_CONFIG["start_date"],
        end_date=FROZEN_CONFIG["end_date"],
        benchmark_symbol=FROZEN_CONFIG["benchmark_symbol"],
        initial_cash=float(FROZEN_CONFIG["initial_cash"]),
        output_dir=args.output_dir,
        auto_download_us_data=bool(args.auto_download_us_data),
    )
    result = run_frozen_winner_backtest(
        config=config,
        winner_spec=winner_strategy.get_spec(),
        output_dir=Path(args.output_dir),
    )
    print("Winner replay output:", result["output_dir"])


if __name__ == "__main__":
    main()
""" % (str(SCRIPT_DIR), json.dumps(config_payload, indent=2, sort_keys=True))
    (run_dir / "run_winner_backtest.py").write_text(script, encoding="utf-8")


def write_winner_doc(run_dir: Path, config: ResearchConfig, best_evaluation: CandidateEvaluation) -> None:
    signals = ", ".join(best_evaluation.spec.enabled_signals)
    metrics = best_evaluation.metrics
    lines = [
        "# %s Winner Strategy" % config.symbol.upper(),
        "",
        "## Summary",
        "",
        "- Universe: `%s`" % config.symbol.upper(),
        "- Benchmark regime reference: `%s`" % (best_evaluation.spec.benchmark_symbol or "none"),
        "- Frequency: `day` signal, next-day open execution.",
        "- Active signals: `%s`." % signals,
        "- Position model: fixed `%0.2f%%` of account on entry." % (best_evaluation.spec.position_size_pct * 100.0),
        "",
        "## Final Metrics",
        "",
        "- Research score: `%0.4f`" % metrics["research_score"],
        "- Sharpe: `%0.4f`" % metrics["sharpe"],
        "- Cumulative return: `%0.2f%%`" % (metrics["cumulative_return"] * 100.0),
        "- Max drawdown: `%0.2f%%`" % metrics["max_drawdown_pct"],
        "- Completed trades: `%d`" % metrics["completed_trades"],
        "",
        "## Frozen DSL",
        "",
        "```json",
        json.dumps(best_evaluation.spec.to_dict(), indent=2, sort_keys=True),
        "```",
    ]
    (run_dir / "winner_strategy.md").write_text("\n".join(lines), encoding="utf-8")


def build_research_report(
    *,
    config: ResearchConfig,
    market_summary: Dict[str, Any],
    round_results: Sequence[ResearchRoundResult],
    leaderboard: pd.DataFrame,
    best_evaluation: CandidateEvaluation,
) -> str:
    final_metrics = best_evaluation.metrics
    top_rows = build_round_summary_rows(sort_leaderboard_frame(leaderboard), limit=10)
    lines = [
        "# %s Daily Codex Autoresearch Report" % config.symbol.upper(),
        "",
        "## Summary",
        "",
        "- Region: `%s`" % config.region,
        "- Research window: `%s` to `%s`." % (market_summary["start_date"], market_summary["end_date"]),
        "- Provider mode: `%s`." % market_summary["provider_mode"],
        "- Final winner: `%s`." % best_evaluation.spec.name,
        "- Final research score: `%0.4f`." % final_metrics["research_score"],
        "",
        "## Market Summary",
        "",
        "```json",
        json.dumps(market_summary, indent=2, sort_keys=True),
        "```",
        "",
        "## Rounds",
        "",
    ]
    for result in round_results:
        lines.extend(
            [
                "### %s" % result.round_name,
                "",
                "- Objective: %s" % result.objective,
                "- Candidate count: `%d`" % result.candidate_count,
                "- Winner: `%s`" % result.winner_name,
                "- Improved best: `%s`" % ("yes" if result.improved_best else "no"),
                "- Leaderboard: `%s`" % result.leaderboard_path,
                "",
            ]
        )
    lines.extend(
        [
            "## Global Leaderboard",
            "",
            dataframe_to_markdown(pd.DataFrame(top_rows)),
            "",
            "## Final Winner",
            "",
            "```json",
            json.dumps(best_evaluation.spec.to_dict(), indent=2, sort_keys=True),
            "```",
            "",
            "## Output Files",
            "",
            "- `leaderboard.csv`",
            "- `experiment_log.jsonl`",
            "- `research_report.md`",
            "- `codex_trace/`",
            "- `winner_strategy.py`",
            "- `run_winner_backtest.py`",
            "- `orders.csv`",
            "- `report_1day.csv`",
            "- `winner_strategy.md`",
            "- `winner_summary.json`",
        ]
    )
    return "\n".join(lines)


def run_frozen_winner_backtest(
    config: ResearchConfig,
    winner_spec: CandidateStrategySpec,
    output_dir: Path,
    data_bundle: Optional[ResearchDataBundle] = None,
) -> Dict[str, Any]:
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle, _market_summary = prepare_data_bundle(config, output_dir, data_bundle=data_bundle)
    if bundle.provider_path is not None:
        init_qlib(bundle.provider_path, config.region)
    evaluation = evaluate_candidate_spec(
        spec=winner_spec,
        config=config,
        bundle=bundle,
        round_number=999,
        round_name="winner_replay",
        origin="export",
    )
    evaluation.orders_frame.to_csv(output_dir / "orders.csv", index=False)
    evaluation.signal_frame.to_csv(output_dir / "signal_diagnostics.csv", index=False)
    evaluation.daily_state.to_csv(output_dir / "daily_state.csv", index=False)
    evaluation.completed_trades.to_csv(output_dir / "completed_trades.csv", index=False)
    evaluation.report_frame.to_csv(output_dir / "report_1day.csv")
    (output_dir / "winner_summary.json").write_text(json.dumps(evaluation.metrics, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "output_dir": str(output_dir),
        "metrics": evaluation.metrics,
    }


def _copy_final_artifacts(run_dir: Path, best_evaluation: CandidateEvaluation) -> None:
    best_evaluation.orders_frame.to_csv(run_dir / "orders.csv", index=False)
    best_evaluation.report_frame.to_csv(run_dir / "report_1day.csv")
    best_evaluation.signal_frame.to_csv(run_dir / "signal_diagnostics.csv", index=False)
    best_evaluation.completed_trades.to_csv(run_dir / "completed_trades.csv", index=False)
    best_evaluation.daily_state.to_csv(run_dir / "daily_state.csv", index=False)
    (run_dir / "winner_summary.json").write_text(
        json.dumps(best_evaluation.metrics, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def run_autoresearch(
    config: ResearchConfig,
    codex_client: Optional[Any] = None,
    data_bundle: Optional[ResearchDataBundle] = None,
) -> Dict[str, Any]:
    import time

    start_ts = time.time()
    run_dir = ensure_output_dir(config.output_dir)
    trace_dir = run_dir / "codex_trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_config.json").write_text(json.dumps(config.to_prompt_summary(), indent=2, sort_keys=True), encoding="utf-8")

    bundle, market_summary = prepare_data_bundle(config, run_dir, data_bundle=data_bundle)
    if bundle.provider_path is not None:
        init_qlib(bundle.provider_path, config.region)

    if codex_client is None:
        codex_client = CodexResponsesClient(
            auth_profile_id=config.auth_profile_id,
            state_dir=config.state_dir,
        )

    experiment_logs = []
    round_results = []
    all_evaluations = []
    seen_fingerprints = set()
    rejection_memory = []
    best_evaluation = None

    default_benchmark_symbol = config.resolved_benchmark_symbol()
    baseline_specs = build_baseline_specs(default_benchmark_symbol)
    baseline_evaluations = evaluate_candidate_batch(
        baseline_specs,
        config=config,
        bundle=bundle,
        round_number=0,
        round_name="round_00_baseline",
        origin="baseline",
    )
    all_evaluations.extend(baseline_evaluations)
    for evaluation in baseline_evaluations:
        seen_fingerprints.add(evaluation.spec.fingerprint())
    baseline_winner = next((item for item in baseline_evaluations if item.metrics["accepted"]), baseline_evaluations[0])
    if not baseline_winner.metrics["accepted"]:
        raise RuntimeError("No viable baseline strategy survived the hard rejection rules.")
    best_evaluation = baseline_winner
    baseline_round_dir = run_dir / "round_00_baseline"
    baseline_leaderboard_path = persist_round_artifacts(baseline_round_dir, baseline_evaluations, baseline_winner.spec.fingerprint())
    round_results.append(
        ResearchRoundResult(
            round_number=0,
            round_name="round_00_baseline",
            objective="Seed the autoresearch run with fixed baseline strategies.",
            candidate_count=len(baseline_evaluations),
            winner_name=baseline_winner.spec.name,
            winner_spec=baseline_winner.spec.to_dict(),
            winner_metrics=dict(baseline_winner.metrics),
            improved_best=True,
            leaderboard_path=str(baseline_leaderboard_path),
        )
    )
    for evaluation in baseline_evaluations:
        experiment_logs.append(
            {
                "event": "candidate_evaluation",
                "round_number": 0,
                "round_name": "round_00_baseline",
                "candidate_name": evaluation.spec.name,
                "metrics": evaluation.metrics,
                "spec": evaluation.spec.to_dict(),
            }
        )

    no_improve_rounds = 0
    for round_number in range(1, config.max_rounds + 1):
        if no_improve_rounds >= config.patience:
            break
        if time.time() - start_ts >= config.time_budget_sec:
            break

        global_leaderboard = leaderboard_frame_from_evaluations(all_evaluations)
        system_prompt = "\n\n".join([build_system_prompt(), build_schema_prompt()])
        user_prompt = build_round_prompt(
            config_summary=config.to_prompt_summary(),
            market_summary=market_summary,
            leaderboard_rows=build_round_summary_rows(global_leaderboard),
            rejected_patterns=rejection_memory,
            current_best={
                "name": best_evaluation.spec.name,
                "metrics": best_evaluation.metrics,
                "spec": best_evaluation.spec.to_dict(),
            },
            round_number=round_number,
            candidates_per_round=config.candidates_per_round,
        )

        parsed_payload, raw_text, response_json = codex_client.request_candidates(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=config.llm_model,
        )
        response = CodexProposalResponse(parsed_payload=parsed_payload, raw_text=raw_text, response_json=response_json)
        write_trace_files(trace_dir, "round_%02d" % round_number, system_prompt, user_prompt, response)

        raw_candidates = parsed_payload.get("candidates") or []
        if not isinstance(raw_candidates, list):
            raw_candidates = []
        raw_ablation = parsed_payload.get("ablation")
        if isinstance(raw_ablation, dict):
            raw_candidates.append(raw_ablation)

        candidate_specs, validation_rejections = normalize_candidate_payloads(
            raw_candidates,
            default_benchmark_symbol=default_benchmark_symbol,
            seen_fingerprints=seen_fingerprints,
        )
        rejection_memory.extend(validation_rejections)
        if not candidate_specs:
            no_improve_rounds += 1
            experiment_logs.append(
                {
                    "event": "round_skipped",
                    "round_number": round_number,
                    "reason": "no_valid_candidates",
                    "validation_rejections": validation_rejections,
                }
            )
            continue

        round_name = "round_%02d_codex" % round_number
        evaluations = evaluate_candidate_batch(
            candidate_specs,
            config=config,
            bundle=bundle,
            round_number=round_number,
            round_name=round_name,
            origin="codex",
        )
        all_evaluations.extend(evaluations)
        for evaluation in evaluations:
            seen_fingerprints.add(evaluation.spec.fingerprint())
            if not evaluation.metrics["accepted"]:
                rejection_memory.append(
                    "hard_reject:%s:%s" % (evaluation.spec.name, ",".join(evaluation.metrics["rejection_reasons"]))
                )

        round_winner = evaluations[0]
        improved_best = bool(round_winner.metrics["accepted"]) and float(round_winner.metrics["research_score"]) > float(
            best_evaluation.metrics["research_score"]
        )
        if improved_best:
            best_evaluation = round_winner
            no_improve_rounds = 0
        else:
            no_improve_rounds += 1

        round_dir = run_dir / round_name
        leaderboard_path = persist_round_artifacts(round_dir, evaluations, round_winner.spec.fingerprint())
        round_results.append(
            ResearchRoundResult(
                round_number=round_number,
                round_name=round_name,
                objective="Codex proposes new typed DSL candidates plus one ablation.",
                candidate_count=len(evaluations),
                winner_name=round_winner.spec.name,
                winner_spec=round_winner.spec.to_dict(),
                winner_metrics=dict(round_winner.metrics),
                improved_best=improved_best,
                leaderboard_path=str(leaderboard_path),
            )
        )
        for evaluation in evaluations:
            experiment_logs.append(
                {
                    "event": "candidate_evaluation",
                    "round_number": round_number,
                    "round_name": round_name,
                    "candidate_name": evaluation.spec.name,
                    "metrics": evaluation.metrics,
                    "spec": evaluation.spec.to_dict(),
                }
            )

    if best_evaluation is None:
        raise RuntimeError("Autoresearch did not produce any candidate evaluation.")

    leaderboard = leaderboard_frame_from_evaluations(all_evaluations)
    leaderboard["is_final_winner"] = leaderboard["fingerprint"] == best_evaluation.spec.fingerprint()
    leaderboard.to_csv(run_dir / "leaderboard.csv", index=False)
    write_experiment_log(run_dir / "experiment_log.jsonl", experiment_logs)
    _copy_final_artifacts(run_dir, best_evaluation)
    write_winner_strategy_script(run_dir, best_evaluation.spec)
    write_winner_runner_script(run_dir, config)
    write_winner_doc(run_dir, config, best_evaluation)

    report = build_research_report(
        config=config,
        market_summary=market_summary,
        round_results=round_results,
        leaderboard=leaderboard,
        best_evaluation=best_evaluation,
    )
    (run_dir / "research_report.md").write_text(report, encoding="utf-8")

    return {
        "output_dir": str(run_dir),
        "winner_name": best_evaluation.spec.name,
        "winner_score": float(best_evaluation.metrics["research_score"]),
        "winner_spec": best_evaluation.spec.to_dict(),
        "round_results": [asdict(item) for item in round_results],
    }
