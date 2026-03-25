from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol, Sequence, Tuple

import yaml

from ..layout import get_repo_layout
from ..tradingbot.tiger import (
    build_tiger_portfolio_snapshot,
    connect_tiger_clients,
    find_latest_run_dir,
    mask_account,
)
from .base import NotificationMessage


HELP_TEXT = """可用命令:
- 帮助 / help
- 持仓 / positions
- 状态 / strategy
- 最近执行 / last"""


@dataclass(frozen=True)
class FeishuAppConfig:
    app_id: str
    app_secret: str
    domain: str
    push_chat_id: str
    allowed_dm_open_ids: Tuple[str, ...]


@dataclass(frozen=True)
class TigerRuntimeConfig:
    config_path: str
    paper_account: Optional[str] = None


@dataclass(frozen=True)
class NotificationConfig:
    push_preview: bool = True
    push_submission: bool = True
    push_summary: bool = False
    push_errors: bool = True


@dataclass(frozen=True)
class FeishuBotConfig:
    feishu: FeishuAppConfig
    tiger: TigerRuntimeConfig
    strategy_run_dir: Path
    notifications: NotificationConfig
    artifacts_dir: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["strategy_run_dir"] = str(self.strategy_run_dir)
        payload["artifacts_dir"] = str(self.artifacts_dir) if self.artifacts_dir else None
        return payload


@dataclass(frozen=True)
class StrategyStatusSnapshot:
    run_dir: Path
    symbol: Optional[str]
    strategy_name: str
    research_score: Optional[float]
    sharpe: Optional[float]
    cumulative_return_pct: Optional[float]
    max_drawdown_pct: Optional[float]
    preview_path: Optional[Path] = None
    latest_preview: Optional[Dict[str, Any]] = None
    submission_path: Optional[Path] = None
    latest_submission: Optional[Dict[str, Any]] = None
    summary_path: Optional[Path] = None
    latest_summary: Optional[Dict[str, Any]] = None
    ledger_path: Optional[Path] = None
    latest_execution_record: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_dir": str(self.run_dir),
            "symbol": self.symbol,
            "strategy_name": self.strategy_name,
            "research_score": self.research_score,
            "sharpe": self.sharpe,
            "cumulative_return_pct": self.cumulative_return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "preview_path": str(self.preview_path) if self.preview_path else None,
            "latest_preview": self.latest_preview,
            "submission_path": str(self.submission_path) if self.submission_path else None,
            "latest_submission": self.latest_submission,
            "summary_path": str(self.summary_path) if self.summary_path else None,
            "latest_summary": self.latest_summary,
            "ledger_path": str(self.ledger_path) if self.ledger_path else None,
            "latest_execution_record": self.latest_execution_record,
        }


class FeishuMessenger(Protocol):
    def send_group_text(self, chat_id: str, text: str) -> Dict[str, Any]:
        ...

    def send_group_card(self, chat_id: str, title: str, markdown: str, fallback_text: str) -> Dict[str, Any]:
        ...

    def reply_text(self, message_id: str, text: str) -> Dict[str, Any]:
        ...


class _SdkNamespace(Protocol):
    Client: Any
    EventDispatcherHandler: Any
    AppType: Any
    LogLevel: Any
    ws: Any
    FEISHU_DOMAIN: str
    LARK_DOMAIN: str


_ASYNC_FEISHU_EXECUTOR: Optional[ThreadPoolExecutor] = None
_ASYNC_FEISHU_EXECUTOR_LOCK = threading.Lock()
LAYOUT = get_repo_layout()


def _require_lark_oapi() -> _SdkNamespace:
    try:
        import lark_oapi as sdk  # type: ignore[import-not-found]
        from lark_oapi.api.im.v1 import (  # type: ignore[import-not-found]
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )
        from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "lark-oapi is required for Feishu bot support. Install it with: pip install 'pyqlib[feishu]'"
        ) from exc
    sdk.FEISHU_DOMAIN = FEISHU_DOMAIN
    sdk.LARK_DOMAIN = LARK_DOMAIN
    sdk.CreateMessageRequest = CreateMessageRequest
    sdk.CreateMessageRequestBody = CreateMessageRequestBody
    sdk.ReplyMessageRequest = ReplyMessageRequest
    sdk.ReplyMessageRequestBody = ReplyMessageRequestBody
    return sdk


def _env_first(names: Sequence[str]) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _resolve_text(
    raw_value: Any,
    env_names: Sequence[str],
    *,
    default: Optional[str] = None,
    required: bool = False,
    label: str,
) -> Optional[str]:
    value = str(raw_value).strip() if raw_value is not None and str(raw_value).strip() else _env_first(env_names)
    if value is None or value == "":
        value = default
    if required and (value is None or value == ""):
        raise ValueError("Missing required Feishu config field: %s" % label)
    return value


