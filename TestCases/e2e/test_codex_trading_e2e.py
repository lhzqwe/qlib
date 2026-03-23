from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from codex_trading.autoresearch import (
    CodexProfile,
    CodexResponsesClient,
    auth_store_lock,
    extract_json_payload,
    import_codex_auth_profile,
    load_auth_store,
    parse_sse_response,
    refresh_codex_profile,
)
from codex_trading.autoresearch.auth import CodexAuthError, CodexReauthRequiredError
from codex_trading.autoresearch.service import (
    ResearchConfig,
    compute_research_score,
    normalize_candidate_payloads,
    run_autoresearch,
    sort_leaderboard_frame,
)
from codex_trading.notifications.feishu import (
    FeishuAppConfig,
    FeishuBotConfig,
    FeishuBotService,
    NotificationConfig,
    TigerRuntimeConfig,
    build_notification_text,
    build_strategy_status_snapshot,
    format_last_execution_message,
    format_trade_summary_message,
    format_strategy_message,
    load_feishu_bot_config,
    resolve_query_command,
)
from codex_trading.tradingbot.tiger import (
    LiveStrategySnapshot,
    TigerDailyBarBundle,
    TigerAccountSummary,
    TigerExecutionRecord,
    TigerPortfolioSnapshot,
    TigerPositionSnapshot,
    TigerQuoteSnapshot,
    TigerTradePlan,
    assess_execution_guard,
    build_execution_key,
    build_live_strategy_snapshot,
    build_tiger_portfolio_snapshot,
    build_tiger_trade_plan,
    get_execution_record,
    load_frozen_strategy_context,
    poll_tiger_order_to_terminal,
    select_paper_account,
    upsert_execution_record,
)
from codex_trading.tradingbot.automation_runtime import (
    AutomationSessionHooks,
    build_trade_summary_payload,
    execute_automation_session,
    _determine_trade_window,
)


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


def build_repo_python_env() -> dict:
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH", "")
    pythonpath_parts = [str(repo_root)]
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return env


def create_temp_provider(tmp_path: Path) -> Path:
    source_dir = tmp_path / "source_csv"
    provider_dir = tmp_path / "qlib_provider"
    repo_root = Path(__file__).resolve().parents[2]
    env = build_repo_python_env()
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
        cwd=str(repo_root),
        env=env,
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
        env=build_repo_python_env(),
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
        "codex_trading.tradingbot.automation_runtime._now_in_market_tz",
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
        "codex_trading.tradingbot.automation_runtime._now_in_market_tz",
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
        "codex_trading.tradingbot.automation_runtime._now_in_market_tz",
        lambda: datetime(2026, 3, 23, 9, 50, tzinfo=market_tz),
    )
    late = _determine_trade_window(
        market_status=MarketStatus("TRADING", datetime(2026, 3, 24, 9, 30, tzinfo=market_tz)),
        execution_window_minutes=15,
        max_preopen_wait_minutes=180,
    )
    assert late["can_trade_today"] is False
    assert late["reason"] == "beyond_execution_window"


def test_poll_tiger_order_falls_back_when_get_order_errors(tmp_path):
    ledger_path = tmp_path / "execution_ledger.json"

    class FakeContract:
        def __init__(self, symbol):
            self.symbol = symbol

    class FakeOrder:
        def __init__(self, status, filled, order_id=9):
            self.order_id = order_id
            self.id = order_id
            self.contract = FakeContract("GOOGL")
            self.action = "BUY"
            self.quantity = 1
            self.filled = filled
            self.avg_fill_price = 305.09
            self.limit_price = None
            self.user_mark = "manual-flow-test"
            self.external_id = self.user_mark
            self.reason = None
            self.trade_time = None
            self.update_time = None
            self.status = status

    class FakeTradeClient:
        def __init__(self):
            self.fetch_count = 0

        def get_order(self, account=None, order_id=None, is_brief=False):
            raise RuntimeError("biz param error")

        def get_open_orders(self, account=None, **kwargs):
            return []

        def get_orders(self, account=None, start_time=None, end_time=None, limit=200, is_brief=False, **kwargs):
            self.fetch_count += 1
            if self.fetch_count == 1:
                return []
            return [FakeOrder("Filled", 1.0, order_id=11)]

        def cancel_order(self, account=None, order_id=None):
            return order_id

    result = poll_tiger_order_to_terminal(
        trade_client=FakeTradeClient(),
        account="paper",
        order_id=9,
        deadline=datetime.now(timezone.utc) + timedelta(seconds=1),
        poll_seconds=1,
        ledger_path=ledger_path,
        execution_key="manual-flow-test",
        signal_bar_date="2026-03-23",
        trade_date="2026-03-23",
    )
    assert result["final_order_status"] == "Filled"
    assert result["filled_quantity"] == 1.0


