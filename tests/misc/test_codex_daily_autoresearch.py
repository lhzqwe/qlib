from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest


EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "codex_daily_autoresearch"
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from auth import CodexProfile, auth_store_lock, import_codex_auth_profile, load_auth_store, refresh_codex_profile
from codex_client import CodexAuthError, CodexReauthRequiredError, CodexResponsesClient, extract_json_payload, parse_sse_response
from research_core import (
    ResearchConfig,
    build_baseline_specs,
    compute_research_score,
    normalize_candidate_payloads,
    run_autoresearch,
    sort_leaderboard_frame,
)
from tiger_bridge import (
    LiveStrategySnapshot,
    TigerDailyBarBundle,
    TigerAccountSummary,
    TigerExecutionRecord,
    TigerQuoteSnapshot,
    assess_execution_guard,
    build_execution_key,
    build_live_strategy_snapshot,
    build_tiger_trade_plan,
    get_execution_record,
    load_frozen_strategy_context,
    poll_tiger_order_to_terminal,
    select_paper_account,
    upsert_execution_record,
)
from run_tiger_paper_automation import _determine_trade_window


def make_fake_jwt(email: str, expires_in_seconds: int) -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "email": email,
        "exp": int(time.time()) + expires_in_seconds,
    }

    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return "%s.%s." % (encode(header), encode(payload))


class DummyResponse:
    def __init__(self, status_code=200, json_body=None, lines=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self._lines = list(lines or [])
        self.text = text
        self.reason = "error"

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body

    def iter_lines(self):
        for line in self._lines:
            yield line


class FakeCodexClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def request_candidates(self, system_prompt, user_prompt, model):
        payload = self.payloads.pop(0)
        raw_text = json.dumps(payload, indent=2, sort_keys=True)
        response_json = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": raw_text}],
                }
            ],
        }
        return payload, raw_text, response_json


def build_synthetic_source(symbol: str, dates: pd.DatetimeIndex, close_values: np.ndarray) -> pd.DataFrame:
    open_values = close_values * (1.0 + 0.002 * np.sin(np.arange(len(dates)) / 7.0))
    high_values = np.maximum(open_values, close_values) * 1.01
    low_values = np.minimum(open_values, close_values) * 0.99
    volume_values = 1_000_000 * (1.0 + 0.25 * np.sin(np.arange(len(dates)) / 5.0))
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "symbol": symbol,
            "open": open_values,
            "high": high_values,
            "low": low_values,
            "close": close_values,
            "volume": volume_values,
            "factor": 1.0,
            "change": pd.Series(close_values, index=dates).pct_change(fill_method=None).fillna(0.0).to_numpy(),
        }
    )


def build_synthetic_quote_frame(dates: pd.DatetimeIndex, close_values: np.ndarray) -> pd.DataFrame:
    open_values = close_values * (1.0 + 0.002 * np.sin(np.arange(len(dates)) / 7.0))
    high_values = np.maximum(open_values, close_values) * 1.01
    low_values = np.minimum(open_values, close_values) * 0.99
    volume_values = 1_000_000 * (1.0 + 0.25 * np.sin(np.arange(len(dates)) / 5.0))
    return pd.DataFrame(
        {
            "open_adj": open_values,
            "high_adj": high_values,
            "low_adj": low_values,
            "close_adj": close_values,
            "factor": 1.0,
            "volume": volume_values,
            "open_raw": open_values,
            "high_raw": high_values,
            "low_raw": low_values,
            "close_raw": close_values,
        },
        index=pd.DatetimeIndex(dates),
    )