def _resolve_string_list(raw_value: Any, env_names: Sequence[str]) -> Tuple[str, ...]:
    if isinstance(raw_value, (list, tuple)):
        values = [str(item).strip() for item in raw_value if str(item).strip()]
        return tuple(values)
    env_value = _env_first(env_names)
    if env_value:
        values = [item.strip() for item in env_value.split(",") if item.strip()]
        return tuple(values)
    return tuple()


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_existing(paths: Sequence[Path]) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None, None
    chosen = sorted(existing, key=lambda item: item.stat().st_mtime)[-1]
    return chosen, _read_json(chosen)


def _latest_ledger_record(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    records = payload.get("records") or {}
    if not isinstance(records, dict) or not records:
        return None
    ranked = sorted(
        (value for value in records.values() if isinstance(value, dict)),
        key=lambda item: str(item.get("last_checked_at") or ""),
    )
    return dict(ranked[-1]) if ranked else None


def _format_currency(amount: Optional[float], currency: Optional[str]) -> str:
    if amount is None:
        return "n/a"
    return "%s %.2f" % (str(currency or "USD"), float(amount))


def _format_percent(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return "%.2f%%" % float(value)


def _format_ratio(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return ("%0." + str(int(digits)) + "f") % float(value)


def _action_label(action: Any, default: str = "观望") -> str:
    normalized = str(action or "").strip().lower()
    mapping = {
        "buy": "买入",
        "sell": "卖出",
        "hold": "观望",
    }
    return mapping.get(normalized, default if normalized == "" else str(action))


def _resolve_status_action(status: "StrategyStatusSnapshot", *, kind: str) -> str:
    if kind == "submission" and status.latest_submission:
        trade_plan = status.latest_submission.get("trade_plan") or {}
        return str(trade_plan.get("action") or status.latest_submission.get("action") or "")
    if kind == "summary" and status.latest_summary:
        return str((status.latest_summary.get("trade_plan") or {}).get("action") or status.latest_summary.get("action") or "")
    if status.latest_preview:
        trade_plan = status.latest_preview.get("trade_plan") or {}
        live_snapshot = status.latest_preview.get("live_snapshot") or {}
        return str(trade_plan.get("action") or live_snapshot.get("next_session_action") or "")
    return ""


def _format_trade_plan_summary(trade_plan: Optional[Dict[str, Any]]) -> str:
    if not isinstance(trade_plan, dict) or not trade_plan:
        return "n/a"
    action = _action_label(trade_plan.get("action") or "hold")
    quantity = trade_plan.get("quantity")
    order_type = str(trade_plan.get("order_type") or "market")
    parts = [action]
    if quantity is not None and quantity != "":
        quantity_value = float(quantity)
        if quantity_value.is_integer():
            parts.append(str(int(quantity_value)))
        else:
            parts.append(_format_ratio(quantity_value, 2))
    if order_type:
        parts.append(order_type)
    summary = " ".join(parts)
    limit_price = trade_plan.get("limit_price")
    if limit_price is not None and limit_price != "":
        summary += " @ %s" % _format_ratio(float(limit_price), 2)
    return summary


def _resolve_feishu_domain(sdk: _SdkNamespace, domain: str) -> str:
    normalized = str(domain or "feishu").strip().lower()
    if normalized == "lark":
        return sdk.LARK_DOMAIN
    if normalized == "feishu":
        return sdk.FEISHU_DOMAIN
    return str(domain).strip()


def _safe_message_text(message_type: Optional[str], content: Optional[str]) -> str:
    raw_content = str(content or "")
    if str(message_type or "").lower() != "text":
        return ""
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError:
        return raw_content.strip()
    if isinstance(payload, dict):
        return str(payload.get("text") or "").strip()
    return raw_content.strip()


def resolve_query_command(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return "help"
    head = normalized.split()[0].strip().lower()
    mapping = {
        "帮助": "help",
        "help": "help",
        "/help": "help",
        "持仓": "positions",
        "positions": "positions",
        "/positions": "positions",
        "状态": "strategy",
        "strategy": "strategy",
        "/strategy": "strategy",
        "最近执行": "last",
        "last": "last",
        "/last": "last",
    }
    return mapping.get(head, "help")


def load_feishu_bot_config(
    config_path: str,
    *,
    strategy_run_dir_override: Optional[str] = None,
    tiger_config_override: Optional[str] = None,
    artifacts_dir_override: Optional[str] = None,
) -> FeishuBotConfig:
    path = Path(config_path).expanduser().resolve()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("Feishu config must be a mapping: %s" % path)

    feishu_payload = payload.get("feishu") or {}
    tiger_payload = payload.get("tiger") or {}
    strategy_payload = payload.get("strategy") or {}
    notifications_payload = payload.get("notifications") or {}

    app_id = _resolve_text(
        feishu_payload.get("app_id"),
        ["QLIB_FEISHU_APP_ID", "FEISHU_APP_ID"],
        required=True,
        label="feishu.app_id",
    )
    app_secret = _resolve_text(
        feishu_payload.get("app_secret"),
        ["QLIB_FEISHU_APP_SECRET", "FEISHU_APP_SECRET"],
        required=True,
        label="feishu.app_secret",
    )
    domain = _resolve_text(
        feishu_payload.get("domain"),
        ["QLIB_FEISHU_DOMAIN", "FEISHU_DOMAIN"],
        default="feishu",
        label="feishu.domain",
    )
    push_chat_id = _resolve_text(
        feishu_payload.get("push_chat_id"),
        ["QLIB_FEISHU_PUSH_CHAT_ID", "FEISHU_PUSH_CHAT_ID"],
        required=True,
        label="feishu.push_chat_id",
    )
    allowed_dm_open_ids = _resolve_string_list(
        feishu_payload.get("allowed_dm_open_ids"),
        ["QLIB_FEISHU_ALLOWED_DM_OPEN_IDS", "FEISHU_ALLOWED_DM_OPEN_IDS"],
    )
    tiger_config_path = _resolve_text(
        tiger_config_override or tiger_payload.get("config_path"),
        ["QLIB_TIGER_CONFIG_PATH", "TIGEROPEN_PROPS_PATH"],
        required=True,
        label="tiger.config_path",
    )
    paper_account = _resolve_text(
        tiger_payload.get("paper_account"),
        ["QLIB_TIGER_PAPER_ACCOUNT", "TIGEROPEN_ACCOUNT"],
        label="tiger.paper_account",
    )
    run_dir_text = _resolve_text(
        strategy_run_dir_override or strategy_payload.get("run_dir"),
        ["QLIB_FEISHU_STRATEGY_RUN_DIR"],
        label="strategy.run_dir",
    )
    run_dir = Path(run_dir_text).expanduser().resolve() if run_dir_text else find_latest_run_dir(str(LAYOUT.autoresearch_runs_root))
    artifacts_dir = Path(artifacts_dir_override).expanduser().resolve() if artifacts_dir_override else None
    notifications = NotificationConfig(
        push_preview=_coerce_bool(notifications_payload.get("push_preview"), True),
        push_submission=_coerce_bool(notifications_payload.get("push_submission"), True),
        push_summary=_coerce_bool(notifications_payload.get("push_summary"), False),
        push_errors=_coerce_bool(notifications_payload.get("push_errors"), True),
    )
    return FeishuBotConfig(
        feishu=FeishuAppConfig(
            app_id=str(app_id),
            app_secret=str(app_secret),
            domain=str(domain or "feishu"),
            push_chat_id=str(push_chat_id),
            allowed_dm_open_ids=tuple(allowed_dm_open_ids),
        ),
        tiger=TigerRuntimeConfig(
            config_path=str(tiger_config_path),
            paper_account=str(paper_account) if paper_account else None,
        ),
        strategy_run_dir=run_dir,
        artifacts_dir=artifacts_dir,
        notifications=notifications,
    )


def build_strategy_status_snapshot(run_dir: Path, *, artifacts_dir: Optional[Path] = None) -> StrategyStatusSnapshot:
    run_dir = Path(run_dir).expanduser().resolve()
    winner_summary = _read_json(run_dir / "winner_summary.json")
    if not winner_summary:
        raise FileNotFoundError("winner_summary.json is missing under %s" % run_dir)
    run_config = _read_json(run_dir / "run_config.json") or {}
    artifact_roots = []
    if artifacts_dir is not None:
        artifact_roots.append(Path(artifacts_dir).expanduser().resolve())
    artifact_roots.extend([run_dir / "tiger_paper_auto", run_dir / "tiger_paper"])
    preview_path, latest_preview = _latest_existing(
        [root / "tiger_paper_auto_preview.json" for root in artifact_roots]
        + [root / "tiger_paper_preview.json" for root in artifact_roots]
    )
    submission_path, latest_submission = _latest_existing(
        [root / "tiger_paper_auto_submission.json" for root in artifact_roots]
        + [root / "tiger_paper_submission.json" for root in artifact_roots]
    )
    summary_path, latest_summary = _latest_existing(
        [root / "tiger_paper_auto_summary.json" for root in artifact_roots]
        + [root / "tiger_paper_summary.json" for root in artifact_roots]
    )
    ledger_path, ledger_payload = _latest_existing(
        [root / "execution_ledger.json" for root in artifact_roots]
    )
    cumulative_return_pct = None
    cumulative_return = winner_summary.get("cumulative_return")
    if cumulative_return is not None:
        cumulative_return_pct = float(cumulative_return) * 100.0
    inferred_symbol = run_config.get("symbol")
    if inferred_symbol is None and latest_preview:
        inferred_symbol = (latest_preview.get("live_snapshot") or {}).get("symbol")
    if inferred_symbol is None and latest_submission:
        inferred_symbol = latest_submission.get("symbol")
    if inferred_symbol is None and latest_summary:
        inferred_symbol = latest_summary.get("symbol")
    return StrategyStatusSnapshot(
        run_dir=run_dir,
        symbol=str(inferred_symbol or "") or None,
        strategy_name=str(winner_summary.get("candidate_name") or ""),
        research_score=float(winner_summary["research_score"]) if winner_summary.get("research_score") is not None else None,
        sharpe=float(winner_summary["sharpe"]) if winner_summary.get("sharpe") is not None else None,
        cumulative_return_pct=cumulative_return_pct,
        max_drawdown_pct=float(winner_summary["max_drawdown_pct"]) if winner_summary.get("max_drawdown_pct") is not None else None,
        preview_path=preview_path,
        latest_preview=latest_preview,
        submission_path=submission_path,
        latest_submission=latest_submission,
        summary_path=summary_path,
        latest_summary=latest_summary,
        ledger_path=ledger_path,
        latest_execution_record=_latest_ledger_record(ledger_payload),
    )


def format_portfolio_message(portfolio_snapshot: Any) -> str:
    lines = [
        "账户: %s (%s)" % (mask_account(portfolio_snapshot.selected_account), portfolio_snapshot.selected_account_type),
        "可用现金: %s" % _format_currency(portfolio_snapshot.available_cash, portfolio_snapshot.base_currency),
        "净值: %s" % _format_currency(portfolio_snapshot.net_liquidation, portfolio_snapshot.base_currency),
    ]
    positions = list(getattr(portfolio_snapshot, "positions", ()) or ())
    if positions:
        lines.append("持仓:")
        for item in positions:
            lines.append(
                "- %s: %.2f 股, 可卖 %.2f, 市值 %s, 成本 %s"
                % (
                    item.symbol,
                    float(item.quantity),
                    float(item.salable_quantity),
                    _format_currency(item.market_value, item.currency or portfolio_snapshot.base_currency),
                    _format_ratio(item.average_cost, 2),
                )
            )
    else:
        lines.append("持仓: 空仓")

    open_orders = list(getattr(portfolio_snapshot, "open_orders", ()) or ())
    if open_orders:
        lines.append("挂单:")
        for item in open_orders:
            price_part = "@ MKT"
            if item.get("limit_price") is not None:
                price_part = "@ %.2f" % float(item["limit_price"])
            lines.append(
                "- %s %s %.0f %s, 状态=%s, order_id=%s"
                % (
                    str(item.get("action") or ""),
                    str(item.get("symbol") or ""),
                    float(item.get("quantity") or 0.0),
                    price_part,
                    str(item.get("status") or ""),
                    str(item.get("order_id") or ""),
                )
            )
    else:
        lines.append("挂单: 无")
    return "\n".join(lines)


def format_strategy_message(status: StrategyStatusSnapshot) -> str:
    lines = [
        "策略: %s" % status.strategy_name,
        "标的: %s" % (status.symbol or "n/a"),
        "Research Score: %s" % _format_ratio(status.research_score, 4),
        "Sharpe: %s" % _format_ratio(status.sharpe, 4),
        "累计收益: %s" % _format_percent(status.cumulative_return_pct),
        "最大回撤: %s" % _format_percent(status.max_drawdown_pct),
    ]
    preview = status.latest_preview
    if preview:
        live_snapshot = preview.get("live_snapshot") or {}
        trade_plan = preview.get("trade_plan") or {}
        execution_guard = preview.get("execution_guard") or {}
        action = _action_label(trade_plan.get("action") or live_snapshot.get("next_session_action") or "hold")
        lines.extend(
            [
                "最近预览:",
                "- trade_date=%s" % str(preview.get("trade_date") or "n/a"),
                "- signal_bar=%s" % str(preview.get("signal_bar_date") or "n/a"),
                "- 方向=%s" % action,
                "- 计划=%s / %s" % (_format_trade_plan_summary(trade_plan), str(trade_plan.get("reason") or "n/a")),
                "- guard=%s / %s"
                % (str(execution_guard.get("decision") or "n/a"), str(execution_guard.get("reason") or "n/a")),
            ]
        )
    else:
        lines.append("最近预览: 尚未生成")
    return "\n".join(lines)


def format_last_execution_message(status: StrategyStatusSnapshot) -> str:
    def _display_scalar(value: Any, default: str = "n/a") -> str:
        if value is None or value == "":
            return default
        return str(value)

    submission = status.latest_submission
    if submission:
        trade_plan = submission.get("trade_plan") or {}
        lines = [
            "最近执行: submission",
            "策略: %s" % status.strategy_name,
            "标的: %s" % _display_scalar(submission.get("symbol") or status.symbol),
            "trade_date: %s" % _display_scalar(submission.get("trade_date")),
            "signal_bar: %s" % _display_scalar(submission.get("signal_bar_date")),
            "动作: %s" % _action_label(trade_plan.get("action") or submission.get("action"), "unknown"),
            "下单: %s" % _format_trade_plan_summary(trade_plan),
            "order_id: %s" % _display_scalar(submission.get("order_id")),
            "状态: %s" % _display_scalar(submission.get("final_order_status") or submission.get("status"), "submitted"),
            "成交数量: %s" % _display_scalar(submission.get("filled_quantity")),
            "成交均价: %s" % _display_scalar(submission.get("avg_fill_price")),
        ]
        return "\n".join(lines)

    record = status.latest_execution_record
    if record:
        lines = [
            "最近执行: ledger",
            "策略: %s" % status.strategy_name,
            "trade_date: %s" % _display_scalar(record.get("trade_date")),
            "signal_bar: %s" % _display_scalar(record.get("signal_bar_date")),
            "order_id: %s" % _display_scalar(record.get("order_id")),
            "状态: %s" % _display_scalar(record.get("status")),
            "成交数量: %s" % _display_scalar(record.get("filled_quantity")),
            "成交均价: %s" % _display_scalar(record.get("avg_fill_price")),
        ]
        return "\n".join(lines)

    preview = status.latest_preview
    if preview:
        live_snapshot = preview.get("live_snapshot") or {}
        trade_plan = preview.get("trade_plan") or {}
        return "\n".join(
            [
                "最近执行: preview",
                "策略: %s" % status.strategy_name,
                "trade_date: %s" % str(preview.get("trade_date") or "n/a"),
                "signal_bar: %s" % str(preview.get("signal_bar_date") or "n/a"),
                "方向: %s" % _action_label(trade_plan.get("action") or live_snapshot.get("next_session_action") or "hold"),
                "计划: %s / %s" % (_format_trade_plan_summary(trade_plan), str(trade_plan.get("reason") or "n/a")),
            ]
        )
    return "最近执行: 尚无 preview 或 submission 产物"


def format_trade_summary_message(status: StrategyStatusSnapshot) -> str:
    def _display_scalar(value: Any, default: str = "n/a") -> str:
        if value is None or value == "":
            return default
        return str(value)

    summary = status.latest_summary
    if not summary:
        return format_last_execution_message(status)

    trade_plan = summary.get("trade_plan") or {}
    if not trade_plan:
        trade_plan = {
            "action": summary.get("action"),
            "quantity": summary.get("planned_quantity"),
            "order_type": summary.get("order_type"),
        }
    base_currency = summary.get("base_currency") or "USD"
    lines = [
        "当日总结: %s" % _display_scalar(summary.get("trade_date")),
        "策略: %s" % status.strategy_name,
        "标的: %s" % _display_scalar(summary.get("symbol") or status.symbol),
        "动作: %s" % _action_label(summary.get("action"), "unknown"),
        "下单: %s" % _format_trade_plan_summary(trade_plan),
        "order_id: %s" % _display_scalar(summary.get("order_id")),
        "状态: %s" % _display_scalar(summary.get("final_order_status") or summary.get("status"), "submitted"),
        "成交数量: %s" % _display_scalar(summary.get("filled_quantity")),
        "成交均价: %s" % _display_scalar(summary.get("avg_fill_price")),
        "日终现金: %s" % _format_currency(summary.get("end_of_day_cash"), base_currency),
        "日终持仓: %s" % _display_scalar(summary.get("end_of_day_position")),
    ]
    position_status = str(summary.get("position_status") or "").lower()
    if position_status == "opened":
        lines.append("结果: 已建仓，未平仓")
    elif position_status == "closed":
        lines.append("结果: 已平仓")
    elif position_status:
        lines.append("结果: %s" % position_status)
    realized_pnl = summary.get("realized_pnl")
    if realized_pnl is not None:
        lines.append("realized_pnl: %s" % _format_currency(realized_pnl, base_currency))
    return_pct = summary.get("return_pct")
    if return_pct is not None:
        lines.append("return: %s" % _format_percent(return_pct))
    entry_date = summary.get("entry_date")
    exit_date = summary.get("exit_date")
    if entry_date or exit_date:
        lines.append("round_trip: %s -> %s" % (_display_scalar(entry_date), _display_scalar(exit_date)))
    return "\n".join(lines)


def _build_fallback_strategy_status(run_dir: Path) -> StrategyStatusSnapshot:
    resolved = Path(run_dir).expanduser().resolve()
    return StrategyStatusSnapshot(
        run_dir=resolved,
        symbol=None,
        strategy_name=resolved.name or "unknown",
        research_score=None,
        sharpe=None,
        cumulative_return_pct=None,
        max_drawdown_pct=None,
    )


def build_notification_text(
    kind: str,
    status: StrategyStatusSnapshot,
    *,
    error: Optional[str] = None,
    stage: Optional[str] = None,
) -> Tuple[str, str]:
    action_text = _action_label(_resolve_status_action(status, kind=kind), default="")
    if kind == "preview":
        if action_text:
            title = "【%s】盘前计划" % action_text
        else:
            title = "盘前计划"
    elif kind == "submission":
        if action_text:
            title = "【%s成交】盘中成交" % action_text
        else:
            title = "盘中成交"
    elif kind == "summary":
        if action_text:
            title = "【%s】当日交易总结" % action_text
        else:
            title = "当日交易总结"
    elif kind == "error":
        title = "自动化策略异常"
    else:
        title = "自动化策略通知"
    if kind == "submission":
        body = format_last_execution_message(status)
    elif kind == "summary":
        body = format_trade_summary_message(status)
    elif kind == "error":
        body = "\n".join(
            [
                "阶段: %s" % str(stage or "unknown"),
                "策略: %s" % status.strategy_name,
                "标的: %s" % (status.symbol or "n/a"),
                "run_dir: %s" % status.run_dir,
                "错误: %s" % str(error or "unknown"),
            ]
        )
    else:
        body = format_strategy_message(status)
    return title, body


class LarkFeishuMessenger:
    def __init__(self, config: FeishuBotConfig) -> None:
        sdk = _require_lark_oapi()
        self._sdk = sdk
        builder = sdk.Client.builder()
        self._client = (
            builder.app_id(config.feishu.app_id)
            .app_secret(config.feishu.app_secret)
            .app_type(sdk.AppType.SELF)
            .domain(_resolve_feishu_domain(sdk, config.feishu.domain))
            .log_level(sdk.LogLevel.INFO)
            .build()
        )

    def _send_message(self, receive_id_type: str, receive_id: str, msg_type: str, content: str) -> Dict[str, Any]:
        message_api = self._client.im.v1.message
        request = (
            self._sdk.CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                self._sdk.CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                .uuid(uuid.uuid4().hex)
                .build()
            )
            .build()
        )
        response = message_api.create(request)
        if int(getattr(response, "code", -1)) != 0:
            raise RuntimeError("Feishu create message failed: %s" % getattr(response, "msg", "unknown"))
        data = getattr(response, "data", None)
        return {
            "message_id": getattr(data, "message_id", None),
            "chat_id": getattr(data, "chat_id", None),
            "thread_id": getattr(data, "thread_id", None),
        }

    def send_group_text(self, chat_id: str, text: str) -> Dict[str, Any]:
        content = json.dumps({"text": text}, ensure_ascii=False)
        return self._send_message("chat_id", chat_id, "text", content)

    def send(self, message: NotificationMessage) -> Dict[str, Any]:
        target = str(message.channel_target or "")
        if not target:
            raise ValueError("Feishu notification target is required.")
        return self.send_group_card(target, message.title, message.body, message.body)

    def send_group_card(self, chat_id: str, title: str, markdown: str, fallback_text: str) -> Dict[str, Any]:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": markdown},
            ],
        }
        try:
            return self._send_message("chat_id", chat_id, "interactive", json.dumps(card, ensure_ascii=False))
        except Exception:
            return self.send_group_text(chat_id, fallback_text)

    def reply_text(self, message_id: str, text: str) -> Dict[str, Any]:
        request = (
            self._sdk.ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                self._sdk.ReplyMessageRequestBody.builder()
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .msg_type("text")
                .reply_in_thread(False)
                .uuid(uuid.uuid4().hex)
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.reply(request)
        if int(getattr(response, "code", -1)) != 0:
            raise RuntimeError("Feishu reply message failed: %s" % getattr(response, "msg", "unknown"))
        data = getattr(response, "data", None)
        return {
            "message_id": getattr(data, "message_id", None),
            "chat_id": getattr(data, "chat_id", None),
            "thread_id": getattr(data, "thread_id", None),
        }


class FeishuBotService:
    def __init__(
        self,
        config: FeishuBotConfig,
        *,
        messenger: FeishuMessenger,
        portfolio_loader: Optional[Callable[[FeishuBotConfig], Any]] = None,
        strategy_loader: Optional[Callable[[Path], StrategyStatusSnapshot]] = None,
    ) -> None:
        self._config = config
        self._messenger = messenger
        self._portfolio_loader = portfolio_loader or _load_portfolio_snapshot_from_config
        self._strategy_loader = strategy_loader or build_strategy_status_snapshot

    def _is_dm_allowed(self, open_id: Optional[str]) -> bool:
        if not open_id:
            return False
        return str(open_id) in set(self._config.feishu.allowed_dm_open_ids)

    def handle_message_event(self, data: Any) -> None:
        event = getattr(data, "event", None)
        sender = getattr(event, "sender", None)
        sender_id = getattr(sender, "sender_id", None)
        sender_open_id = getattr(sender_id, "open_id", None)
        message = getattr(event, "message", None)
        chat_type = str(getattr(message, "chat_type", "") or "")
        message_id = str(getattr(message, "message_id", "") or "")
        message_type = getattr(message, "message_type", None)
        content = getattr(message, "content", None)

        if chat_type.lower() != "p2p":
            return
        if not message_id:
            return
        if not self._is_dm_allowed(sender_open_id):
            self._messenger.reply_text(message_id, "未授权访问当前账户信息。请先在配置中添加你的 open_id。")
            return

        text = _safe_message_text(message_type, content)
        command = resolve_query_command(text)
        try:
            if command == "positions":
                reply = format_portfolio_message(self._portfolio_loader(self._config))
            elif command == "strategy":
                reply = format_strategy_message(self._strategy_loader(self._config.strategy_run_dir))
            elif command == "last":
                reply = format_last_execution_message(self._strategy_loader(self._config.strategy_run_dir))
            else:
                reply = HELP_TEXT
        except Exception as exc:
            reply = "处理失败: %s" % str(exc)
        self._messenger.reply_text(message_id, reply)


def _load_portfolio_snapshot_from_config(config: FeishuBotConfig) -> Any:
    _tiger_namespace, config_obj, _quote_client, trade_client = connect_tiger_clients(config.tiger.config_path)
    return build_tiger_portfolio_snapshot(
        trade_client=trade_client,
        config_account=getattr(config_obj, "account", None),
        requested_paper_account=config.tiger.paper_account,
    )


def _safe_send_notification(
    config: FeishuBotConfig,
    *,
    kind: str,
    enabled: bool,
    error: Optional[str] = None,
    stage: Optional[str] = None,
) -> bool:
    if not enabled:
        return False
    try:
        try:
            status = build_strategy_status_snapshot(config.strategy_run_dir, artifacts_dir=config.artifacts_dir)
        except Exception:
            if kind != "error":
                raise
            status = _build_fallback_strategy_status(config.strategy_run_dir)
        title, body = build_notification_text(kind, status, error=error, stage=stage)
        messenger = LarkFeishuMessenger(config)
        messenger.send(
            NotificationMessage(
                kind=kind,
                title=title,
                body=body,
                channel_target=config.feishu.push_chat_id,
                metadata={"strategy_run_dir": str(config.strategy_run_dir)},
            )
        )
        return True
    except Exception as exc:
        print("Feishu %s notification failed: %s" % (kind, exc), file=sys.stderr)
        return False


def _get_async_feishu_executor() -> ThreadPoolExecutor:
    global _ASYNC_FEISHU_EXECUTOR
    with _ASYNC_FEISHU_EXECUTOR_LOCK:
        if _ASYNC_FEISHU_EXECUTOR is None:
            _ASYNC_FEISHU_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="feishu-notify")
        return _ASYNC_FEISHU_EXECUTOR


def _shutdown_async_feishu_executor() -> None:
    global _ASYNC_FEISHU_EXECUTOR
    with _ASYNC_FEISHU_EXECUTOR_LOCK:
        executor = _ASYNC_FEISHU_EXECUTOR
        _ASYNC_FEISHU_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=True)