def create_feishu_status_artifacts(
    tmp_path: Path,
    *,
    with_preview: bool = True,
    with_submission: bool = True,
    with_summary: bool = True,
) -> Path:
    run_dir = tmp_path / "run"
    auto_dir = run_dir / "tiger_paper_auto"
    auto_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "winner_summary.json").write_text(
        json.dumps(
            {
                "candidate_name": "winner_alpha",
                "research_score": 0.9123,
                "sharpe": 1.4567,
                "cumulative_return": 0.238,
                "max_drawdown_pct": -8.1,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (run_dir / "run_config.json").write_text(
        json.dumps({"symbol": "AMD"}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (run_dir / "run_winner_backtest.py").write_text(
        """from __future__ import annotations

import json

FROZEN_CONFIG = json.loads('{\"auto_download_us_data\": true, \"benchmark_symbol\": \"QQQ\", \"end_date\": \"2026-03-23\", \"initial_cash\": 100000.0, \"provider_uri\": \"~/.qlib/qlib_data/us_data\", \"region\": \"us\", \"start_date\": \"2024-03-22\", \"symbol\": \"AMD\"}')
""",
        encoding="utf-8",
    )

    if with_preview:
        (auto_dir / "tiger_paper_auto_preview.json").write_text(
            json.dumps(
                {
                    "run_dir": str(run_dir),
                    "symbol": "AMD",
                    "strategy_name": "winner_alpha",
                    "signal_bar_date": "2026-03-20",
                    "trade_date": "2026-03-23",
                    "live_snapshot": {
                        "symbol": "AMD",
                        "next_session_action": "buy",
                    },
                    "trade_plan": {
                        "action": "buy",
                        "reason": "rebalance_to_target",
                    },
                    "execution_guard": {
                        "decision": "submit_new_order",
                        "reason": "no_conflict",
                    },
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    if with_submission:
        (auto_dir / "tiger_paper_auto_submission.json").write_text(
            json.dumps(
                {
                    "order_id": 12345,
                    "status": "Filled",
                    "final_order_status": "Filled",
                    "filled_quantity": 10.0,
                    "avg_fill_price": 101.25,
                    "symbol": "AMD",
                    "strategy_name": "winner_alpha",
                    "signal_bar_date": "2026-03-20",
                    "trade_date": "2026-03-23",
                    "trade_plan": {
                        "action": "buy",
                        "quantity": 10,
                        "order_type": "market",
                    },
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    if with_summary and with_submission:
        (auto_dir / "tiger_paper_auto_summary.json").write_text(
            json.dumps(
                {
                    "action": "buy",
                    "avg_fill_price": 101.25,
                    "base_currency": "USD",
                    "end_of_day_cash": 1200.5,
                    "end_of_day_position": 10.0,
                    "filled_quantity": 10.0,
                    "final_order_status": "Filled",
                    "order_id": 12345,
                    "planned_quantity": 10,
                    "position_status": "opened",
                    "signal_bar_date": "2026-03-20",
                    "symbol": "AMD",
                    "strategy_name": "winner_alpha",
                    "trade_date": "2026-03-23",
                    "trade_plan": {
                        "action": "buy",
                        "quantity": 10,
                        "order_type": "market",
                    },
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    (auto_dir / "execution_ledger.json").write_text(
        json.dumps(
            {
                "records": {
                    "paper:AMD:winner_alpha:2026-03-23:2026-03-20:buy": {
                        "execution_key": "paper:AMD:winner_alpha:2026-03-23:2026-03-20:buy",
                        "order_id": 12345,
                        "trade_date": "2026-03-23",
                        "signal_bar_date": "2026-03-20",
                        "status": "Filled",
                        "filled_quantity": 10.0,
                        "avg_fill_price": 101.25,
                        "last_checked_at": "2026-03-23T13:31:00+00:00",
                    }
                }
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return run_dir


def create_compact_googl_replay_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "googl_replay_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "benchmark_symbol": "QQQ",
                "end_date": "2026-03-22",
                "initial_cash": 100000.0,
                "provider_uri": "~/.qlib/qlib_data/us_data",
                "region": "us",
                "start_date": "2024-03-22",
                "symbol": "GOOGL",
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (run_dir / "winner_summary.json").write_text(
        json.dumps(
            {
                "candidate_name": "trend_short_momo_market_holdcap_28",
                "research_score": 1.9959469462444672,
                "sharpe": 1.9959469462444672,
                "cumulative_return": 1.0893521585275274,
                "max_drawdown_pct": -9.239786124830896,
                "spec_json": json.dumps(
                    {
                        "atr_stop_mult": 0.0,
                        "atr_window": 14,
                        "bb_percentile": 0.35,
                        "bb_window": 20,
                        "benchmark_symbol": "QQQ",
                        "cooldown_days": 2,
                        "ema_fast": 12,
                        "ema_slow": 40,
                        "enabled_signals": ["ema_trend", "short_momentum", "benchmark_regime"],
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
                        "vote_threshold": 0.67,
                    },
                    sort_keys=True,
                ),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (run_dir / "run_winner_backtest.py").write_text(
        """from __future__ import annotations

import json

FROZEN_CONFIG = json.loads('{\"auto_download_us_data\": true, \"benchmark_symbol\": \"QQQ\", \"end_date\": \"2026-03-22\", \"initial_cash\": 100000.0, \"provider_uri\": \"~/.qlib/qlib_data/us_data\", \"region\": \"us\", \"start_date\": \"2024-03-22\", \"symbol\": \"GOOGL\"}')
""",
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "date": "2024-10-23",
                "account": 107071.89546986391,
                "cash": 5432.966392715476,
                "position_amount": 628.0,
                "close_adj": 161.84542846679688,
                "executed_action": "buy",
                "executed_value": 102875.22741699219,
                "executed_cost": 51.437613708496094,
                "net_return": -0.01188390857957422,
                "peak": 110001.1050709839,
                "drawdown": -0.026628910675304263,
            },
            {
                "date": "2024-10-24",
                "account": 106950.61009510806,
                "cash": 106950.61009510806,
                "position_amount": 0.0,
                "close_adj": 161.7857666015625,
                "executed_action": "sell",
                "executed_value": 101670.14892578125,
                "executed_cost": 152.50522338867188,
                "net_return": -0.0011327470595678957,
                "peak": 110001.1050709839,
                "drawdown": -0.027731493914605276,
            },
        ]
    ).to_csv(run_dir / "daily_state.csv", index=False)
    pd.DataFrame(
        [
            {
                "entry_date": "2024-10-23",
                "exit_date": "2024-10-24",
                "entry_signal_date": "2024-10-22",
                "exit_signal_date": "2024-10-23",
                "pnl": -1409.0213283080957,
                "bars_held": 1,
                "return_pct": -1.368956555512429,
            }
        ]
    ).to_csv(run_dir / "completed_trades.csv", index=False)
    return run_dir


def test_tiger_portfolio_snapshot_and_position_normalization():
    class FakeTradeClient:
        def get_managed_accounts(self):
            return [
                SimpleNamespace(account="20190000000000001", account_type="SEC"),
                SimpleNamespace(account="20190000000000002", account_type="PAPER"),
            ]

        def get_positions(self, account=None, sec_type=None):
            assert account == "20190000000000002"
            assert sec_type == "STK"
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(symbol="AMD", currency="USD"),
                    quantity=10,
                    salable_qty=8,
                    market_value=1025.0,
                    average_cost=100.5,
                ),
                SimpleNamespace(
                    contract=SimpleNamespace(symbol="AAPL", currency="USD"),
                    quantity=4,
                    available_quantity=4,
                    market_value=800.0,
                    avg_cost=190.0,
                ),
            ]

        def get_assets(self, account=None):
            assert account == "20190000000000002"
            return [
                SimpleNamespace(
                    summary=SimpleNamespace(currency="USD", available_funds=1500.0, net_liquidation=5400.0),
                    segments={"S": SimpleNamespace(currency="USD", available_funds=1500.0, net_liquidation=5400.0)},
                )
            ]

        def get_open_orders(self, account=None, sec_type=None):
            assert account == "20190000000000002"
            assert sec_type == "STK"
            return [
                SimpleNamespace(
                    order_id=7,
                    contract=SimpleNamespace(symbol="AMD"),
                    action="BUY",
                    quantity=5,
                    filled=0,
                    avg_fill_price=None,
                    status="Submitted",
                    limit_price=100.0,
                    user_mark="test-7",
                    external_id="test-7",
                    reason=None,
                    trade_time=None,
                    update_time=None,
                )
            ]

    snapshot = build_tiger_portfolio_snapshot(
        trade_client=FakeTradeClient(),
        config_account="20190000000000001",
    )
    assert snapshot.selected_account == "20190000000000002"
    assert snapshot.selected_account_type == "PAPER"
    assert snapshot.available_cash == 1500.0
    assert snapshot.net_liquidation == 5400.0
    assert [item.symbol for item in snapshot.positions] == ["AMD", "AAPL"]
    assert snapshot.positions[0] == TigerPositionSnapshot(
        symbol="AMD",
        quantity=10.0,
        salable_quantity=8.0,
        market_value=1025.0,
        average_cost=100.5,
        currency="USD",
    )
    assert snapshot.open_orders[0]["symbol"] == "AMD"
    assert snapshot.open_orders[0]["status"] == "Submitted"
    assert snapshot.open_orders[0]["limit_price"] == 100.0


def test_feishu_config_loading_and_command_resolution(tmp_path, monkeypatch):
    run_dir = create_feishu_status_artifacts(tmp_path, with_preview=False, with_submission=False)
    config_path = tmp_path / "feishu.yml"
    config_path.write_text(
        json.dumps(
            {
                "feishu": {
                    "app_id": "",
                    "app_secret": "",
                    "domain": "feishu",
                    "push_chat_id": "oc_123456",
                    "allowed_dm_open_ids": ["ou_allowed", "ou_admin"],
                },
                "tiger": {
                    "config_path": "D:/fake/tiger_openapi_config.properties",
                    "paper_account": "20190000000000002",
                },
                "strategy": {
                    "run_dir": str(run_dir),
                },
                "notifications": {
                    "push_preview": True,
                    "push_submission": False,
                    "push_summary": True,
                    "push_errors": True,
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("QLIB_FEISHU_APP_ID", "cli_env_app")
    monkeypatch.setenv("QLIB_FEISHU_APP_SECRET", "env_secret")

    config = load_feishu_bot_config(str(config_path))
    assert config.feishu.app_id == "cli_env_app"
    assert config.feishu.app_secret == "env_secret"
    assert config.feishu.push_chat_id == "oc_123456"
    assert config.feishu.allowed_dm_open_ids == ("ou_allowed", "ou_admin")
    assert config.tiger.paper_account == "20190000000000002"
    assert config.strategy_run_dir == run_dir.resolve()
    assert config.notifications.push_submission is False
    assert config.notifications.push_summary is True

    assert resolve_query_command("持仓") == "positions"
    assert resolve_query_command("strategy now") == "strategy"
    assert resolve_query_command("最近执行") == "last"
    assert resolve_query_command("something else") == "help"


def test_strategy_status_loading_and_notification_formatting(tmp_path):
    run_dir = create_feishu_status_artifacts(tmp_path)
    status = build_strategy_status_snapshot(run_dir)

    assert status.strategy_name == "winner_alpha"
    assert status.symbol == "AMD"
    assert status.latest_submission["order_id"] == 12345

    preview_title, preview_body = build_notification_text("preview", status)
    submission_title, submission_body = build_notification_text("submission", status)
    summary_title, summary_body = build_notification_text("summary", status)
    error_title, error_body = build_notification_text("error", status, error="network timeout", stage="automation")

    assert preview_title == "【买入】盘前计划"
    assert "最近预览:" in preview_body
    assert "- 方向=买入" in preview_body
    assert "- 计划=买入 market / rebalance_to_target" in preview_body
    assert submission_title == "【买入成交】盘中成交"
    assert "动作: 买入" in submission_body
    assert "下单: 买入 10 market" in submission_body
    assert "order_id: 12345" in submission_body
    assert "状态: Filled" in submission_body
    assert summary_title == "【买入】当日交易总结"
    assert "当日总结: 2026-03-23" in summary_body
    assert "结果: 已建仓，未平仓" in summary_body
    assert error_title == "自动化策略异常"
    assert "阶段: automation" in error_body
    assert "错误: network timeout" in error_body


def test_automation_replay_session_end_to_end_and_idempotency(tmp_path):
    run_dir = create_compact_googl_replay_run(tmp_path)
    output_dir = run_dir / "tiger_paper_auto"
    market_tz = ZoneInfo("America/New_York")
    trade_client = SimpleNamespace()
    quote_client = SimpleNamespace()
    tiger_namespace = {"Market": SimpleNamespace(US="US")}
    config_obj = SimpleNamespace(account="20190000000000002")

    snapshots = {
        "2024-10-23": LiveStrategySnapshot(
            symbol="GOOGL",
            strategy_name="trend_short_momo_market_holdcap_28",
            as_of_date="2024-10-22",
            signal_bar_date="2024-10-22",
            next_trade_date="2024-10-23",
            latest_close=164.19187927246094,
            signal_ready=True,
            vote_ratio=1.0,
            signal_states={"ema_trend": True, "short_momentum": True, "benchmark_regime": True},
            current_position_amount=0.0,
            in_position=False,
            cooldown_remaining=0,
            bars_in_position=0,
            next_session_action="buy",
            action_reasons=tuple(),
            target_position_pct=0.95,
            benchmark_symbol="QQQ",
        ),
        "2024-10-24": LiveStrategySnapshot(
            symbol="GOOGL",
            strategy_name="trend_short_momo_market_holdcap_28",
            as_of_date="2024-10-23",
            signal_bar_date="2024-10-23",
            next_trade_date="2024-10-24",
            latest_close=161.84542846679688,
            signal_ready=True,
            vote_ratio=1.0,
            signal_states={"ema_trend": True, "short_momentum": True, "benchmark_regime": True},
            current_position_amount=628.0,
            in_position=True,
            cooldown_remaining=0,
            bars_in_position=1,
            next_session_action="sell",
            action_reasons=("ma_breakdown",),
            target_position_pct=0.95,
            benchmark_symbol="QQQ",
        ),
    }
    account_summaries = {
        "2024-10-23": TigerAccountSummary(
            selected_account="20190000000000002",
            selected_account_type="PAPER",
            available_cash=108359.63142341615,
            base_currency="USD",
            current_quantity=0.0,
            salable_quantity=0.0,
            open_orders=tuple(),
            config_account="20190000000000002",
            net_liquidation=108359.63142341615,
        ),
        "2024-10-24": TigerAccountSummary(
            selected_account="20190000000000002",
            selected_account_type="PAPER",
            available_cash=5432.966392715476,
            base_currency="USD",
            current_quantity=628.0,
            salable_quantity=628.0,
            open_orders=tuple(),
            config_account="20190000000000002",
            net_liquidation=107071.89546986391,
        ),
    }
    quote_snapshots = {
        "2024-10-23": TigerQuoteSnapshot(
            symbol="GOOGL",
            source="tiger_brief",
            is_realtime=True,
            quote_timestamp="2024-10-23T13:30:05+00:00",
            quote_age_seconds=5.0,
            latest_price=163.81,
            ask_price=163.81,
            bid_price=163.79,
            open_price=164.19,
            prev_close=164.19,
            volume=1000000.0,
        ),
        "2024-10-24": TigerQuoteSnapshot(
            symbol="GOOGL",
            source="tiger_brief",
            is_realtime=True,
            quote_timestamp="2024-10-24T13:30:05+00:00",
            quote_age_seconds=5.0,
            latest_price=161.90,
            ask_price=161.90,
            bid_price=161.88,
            open_price=161.85,
            prev_close=161.85,
            volume=1000000.0,
        ),
    }
    trade_plans = {
        "2024-10-23": TigerTradePlan(
            action="buy",
            quantity=628,
            order_type="market",
            can_submit=True,
            reason="ready",
            reference_price=163.81,
            cash_buffer_pct=0.98,
            target_position_pct=0.95,
            quote_is_realtime=True,
            quote_source="tiger_brief",
        ),
        "2024-10-24": TigerTradePlan(
            action="sell",
            quantity=628,
            order_type="market",
            can_submit=True,
            reason="ready",
            reference_price=161.90,
            cash_buffer_pct=0.98,
            target_position_pct=0.95,
            quote_is_realtime=True,
            quote_source="tiger_brief",
        ),
    }
    portfolio_snapshots = {
        "2024-10-23": TigerPortfolioSnapshot(
            selected_account="20190000000000002",
            selected_account_type="PAPER",
            available_cash=5432.966392715476,
            base_currency="USD",
            positions=(
                TigerPositionSnapshot(
                    symbol="GOOGL",
                    quantity=628.0,
                    salable_quantity=628.0,
                    market_value=101670.14892578125,
                    average_cost=163.81,
                    currency="USD",
                ),
            ),
            open_orders=tuple(),
            config_account="20190000000000002",
            net_liquidation=107071.89546986391,
        ),
        "2024-10-24": TigerPortfolioSnapshot(
            selected_account="20190000000000002",
            selected_account_type="PAPER",
            available_cash=106950.61009510806,
            base_currency="USD",
            positions=tuple(),
            open_orders=tuple(),
            config_account="20190000000000002",
            net_liquidation=106950.61009510806,
        ),
    }
    fill_prices = {"2024-10-23": 163.81, "2024-10-24": 161.90}
    order_ids = {"2024-10-23": 8801001, "2024-10-24": 8801002}
    sleep_calls = []

    def build_args(trade_date: str):
        return SimpleNamespace(
            strategy_run_dir=str(run_dir),
            runs_root=str(run_dir.parent),
            tiger_config="D:/fake/tiger_openapi_config.properties",
            paper_account="20190000000000002",
            signal_start_date=None,
            signal_end_date=trade_date,
            order_type="market",
            limit_price=None,
            cash_buffer_pct=0.98,
            output_dir=str(output_dir),
            execution_window_minutes=15,
            max_preopen_wait_minutes=180,
            order_poll_sec=1,
            max_quote_staleness_sec=15,
            bars_lookback_buffer=30,
            feishu_config="mock://feishu",
            submit=True,
        )

    def make_notifier(kind: str, collector: list):
        def _notify(config_path, *, strategy_run_dir_override=None, tiger_config_override=None):
            assert config_path == "mock://feishu"
            status = build_strategy_status_snapshot(Path(strategy_run_dir_override))
            title, body = build_notification_text(kind, status)
            collector.append({"kind": kind, "title": title, "body": body})
            return True

        return _notify

    def make_hooks(trade_date: str, collector: list) -> AutomationSessionHooks:
        open_market = datetime.fromisoformat("%sT09:30:00-04:00" % trade_date).astimezone(market_tz)
        preopen_market = open_market - timedelta(minutes=10)
        submission_deadline = open_market + timedelta(minutes=15)
        market_statuses = [
            SimpleNamespace(market="US", status="NOT_YET_OPEN", trading_status="NOT_YET_OPEN", open_time=open_market),
            SimpleNamespace(market="US", status="TRADING", trading_status="TRADING", open_time=open_market),
        ]
        trade_windows = [
            {
                "can_trade_today": True,
                "reason": "wait_for_open",
                "trade_date": trade_date,
                "now_market": preopen_market,
                "wait_seconds": 1.0,
                "submission_deadline": submission_deadline,
            },
            {
                "can_trade_today": True,
                "reason": "within_execution_window",
                "trade_date": trade_date,
                "now_market": open_market + timedelta(minutes=1),
                "wait_seconds": 0.0,
                "submission_deadline": submission_deadline,
            },
        ]

        def poll_order(**kwargs):
            result = {
                "order_id": kwargs["order_id"],
                "final_order_status": "Filled",
                "filled_quantity": 628.0,
                "avg_fill_price": fill_prices[trade_date],
                "reason": None,
            }
            upsert_execution_record(
                ledger_path=kwargs["ledger_path"],
                execution_key=kwargs["execution_key"],
                order_id=kwargs["order_id"],
                account=kwargs["account"],
                trade_date=kwargs["trade_date"],
                signal_bar_date=kwargs["signal_bar_date"],
                status="Filled",
                filled_quantity=628.0,
                avg_fill_price=fill_prices[trade_date],
            )
            return result

        return AutomationSessionHooks(
            resolve_market_status=lambda quote_client, market_enum: market_statuses.pop(0),
            determine_trade_window=lambda market_status, execution_window_minutes, max_preopen_wait_minutes: trade_windows.pop(0),
            sleep_fn=lambda seconds: sleep_calls.append(seconds),
            load_context=load_frozen_strategy_context,
            build_snapshot=lambda **kwargs: snapshots[kwargs["executable_trade_date"]],
            build_account_summary=lambda **kwargs: account_summaries[trade_date],
            fetch_quote_snapshot=lambda **kwargs: quote_snapshots[trade_date],
            build_trade_plan=lambda **kwargs: trade_plans[kwargs["snapshot"].next_trade_date],
            build_execution_key=build_execution_key,
            fetch_symbol_orders=lambda **kwargs: ([], []),
            assess_execution_guard=assess_execution_guard,
            create_order=lambda **kwargs: {"external_id": kwargs["external_id"], "symbol": kwargs["symbol"]},
            place_order=lambda trade_client, order: order_ids[trade_date],
            upsert_execution_record=upsert_execution_record,
            poll_order=poll_order,
            build_portfolio_snapshot=lambda **kwargs: portfolio_snapshots[trade_date],
            notify_preview=make_notifier("preview", collector),
            notify_submission=make_notifier("submission", collector),
            notify_summary=make_notifier("summary", collector),
        )

    notifications = []
    buy_result = execute_automation_session(
        build_args("2024-10-23"),
        run_dir=run_dir,
        output_dir=output_dir,
        tiger_namespace=tiger_namespace,
        config_obj=config_obj,
        quote_client=quote_client,
        trade_client=trade_client,
        hooks=make_hooks("2024-10-23", notifications),
    )
    sell_result = execute_automation_session(
        build_args("2024-10-24"),
        run_dir=run_dir,
        output_dir=output_dir,
        tiger_namespace=tiger_namespace,
        config_obj=config_obj,
        quote_client=quote_client,
        trade_client=trade_client,
        hooks=make_hooks("2024-10-24", notifications),
    )

    assert buy_result.order_submitted is True
    assert buy_result.preview_payload["trade_date"] == "2024-10-23"
    assert buy_result.preview_payload["trade_plan"]["quantity"] == 628
    assert buy_result.submission_payload["order_id"] == 8801001
    assert buy_result.summary_payload["position_status"] == "opened"
    assert buy_result.summary_payload["end_of_day_cash"] == pytest.approx(5432.966392715476)
    assert buy_result.summary_payload["end_of_day_position"] == pytest.approx(628.0)

    assert sell_result.order_submitted is True
    assert sell_result.preview_payload["trade_date"] == "2024-10-24"
    assert sell_result.preview_payload["trade_plan"]["quantity"] == 628
    assert sell_result.submission_payload["order_id"] == 8801002
    assert sell_result.summary_payload["position_status"] == "closed"
    assert sell_result.summary_payload["realized_pnl"] == pytest.approx(-1409.0213283080957)
    assert sell_result.summary_payload["return_pct"] == pytest.approx(-1.368956555512429)
    assert sell_result.summary_payload["entry_date"] == "2024-10-23"
    assert sell_result.summary_payload["exit_date"] == "2024-10-24"

    assert [item["kind"] for item in notifications] == ["preview", "submission", "preview", "submission"]
    assert notifications[0]["title"] == "【买入】盘前计划"
    assert "trade_date=2024-10-23" in notifications[0]["body"]
    assert "计划=买入 628 market / ready" in notifications[0]["body"]
    assert notifications[1]["title"] == "【买入成交】盘中成交"
    assert "order_id: 8801001" in notifications[1]["body"]
    assert "成交均价: 163.81" in notifications[1]["body"]
    assert "动作: 买入" in notifications[1]["body"]
    assert notifications[2]["title"] == "【卖出】盘前计划"
    assert "trade_date=2024-10-24" in notifications[2]["body"]
    assert "计划=卖出 628 market / ready" in notifications[2]["body"]
    assert notifications[3]["title"] == "【卖出成交】盘中成交"
    assert "order_id: 8801002" in notifications[3]["body"]
    assert "成交均价: 161.9" in notifications[3]["body"]
    assert "动作: 卖出" in notifications[3]["body"]

    idempotent_notifications = []
    idempotent_result = execute_automation_session(
        build_args("2024-10-23"),
        run_dir=run_dir,
        output_dir=output_dir,
        tiger_namespace=tiger_namespace,
        config_obj=config_obj,
        quote_client=quote_client,
        trade_client=trade_client,
        hooks=make_hooks("2024-10-23", idempotent_notifications),
    )
    assert idempotent_result.order_submitted is False
    assert idempotent_result.submission_payload is None
    assert idempotent_result.summary_payload is None
    assert [item["kind"] for item in idempotent_notifications] == ["preview"]
    assert sleep_calls == [1.0, 1.0, 1.0]


def test_strategy_status_loading_without_preview_or_submission(tmp_path):
    run_dir = create_feishu_status_artifacts(tmp_path, with_preview=False, with_submission=False)
    status = build_strategy_status_snapshot(run_dir)

    assert status.latest_preview is None
    assert status.latest_submission is None
    assert "最近预览: 尚未生成" in format_strategy_message(status)
    assert "最近执行: ledger" in format_last_execution_message(status)


def test_feishu_bot_service_queries_and_allowlist(tmp_path):
    run_dir = create_feishu_status_artifacts(tmp_path)
    config = FeishuBotConfig(
        feishu=FeishuAppConfig(
            app_id="cli_test",
            app_secret="secret",
            domain="feishu",
            push_chat_id="oc_push",
            allowed_dm_open_ids=("ou_allowed",),
        ),
        tiger=TigerRuntimeConfig(
            config_path="D:/fake/tiger_openapi_config.properties",
            paper_account="20190000000000002",
        ),
        strategy_run_dir=run_dir,
        notifications=NotificationConfig(),
    )
    portfolio = TigerPortfolioSnapshot(
        selected_account="20190000000000002",
        selected_account_type="PAPER",
        available_cash=1200.0,
        base_currency="USD",
        positions=(
            TigerPositionSnapshot(
                symbol="AMD",
                quantity=10.0,
                salable_quantity=8.0,
                market_value=1012.0,
                average_cost=98.5,
                currency="USD",
            ),
        ),
        open_orders=tuple(),
        config_account="20190000000000001",
        net_liquidation=5000.0,
    )

    class FakeMessenger:
        def __init__(self):
            self.replies = []

        def reply_text(self, message_id, text):
            self.replies.append((message_id, text))
            return {"message_id": message_id}

    messenger = FakeMessenger()
    service = FeishuBotService(
        config,
        messenger=messenger,
        portfolio_loader=lambda _config: portfolio,
        strategy_loader=lambda path: build_strategy_status_snapshot(path),
    )

    def build_event(open_id: str, chat_type: str, text: str):
        return SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=open_id)),
                message=SimpleNamespace(
                    chat_type=chat_type,
                    message_id="msg-%s-%s" % (open_id, text),
                    message_type="text",
                    content=json.dumps({"text": text}, ensure_ascii=False),
                ),
            )
        )

    service.handle_message_event(build_event("ou_allowed", "p2p", "持仓"))
    service.handle_message_event(build_event("ou_allowed", "p2p", "状态"))
    service.handle_message_event(build_event("ou_allowed", "p2p", "最近执行"))
    service.handle_message_event(build_event("ou_denied", "p2p", "持仓"))
    service.handle_message_event(build_event("ou_allowed", "group", "持仓"))

    assert len(messenger.replies) == 4
    reply_texts = [item[1] for item in messenger.replies]
    assert "账户:" in reply_texts[0]
    assert "最近预览:" in reply_texts[1]
    assert "最近执行: submission" in reply_texts[2]
    assert "未授权访问当前账户信息" in reply_texts[3]