def create_temp_provider(tmp_path: Path) -> Path:
    source_dir = tmp_path / "source_csv"
    provider_dir = tmp_path / "qlib_provider"
    source_dir.mkdir(parents=True, exist_ok=True)

    dates = pd.bdate_range("2020-01-01", periods=420)
    step = np.arange(len(dates))
    nvda_close = 120.0 + 0.03 * step + 12.0 * np.sin(step / 13.0) + 6.0 * np.sin(step / 37.0)
    qqq_close = 80.0 + 0.04 * step + 4.0 * np.sin(step / 29.0)

    build_synthetic_source("NVDA", dates, nvda_close).to_csv(source_dir / "nvda.csv", index=False)
    build_synthetic_source("QQQ", dates, qqq_close).to_csv(source_dir / "qqq.csv", index=False)

    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "scripts" / "dump_bin.py"),
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
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    return provider_dir


def test_auth_import_refresh_and_lock(tmp_path):
    state_dir = tmp_path / "state"
    codex_auth_path = tmp_path / "auth.json"
    expired_access = make_fake_jwt("tester@example.com", -3600)
    fresh_access = make_fake_jwt("tester@example.com", 3600)
    codex_auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": expired_access,
                    "refresh_token": "refresh-old",
                    "account_id": "acct-old",
                },
            }
        ),
        encoding="utf-8",
    )

    profile_id, profile = import_codex_auth_profile(
        state_dir=str(state_dir),
        codex_auth_path=str(codex_auth_path),
    )
    assert profile_id.endswith("tester@example.com")
    assert profile.email == "tester@example.com"

    with auth_store_lock(str(state_dir), timeout_seconds=0.2, poll_seconds=0.01):
        with pytest.raises(TimeoutError):
            with auth_store_lock(str(state_dir), timeout_seconds=0.05, poll_seconds=0.01):
                pass

    def fake_post(*args, **kwargs):
        return DummyResponse(
            status_code=200,
            json_body={
                "access_token": fresh_access,
                "refresh_token": "refresh-new",
                "expires_in": 3600,
                "account_id": "acct-new",
            },
        )

    refreshed_id, refreshed = refresh_codex_profile(
        profile_id,
        state_dir=str(state_dir),
        force=True,
        requests_post=fake_post,
    )
    assert refreshed_id == profile_id
    assert refreshed.refresh == "refresh-new"
    assert refreshed.account_id == "acct-new"
    assert load_auth_store(str(state_dir))["profiles"][profile_id]["refresh"] == "refresh-new"


def test_sse_parsing_and_reauth_handling():
    success_response = DummyResponse(
        lines=[
            b"event: response.completed",
            b'data: {"type":"response.completed","response":{"status":"completed","output_text":"{\\"candidates\\": [], \\"ablation\\": null}"}}',
            b"",
        ]
    )
    payload = parse_sse_response(success_response)
    assert payload["status"] == "completed"
    assert extract_json_payload(payload["output_text"])["candidates"] == []

    malformed_response = DummyResponse(
        lines=[
            b"event: response.output_text.delta",
            b'data: {"type":"response.output_text.delta","delta":"hello"}',
            b"",
        ]
    )
    with pytest.raises(CodexAuthError):
        parse_sse_response(malformed_response)

    client = CodexResponsesClient()
    with pytest.raises(CodexReauthRequiredError):
        client._post({"model": "gpt-5.4"}, CodexProfile(access="bad", refresh="refresh"), requests_post=lambda *a, **k: DummyResponse(status_code=401, json_body={"error": {"message": "unauthorized"}}))