atexit.register(_shutdown_async_feishu_executor)


def notify_feishu_preview(
    config_path: str,
    *,
    strategy_run_dir_override: Optional[str] = None,
    tiger_config_override: Optional[str] = None,
    artifacts_dir_override: Optional[str] = None,
) -> bool:
    config = load_feishu_bot_config(
        config_path,
        strategy_run_dir_override=strategy_run_dir_override,
        tiger_config_override=tiger_config_override,
        artifacts_dir_override=artifacts_dir_override,
    )
    return _safe_send_notification(
        config,
        kind="preview",
        enabled=config.notifications.push_preview,
    )


def notify_feishu_submission(
    config_path: str,
    *,
    strategy_run_dir_override: Optional[str] = None,
    tiger_config_override: Optional[str] = None,
    artifacts_dir_override: Optional[str] = None,
) -> bool:
    config = load_feishu_bot_config(
        config_path,
        strategy_run_dir_override=strategy_run_dir_override,
        tiger_config_override=tiger_config_override,
        artifacts_dir_override=artifacts_dir_override,
    )
    return _safe_send_notification(
        config,
        kind="submission",
        enabled=config.notifications.push_submission,
    )


def notify_feishu_summary(
    config_path: str,
    *,
    strategy_run_dir_override: Optional[str] = None,
    tiger_config_override: Optional[str] = None,
    artifacts_dir_override: Optional[str] = None,
) -> bool:
    config = load_feishu_bot_config(
        config_path,
        strategy_run_dir_override=strategy_run_dir_override,
        tiger_config_override=tiger_config_override,
        artifacts_dir_override=artifacts_dir_override,
    )
    return _safe_send_notification(
        config,
        kind="summary",
        enabled=config.notifications.push_summary,
    )


