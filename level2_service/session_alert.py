"""Push notifications and pre-open probe for Tonghuashun account session health."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from datetime import date, datetime
from threading import RLock, Thread
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from .app_sessions import ACCOUNT_ROLES, SessionProvider
from .parsed_values import DirectRequestError, is_exchange_fund_symbol


SHANGHAI = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)

SESSION_ALERT_ERROR_CODES = frozenset(
    {
        "DIRECT_SESSION_EXPIRED",
        "DIRECT_SESSION_UNAVAILABLE",
    }
)

_ROLE_TITLES = {
    "main_fund_flow": "同花顺暗盘账号",
    "core_metrics": "同花顺 L2 核心账号",
    "trade_workspace": "同花顺交易工作区",
}

_ROLE_LABELS = {
    "main_fund_flow": "主力暗盘资金账号 (main_fund_flow)",
    "core_metrics": "L2 核心八项账号 (core_metrics)",
    "trade_workspace": "实盘交易工作区 (trade_workspace)",
}

_ROLE_IMPACTS = {
    "main_fund_flow": "请前往 THS 管理后台续签资金账号会话，避免盘中 09:30 起暗盘分钟曲线缺口",
    "core_metrics": "请前往 THS 管理后台续签核心账号会话，避免 9528 大单/散户/MACDFS 直连中断",
    "trade_workspace": "请检查交易设备同花顺工作区/锁屏状态并完成恢复，避免信号触发后无法下单",
}

_TRADE_WORKSPACE_ALERT_CODES = frozenset(
    {
        "BROKER_LOGIN_REQUIRED",
        "TRADE_DEVICE_OFFLINE",
        "TRADE_NAVIGATION_FAILED",
    }
)
_ALERT_ROLES = frozenset((*ACCOUNT_ROLES, "trade_workspace"))


def _supports_force_kwarg(fn: object) -> bool:
    try:
        sig = inspect.signature(fn)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if "force" in sig.parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def _has_enabled_push_channel(config: dict[str, Any]) -> bool:
    bark_ready = bool(config.get("enabled")) and any(
        isinstance(group, dict) and str(group.get("device_key") or "").strip()
        for group in (config.get("bark_groups") or [])
    )
    sc3 = config.get("sc3_bot") or {}
    sc3_ready = bool(sc3.get("enabled")) and bool(str(sc3.get("token") or "").strip())
    wecom = config.get("wecom") or {}
    wecom_ready = bool(wecom.get("enabled")) and bool(str(wecom.get("corp_id") or "").strip()) and bool(
        str(wecom.get("corp_secret") or "").strip()
    )
    return bark_ready or sc3_ready or wecom_ready


def _time_window_bucket(local_now: datetime) -> str:
    hhmm = local_now.strftime("%H%M")
    if hhmm < "0925":
        return "preopen"
    if hhmm <= "1135":
        return "morning"
    if hhmm <= "1510":
        return "afternoon"
    return "afterhours"


class SessionHealthNotifier:
    """Send deduplicated Bark/SC3/WeCom alerts when direct sessions expire or recover."""

    def __init__(
        self,
        store: Any,
        dispatcher: Any,
        session_provider: SessionProvider | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
    ) -> None:
        self.store = store
        self.dispatcher = dispatcher
        self.session_provider = session_provider
        self.clock = clock
        self._lock = RLock()
        self._active_incident_dates: dict[str, str] = {}

    def _credential_stamp(self, role: str) -> str:
        if role == "trade_workspace":
            return "workspace"
        provider = self.session_provider
        if provider is None:
            return "unknown"
        updated_at: datetime | None = None
        try:
            status = provider.status(role)
            updated_at = getattr(status, "updated_at", None)
        except Exception:
            updated_at = None
        if updated_at is None:
            try:
                bundle = provider.get(role)
                updated_at = getattr(bundle, "updated_at", None) if bundle is not None else None
            except Exception:
                updated_at = None
        if updated_at is None:
            return "missing"
        return hashlib.sha256(updated_at.isoformat().encode("utf-8")).hexdigest()[:12]

    def _should_alert_error(self, role: str, error_code: str) -> bool:
        if role not in _ALERT_ROLES:
            return False
        normalized = str(error_code or "").strip().upper()
        if role == "trade_workspace":
            return normalized in _TRADE_WORKSPACE_ALERT_CODES
        if normalized in SESSION_ALERT_ERROR_CODES:
            return True
        if role == "core_metrics" and normalized == "DIRECT_PROTOCOL_HANDSHAKE_FAILED":
            provider = self.session_provider
            if provider is None:
                return False
            try:
                return provider.status(role).state == "ERROR"
            except Exception:
                return False
        return False

    def notify_error(self, role: str, error_code: str, *, stage: str = "直连请求") -> bool:
        normalized_code = str(error_code or "").strip().upper()
        if not self._should_alert_error(role, normalized_code):
            return False
        if self.store is None or self.dispatcher is None:
            return False
        local_now = self.clock().astimezone(SHANGHAI)
        date_str = local_now.strftime("%Y%m%d")
        window = _time_window_bucket(local_now)
        cred_stamp = self._credential_stamp(role)

        with self._lock:
            self._active_incident_dates[role] = date_str

        try:
            config = self.store.push_config(public=False)
        except Exception:
            logger.warning("session alert skipped: push config unavailable (%s)", role)
            return False
        if not _has_enabled_push_channel(config):
            return False

        event_key = f"session_alert:{role}:{normalized_code}:{cred_stamp}:{date_str}:{window}"
        if not self.store.claim_push_event(event_key):
            return False

        short_title = _ROLE_TITLES.get(role, role)
        title = (
            f"⚠️ {short_title} Cookie 已过期"
            if normalized_code == "DIRECT_SESSION_EXPIRED"
            else f"⚠️ {short_title}会话异常"
        )
        body_lines = [
            f"账号角色：{_ROLE_LABELS.get(role, role)}",
            f"异常代码：{normalized_code}",
            f"触发阶段：{stage}",
            f"检测时间：{local_now.strftime('%Y-%m-%d %H:%M:%S')}",
            f"处理建议：{_ROLE_IMPACTS.get(role, '请前往 THS 管理后台续签会话')}",
        ]
        try:
            result = self.dispatcher.send(config, title, "\n".join(body_lines))
            return bool(result.get("ok", True))
        except Exception as error:
            logger.warning(
                "session alert dispatch failed (%s/%s): %s",
                role,
                normalized_code,
                type(error).__name__,
            )
            return False

    def _has_today_incident(self, role: str, date_str: str) -> bool:
        with self._lock:
            if self._active_incident_dates.get(role) == date_str:
                return True
        checker = getattr(self.store, "has_push_event_matching", None)
        if callable(checker):
            try:
                return bool(checker(f"session_alert:{role}:", f":{date_str}:"))
            except Exception:
                return False
        return False

    def notify_recovered(self, role: str, *, stage: str = "会话续签/采集恢复") -> bool:
        if role not in _ALERT_ROLES or self.store is None or self.dispatcher is None:
            return False
        local_now = self.clock().astimezone(SHANGHAI)
        date_str = local_now.strftime("%Y%m%d")
        if not self._has_today_incident(role, date_str):
            return False

        provider = self.session_provider
        if provider is not None and role != "trade_workspace":
            try:
                if provider.status(role).state != "READY":
                    return False
            except Exception:
                return False

        cred_stamp = self._credential_stamp(role)
        if cred_stamp == "missing":
            return False

        try:
            config = self.store.push_config(public=False)
        except Exception:
            return False
        if not _has_enabled_push_channel(config):
            with self._lock:
                self._active_incident_dates.pop(role, None)
            return False

        event_key = f"session_recovered:{role}:{cred_stamp}:{date_str}"
        if not self.store.claim_push_event(event_key):
            with self._lock:
                self._active_incident_dates.pop(role, None)
            return False

        title = f"✅ {_ROLE_TITLES.get(role, role)}会话已恢复"
        body_lines = [
            f"账号角色：{_ROLE_LABELS.get(role, role)}",
            f"恢复方式：{stage}",
            f"恢复时间：{local_now.strftime('%Y-%m-%d %H:%M:%S')}",
            "当前状态：READY（已恢复正常采集）",
        ]
        try:
            result = self.dispatcher.send(config, title, "\n".join(body_lines))
            return bool(result.get("ok", True))
        except Exception as error:
            logger.warning(
                "session recovery dispatch failed (%s): %s",
                role,
                type(error).__name__,
            )
            return False
        finally:
            with self._lock:
                self._active_incident_dates.pop(role, None)

    def notify_error_background(
        self,
        role: str,
        error_code: str,
        *,
        stage: str = "直连请求",
    ) -> None:
        if not self._should_alert_error(role, str(error_code or "")):
            return
        Thread(
            target=self.notify_error,
            args=(role, error_code),
            kwargs={"stage": stage},
            daemon=True,
            name=f"ths-session-alert-{role}",
        ).start()

    def notify_recovered_background(
        self,
        role: str,
        *,
        stage: str = "管理后台续签",
    ) -> None:
        Thread(
            target=self.notify_recovered,
            args=(role,),
            kwargs={"stage": stage},
            daemon=True,
            name=f"ths-session-recovered-{role}",
        ).start()


class PreopenSessionProbe:
    """Actively verify THS direct sessions before continuous trading starts (08:50+ & 12:52)."""

    def __init__(
        self,
        *,
        is_trading_day: Callable[[date], bool],
        accounts: Any,
        fund_source: Any | None,
        core_source: Any | None,
        session_provider: SessionProvider | None,
        notifier: SessionHealthNotifier,
        trade_executor: Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
        default_symbol: str = "601872",
    ) -> None:
        self.is_trading_day = is_trading_day
        self.accounts = accounts
        self.fund_source = fund_source
        self.core_source = core_source
        self.session_provider = session_provider
        self.notifier = notifier
        self.trade_executor = trade_executor
        self.clock = clock
        self.default_symbol = default_symbol
        self._last_probed_date: str | None = None
        self._probed_windows: set[tuple[str, str]] = set()

    def _pick_fund_probe_symbol(self) -> str:
        if self.accounts is not None:
            lister = getattr(self.accounts, "list_watchlist_symbols", None)
            if callable(lister):
                try:
                    raw: Iterable[str] = lister()
                    for sym in raw:
                        clean = str(sym).strip()
                        if len(clean) == 6 and clean.isdigit() and not is_exchange_fund_symbol(clean):
                            return clean
                except Exception:
                    pass
        return self.default_symbol

    async def poll_once(self, *, force: bool = False) -> dict[str, str]:
        local_now = self.clock().astimezone(SHANGHAI)
        today_str = local_now.strftime("%Y%m%d")
        stage = "盘前 08:50 自动预检"
        if not force:
            if not self.is_trading_day(local_now.date()):
                return {}
            hhmm = local_now.strftime("%H%M")
            if "0850" <= hhmm <= "0924":
                window = "morning"
                stage = "盘前 08:50 自动预检"
            elif "1250" <= hhmm <= "1258":
                window = "midday"
                stage = "午休 12:52 自动预检"
            else:
                return {}
            if (today_str, window) in self._probed_windows:
                return {}
            if window == "morning" and self._last_probed_date == today_str:
                return {}
            self._probed_windows.add((today_str, window))
            if window == "morning":
                self._last_probed_date = today_str

        results: dict[str, str] = {}

        # 1. Check main_fund_flow HTTP session with a single lightweight query.
        if self.fund_source is not None:
            results["main_fund_flow"] = await self._probe_fund_flow(stage)

        # 2. Check core_metrics encrypted bundle / warm connection health.
        if self.core_source is not None or self.session_provider is not None:
            results["core_metrics"] = await self._probe_core_metrics(stage)

        # 3. Check broker trade workspace readiness when executor is configured.
        if self.trade_executor is not None:
            results["trade_workspace"] = await self._probe_trade_workspace(stage)

        return results

    async def _probe_trade_workspace(self, stage: str) -> str:
        probe_fn = getattr(self.trade_executor, "probe_broker_session", None)
        if not callable(probe_fn):
            probe_fn = getattr(self.trade_executor, "keep_warm", None)
        if not callable(probe_fn):
            return "SKIPPED"
        try:
            res = await asyncio.to_thread(probe_fn)
            if isinstance(res, dict) and res.get("ok") is False:
                code = str(res.get("error_code") or "TRADE_NAVIGATION_FAILED").strip().upper()
                if code in _TRADE_WORKSPACE_ALERT_CODES:
                    await asyncio.to_thread(
                        self.notifier.notify_error,
                        "trade_workspace",
                        code,
                        stage=stage,
                    )
                return code
            await asyncio.to_thread(
                self.notifier.notify_recovered,
                "trade_workspace",
                stage=stage,
            )
            return "READY"
        except Exception as error:
            code = str(getattr(error, "error_code", "") or "TRADE_NAVIGATION_FAILED").strip().upper()
            if code in _TRADE_WORKSPACE_ALERT_CODES:
                await asyncio.to_thread(
                    self.notifier.notify_error,
                    "trade_workspace",
                    code,
                    stage=stage,
                )
            return code

    async def _probe_fund_flow(self, stage: str) -> str:
        if self.session_provider is not None:
            try:
                status = self.session_provider.status("main_fund_flow")
                if status.state == "MISSING":
                    await asyncio.to_thread(
                        self.notifier.notify_error,
                        "main_fund_flow",
                        "DIRECT_SESSION_UNAVAILABLE",
                        stage=stage,
                    )
                    return "DIRECT_SESSION_UNAVAILABLE"
                if status.state == "ERROR" and status.error_code in SESSION_ALERT_ERROR_CODES:
                    await asyncio.to_thread(
                        self.notifier.notify_error,
                        "main_fund_flow",
                        status.error_code,
                        stage=stage,
                    )
                    return status.error_code
            except Exception:
                pass

        probe_symbol = self._pick_fund_probe_symbol()
        reader = getattr(self.fund_source, "read_direct", None)
        if not callable(reader):
            return "SKIPPED"
        try:
            if _supports_force_kwarg(reader):
                await asyncio.to_thread(reader, probe_symbol, force=True)
            else:
                await asyncio.to_thread(reader, probe_symbol)
            return "READY"
        except DirectRequestError as error:
            code = error.error_code
            if code in SESSION_ALERT_ERROR_CODES:
                await asyncio.to_thread(
                    self.notifier.notify_error,
                    "main_fund_flow",
                    code,
                    stage=stage,
                )
            return code
        except Exception:
            return "DIRECT_FUND_FLOW_REQUEST_FAILED"

    async def _probe_core_metrics(self, stage: str) -> str:
        if self.session_provider is not None:
            try:
                status = self.session_provider.status("core_metrics")
                if status.state == "MISSING":
                    await asyncio.to_thread(
                        self.notifier.notify_error,
                        "core_metrics",
                        "DIRECT_SESSION_UNAVAILABLE",
                        stage=stage,
                    )
                    return "DIRECT_SESSION_UNAVAILABLE"
                if status.state == "ERROR" and status.error_code:
                    await asyncio.to_thread(
                        self.notifier.notify_error,
                        "core_metrics",
                        status.error_code,
                        stage=stage,
                    )
                    return status.error_code
            except Exception:
                pass

        prewarmer = getattr(self.core_source, "prewarm", None)
        if callable(prewarmer):
            try:
                await asyncio.to_thread(prewarmer)
                return "READY"
            except DirectRequestError as error:
                code = error.error_code
                if code in SESSION_ALERT_ERROR_CODES or code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED":
                    await asyncio.to_thread(
                        self.notifier.notify_error,
                        "core_metrics",
                        code,
                        stage=stage,
                    )
                return code
            except Exception:
                return "DIRECT_PROTOCOL_HANDSHAKE_FAILED"
        return "READY"