def test_candidate_validation_scoring_and_tiebreaks():
    payloads = [
        {
            "name": "clamped",
            "enabled_signals": ["momentum", "ema_trend"],
            "momentum_window": 999,
            "vote_threshold": 5,
        },
        {
            "name": "duplicate",
            "enabled_signals": ["momentum", "ema_trend"],
            "momentum_window": 999,
            "vote_threshold": 5,
        },
        {
            "name": "bad_signal",
            "enabled_signals": ["unsupported"],
        },
        {
            "name": "unknown_field",
            "enabled_signals": ["momentum"],
            "mystery": 1,
        },
    ]
    specs, rejections = normalize_candidate_payloads(payloads, default_benchmark_symbol="QQQ")
    assert len(specs) == 1
    assert specs[0].momentum_window == 252
    assert specs[0].vote_threshold == 1.0
    assert any("duplicate_candidate" in item for item in rejections)
    assert any("invalid_candidate" in item for item in rejections)

    score = compute_research_score(
        num_bars=252,
        completed_trades=6,
        sharpe=1.5,
        max_drawdown_pct=-10.0,
        annual_turnover=500_000.0,
        initial_cash=100_000.0,
    )
    assert round(score["research_score"], 6) == round(1.5 * np.sqrt(1.0), 6)

    leaderboard = pd.DataFrame(
        [
            {
                "candidate_name": "worse_drawdown",
                "accepted": True,
                "research_score": 1.0,
                "sharpe": 1.0,
                "max_drawdown_pct": -10.0,
                "completed_trades": 3,
                "cumulative_return": 0.2,
            },
            {
                "candidate_name": "better_drawdown",
                "accepted": True,
                "research_score": 1.0,
                "sharpe": 1.0,
                "max_drawdown_pct": -5.0,
                "completed_trades": 3,
                "cumulative_return": 0.2,
            },
        ]
    )
    ranked = sort_leaderboard_frame(leaderboard)
    assert ranked.iloc[0]["candidate_name"] == "better_drawdown"