def _submit_async_notification(task: Callable[[], bool]) -> Future:
    return _get_async_feishu_executor().submit(task)


def enqueue_feishu_preview(
    config_path: str,
    *,
    strategy_run_dir_override: Optional[str] = None,
    tiger_config_override: Optional[str] = None,
    artifacts_dir_override: Optional[str] = None,
) -> Future:
    return _submit_async_notification(
        lambda: notify_feishu_preview(
            config_path,
            strategy_run_dir_override=strategy_run_dir_override,
            tiger_config_override=tiger_config_override,
            artifacts_dir_override=artifacts_dir_override,
        )
    )


def enqueue_feishu_submission(
    config_path: str,
    *,
    strategy_run_dir_override: Optional[str] = None,
    tiger_config_override: Optional[str] = None,
    artifacts_dir_override: Optional[str] = None,
) -> Future:
    return _submit_async_notification(
        lambda: notify_feishu_submission(
            config_path,
            strategy_run_dir_override=strategy_run_dir_override,
            tiger_config_override=tiger_config_override,
            artifacts_dir_override=artifacts_dir_override,
        )
    )


def enqueue_feishu_summary(
    config_path: str,
    *,
    strategy_run_dir_override: Optional[str] = None,
    tiger_config_override: Optional[str] = None,
    artifacts_dir_override: Optional[str] = None,
) -> Future:
    return _submit_async_notification(
        lambda: notify_feishu_summary(
            config_path,
            strategy_run_dir_override=strategy_run_dir_override,
            tiger_config_override=tiger_config_override,
            artifacts_dir_override=artifacts_dir_override,
        )
    )