def test_autoresearch_integration_and_export_replay(tmp_path):
    provider_dir = create_temp_provider(tmp_path)
    config = ResearchConfig(
        symbol="NVDA",
        region="us",
        provider_uri=str(provider_dir),
        start_date="2020-01-01",
        end_date="2021-08-31",
        benchmark_symbol="QQQ",
        initial_cash=100000.0,
        max_rounds=1,
        candidates_per_round=2,
        patience=1,
        time_budget_sec=300,
        output_dir=str(tmp_path / "runs"),
        auto_download_us_data=False,
    )
    fake_client = FakeCodexClient(
        [
            {
                "candidates": [
                    {
                        "name": "codex_candidate",
                        "benchmark_symbol": "QQQ",
                        "enabled_signals": ["momentum", "ema_trend", "rsi"],
                        "momentum_window": 55,
                        "ema_fast": 10,
                        "ema_slow": 35,
                        "rsi_entry_midline": 54,
                        "atr_stop_mult": 2.0,
                        "cooldown_days": 4,
                        "max_hold_days": 45,
                        "vote_threshold": 0.5,
                    }
                ],
                "ablation": {
                    "name": "codex_ablation",
                    "benchmark_symbol": "QQQ",
                    "enabled_signals": ["momentum", "ema_trend"],
                    "momentum_window": 55,
                    "ema_fast": 10,
                    "ema_slow": 35,
                    "vote_threshold": 0.5,
                },
            }
        ]
    )

    result = run_autoresearch(config, codex_client=fake_client)
    run_dir = Path(result["output_dir"])

    for required in [
        "leaderboard.csv",
        "experiment_log.jsonl",
        "research_report.md",
        "winner_strategy.py",
        "run_winner_backtest.py",
        "orders.csv",
        "report_1day.csv",
        "winner_strategy.md",
        "winner_summary.json",
    ]:
        assert (run_dir / required).exists(), required
    assert (run_dir / "codex_trace").is_dir()

    original_summary = json.loads((run_dir / "winner_summary.json").read_text(encoding="utf-8"))
    assert original_summary["accepted"] is True
    assert original_summary["completed_trades"] >= 2

    replay_dir = tmp_path / "replay"
    subprocess.run(
        [
            sys.executable,
            str(run_dir / "run_winner_backtest.py"),
            "--provider-uri",
            str(provider_dir),
            "--output-dir",
            str(replay_dir),
            "--no-auto-download-us-data",
        ],
        check=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    replay_summary = json.loads((replay_dir / "winner_summary.json").read_text(encoding="utf-8"))
    assert abs(replay_summary["research_score"] - original_summary["research_score"]) < 1e-8


def test_tiger_paper_account_selection_and_trade_plan():
    class Profile:
        def __init__(self, account, account_type):
            self.account = account
            self.account_type = account_type

    paper_account, paper_type = select_paper_account(
        config_account="51230001",
        managed_accounts=[Profile("51230001", "STANDARD"), Profile("20191221901212121", "PAPER")],
    )
    assert paper_account == "20191221901212121"
    assert paper_type == "PAPER"

    with pytest.raises(ValueError):
        select_paper_account(config_account=None, managed_accounts=[], requested_paper_account="U1234567")

    live_quote = TigerQuoteSnapshot(
        symbol="AMD",
        source="tiger_brief",
        is_realtime=True,
        quote_timestamp="2026-03-23T13:30:05+00:00",
        quote_age_seconds=5.0,
        latest_price=100.0,
        ask_price=100.0,
        bid_price=99.8,
        open_price=99.5,
        prev_close=98.9,
        volume=1000000.0,
    )
    buy_snapshot = LiveStrategySnapshot(
        symbol="AMD",
        strategy_name="winner",
        as_of_date="2026-03-22",
        signal_bar_date="2026-03-22",
        next_trade_date="2026-03-23",
        latest_close=100.0,
        signal_ready=True,
        vote_ratio=1.0,
        signal_states={"ema_trend": True},
        current_position_amount=0.0,
        in_position=False,
        cooldown_remaining=0,
        bars_in_position=0,
        next_session_action="buy",
        action_reasons=tuple(),
        target_position_pct=0.95,
    )
    flat_account = TigerAccountSummary(
        selected_account="20191221901212121",
        selected_account_type="PAPER",
        available_cash=10000.0,
        base_currency="USD",
        current_quantity=0.0,
        salable_quantity=0.0,
        open_orders=tuple(),
        config_account="51230001",
    )
    buy_plan = build_tiger_trade_plan(
        snapshot=buy_snapshot,
        account_summary=flat_account,
        order_type="market",
        quote_snapshot=live_quote,
        max_quote_staleness_sec=15,
    )
    assert buy_plan.action == "buy"
    assert buy_plan.quantity == 93
    assert buy_plan.can_submit is True

    sell_snapshot = LiveStrategySnapshot(
        symbol="AMD",
        strategy_name="winner",
        as_of_date="2026-03-22",
        signal_bar_date="2026-03-22",
        next_trade_date="2026-03-23",
        latest_close=110.0,
        signal_ready=True,
        vote_ratio=1.0,
        signal_states={"ema_trend": False},
        current_position_amount=50.0,
        in_position=True,
        cooldown_remaining=0,
        bars_in_position=8,
        next_session_action="sell",
        action_reasons=("ma_breakdown",),
        target_position_pct=0.95,
    )
    long_account = TigerAccountSummary(
        selected_account="20191221901212121",
        selected_account_type="PAPER",
        available_cash=250.0,
        base_currency="USD",
        current_quantity=50.0,
        salable_quantity=48.0,
        open_orders=tuple(),
        config_account="20191221901212121",
    )
    sell_plan = build_tiger_trade_plan(
        snapshot=sell_snapshot,
        account_summary=long_account,
        order_type="market",
        quote_snapshot=live_quote,
        max_quote_staleness_sec=15,
    )
    assert sell_plan.action == "sell"
    assert sell_plan.quantity == 48
    assert sell_plan.can_submit is True

    delayed_quote = TigerQuoteSnapshot(
        symbol="AMD",
        source="tiger_delay_brief",
        is_realtime=False,
        quote_timestamp="2026-03-23T13:15:00+00:00",
        quote_age_seconds=900.0,
        latest_price=101.0,
        ask_price=None,
        bid_price=None,
        open_price=100.5,
        prev_close=99.5,
        volume=1000000.0,
    )
    delayed_plan = build_tiger_trade_plan(
        snapshot=buy_snapshot,
        account_summary=flat_account,
        order_type="market",
        quote_snapshot=delayed_quote,
        max_quote_staleness_sec=15,
        require_realtime_quote=True,
    )
    assert delayed_plan.can_submit is False
    assert delayed_plan.reason == "realtime_quote_required"

    execution_key = build_execution_key(
        paper_account="20191221901212121",
        symbol="AMD",
        strategy_name="winner",
        trade_date="2026-03-23",
        signal_bar_date="2026-03-22",
        action="buy",
    )
    same_key_guard = assess_execution_guard(
        execution_key=execution_key,
        symbol="AMD",
        action="BUY",
        open_orders=[
            {
                "order_id": 1,
                "symbol": "AMD",
                "action": "BUY",
                "status": "Submitted",
                "user_mark": execution_key,
                "external_id": execution_key,
            }
        ],
        historical_orders=[],
        ledger_record=None,
    )
    assert same_key_guard.decision == "adopt_existing_order"

    foreign_guard = assess_execution_guard(
        execution_key=execution_key,
        symbol="AMD",
        action="BUY",
        open_orders=[
            {
                "order_id": 2,
                "symbol": "AMD",
                "action": "BUY",
                "status": "Submitted",
                "user_mark": "someone-else",
                "external_id": "someone-else",
            }
        ],
        historical_orders=[],
        ledger_record=None,
    )
    assert foreign_guard.decision == "skip"
    assert foreign_guard.reason == "foreign_same_side_open_order"


def test_load_frozen_strategy_context_and_live_snapshot(tmp_path):
    provider_dir = create_temp_provider(tmp_path)
    config = ResearchConfig(
        symbol="NVDA",
        region="us",
        provider_uri=str(provider_dir),
        start_date="2020-01-01",
        end_date="2021-08-31",
        benchmark_symbol="QQQ",
        initial_cash=100000.0,
        max_rounds=0,
        candidates_per_round=2,
        patience=1,
        time_budget_sec=60,
        output_dir=str(tmp_path / "runs"),
        auto_download_us_data=False,
    )
    result = run_autoresearch(config, codex_client=FakeCodexClient([]))
    run_dir = Path(result["output_dir"])

    context = load_frozen_strategy_context(
        run_dir=run_dir,
        signal_end_date="2021-09-01",
    )
    assert context.symbol == "NVDA"
    assert context.winner_spec.name

    dates = pd.bdate_range("2020-09-01", "2021-09-01")
    step = np.arange(len(dates))
    price_frame = build_synthetic_quote_frame(dates, 120.0 + 0.05 * step + 4.0 * np.sin(step / 9.0))
    benchmark_frame = build_synthetic_quote_frame(dates, 300.0 + 0.03 * step + 1.5 * np.sin(step / 17.0))
    bundle = TigerDailyBarBundle(
        symbol="NVDA",
        price_frame=price_frame,
        benchmark_symbol="QQQ",
        benchmark_frame=benchmark_frame,
        trade_start_date="2020-01-01",
        trade_date="2021-09-01",
        warmup_start_date="2020-09-01",
    )
    snapshot = build_live_strategy_snapshot(context=context, output_dir=tmp_path / "live_bridge", bar_bundle=bundle)
    assert snapshot.symbol == "NVDA"
    assert snapshot.as_of_date < "2021-09-01"
    assert snapshot.signal_bar_date == snapshot.as_of_date
    assert 0.0 <= snapshot.vote_ratio <= 1.0
    assert snapshot.next_session_action in {"", "buy", "sell"}
    assert Path(snapshot.signal_frame_path).exists()
    assert Path(snapshot.daily_state_path).exists()


def test_execution_ledger_and_partial_fill_cancellation(tmp_path):
    ledger_path = tmp_path / "execution_ledger.json"
    record = upsert_execution_record(
        ledger_path=ledger_path,
        execution_key="paper:AMD:winner:2026-03-23:2026-03-22:buy",
        order_id=42,
        account="paper",
        trade_date="2026-03-23",
        signal_bar_date="2026-03-22",
        status="Filled",
        filled_quantity=10.0,
        avg_fill_price=101.25,
    )
    loaded = get_execution_record(ledger_path, record.execution_key)
    assert loaded is not None
    assert loaded.status == "Filled"
    assert loaded.order_id == 42

    class FakeContract:
        def __init__(self, symbol):
            self.symbol = symbol

    class FakeOrder:
        def __init__(self, status, filled, order_id=7):
            self.order_id = order_id
            self.id = order_id
            self.contract = FakeContract("AMD")
            self.action = "BUY"
            self.quantity = 10
            self.filled = filled
            self.avg_fill_price = 100.5
            self.limit_price = None
            self.user_mark = "paper:AMD:winner:2026-03-23:2026-03-22:buy"
            self.external_id = self.user_mark
            self.reason = None
            self.trade_time = None
            self.update_time = None
            self.status = status

    class FakeTradeClient:
        def __init__(self):
            self.cancelled = False

        def get_order(self, account=None, order_id=None, is_brief=False):
            if self.cancelled:
                return FakeOrder("Cancelled", 4.0, order_id=order_id)
            return FakeOrder("PartiallyFilled", 4.0, order_id=order_id)

        def cancel_order(self, account=None, order_id=None):
            self.cancelled = True
            return order_id

    result = poll_tiger_order_to_terminal(
        trade_client=FakeTradeClient(),
        account="paper",
        order_id=7,
        deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
        poll_seconds=1,
        ledger_path=ledger_path,
        execution_key="paper:AMD:winner:2026-03-23:2026-03-22:buy",
        signal_bar_date="2026-03-22",
        trade_date="2026-03-23",
    )
    assert result["final_order_status"] == "PartialFillCancelled"
    assert result["filled_quantity"] == 4.0


def test_tiger_trade_window_logic(monkeypatch):
    market_tz = ZoneInfo("America/New_York")

    class MarketStatus:
        def __init__(self, trading_status, open_time):
            self.trading_status = trading_status
            self.status = trading_status
            self.open_time = open_time

    monkeypatch.setattr(
        "run_tiger_paper_automation._now_in_market_tz",
        lambda: datetime(2026, 3, 23, 9, 20, tzinfo=market_tz),
    )
    preopen = _determine_trade_window(
        market_status=MarketStatus("NOT_YET_OPEN", datetime(2026, 3, 23, 9, 30, tzinfo=market_tz)),
        execution_window_minutes=15,
        max_preopen_wait_minutes=180,
    )
    assert preopen["can_trade_today"] is True
    assert preopen["reason"] == "wait_for_open"
    assert preopen["trade_date"] == "2026-03-23"

    monkeypatch.setattr(
        "run_tiger_paper_automation._now_in_market_tz",
        lambda: datetime(2026, 3, 23, 9, 40, tzinfo=market_tz),
    )
    intraday = _determine_trade_window(
        market_status=MarketStatus("TRADING", datetime(2026, 3, 24, 9, 30, tzinfo=market_tz)),
        execution_window_minutes=15,
        max_preopen_wait_minutes=180,
    )
    assert intraday["can_trade_today"] is True
    assert intraday["reason"] == "within_execution_window"

    monkeypatch.setattr(
        "run_tiger_paper_automation._now_in_market_tz",
        lambda: datetime(2026, 3, 23, 9, 50, tzinfo=market_tz),
    )
    late = _determine_trade_window(
        market_status=MarketStatus("TRADING", datetime(2026, 3, 24, 9, 30, tzinfo=market_tz)),
        execution_window_minutes=15,
        max_preopen_wait_minutes=180,
    )
    assert late["can_trade_today"] is False
    assert late["reason"] == "beyond_execution_window"