def notify_feishu_error(
    config_path: str,
    *,
    strategy_run_dir_override: Optional[str] = None,
    tiger_config_override: Optional[str] = None,
    artifacts_dir_override: Optional[str] = None,
    error: str,
    stage: Optional[str] = None,
) -> bool:
    config = load_feishu_bot_config(
        config_path,
        strategy_run_dir_override=strategy_run_dir_override,
        tiger_config_override=tiger_config_override,
        artifacts_dir_override=artifacts_dir_override,
    )
    return _safe_send_notification(
        config,
        kind="error",
        enabled=config.notifications.push_errors,
        error=error,
        stage=stage,
    )


def enqueue_feishu_error(
    config_path: str,
    *,
    strategy_run_dir_override: Optional[str] = None,
    tiger_config_override: Optional[str] = None,
    artifacts_dir_override: Optional[str] = None,
    error: str,
    stage: Optional[str] = None,
) -> Future:
    return _submit_async_notification(
        lambda: notify_feishu_error(
            config_path,
            strategy_run_dir_override=strategy_run_dir_override,
            tiger_config_override=tiger_config_override,
            artifacts_dir_override=artifacts_dir_override,
            error=error,
            stage=stage,
        )
    )


def run_feishu_bot_service(config_path: str) -> None:
    config = load_feishu_bot_config(config_path)
    sdk = _require_lark_oapi()
    messenger = LarkFeishuMessenger(config)
    service = FeishuBotService(config, messenger=messenger)
    dispatcher = sdk.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(service.handle_message_event).build()
    client = sdk.ws.Client(
        config.feishu.app_id,
        config.feishu.app_secret,
        log_level=sdk.LogLevel.INFO,
        event_handler=dispatcher,
        domain=_resolve_feishu_domain(sdk, config.feishu.domain),
    )
    print("Starting Feishu bot service for run dir:", config.strategy_run_dir)
    print("Push group:", config.feishu.push_chat_id)
    print("Allowed DM open_ids:", len(config.feishu.allowed_dm_open_ids))
    client.start()
