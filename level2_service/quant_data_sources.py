"""Unified registry, rule-dependency binding, health monitoring & live-trading guard for quant data sources."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
import re
from threading import RLock
import time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .brent_market import BRENT_SYMBOL, is_brent_market_open
from .parsed_values import SymbolLookup, is_exchange_fund_symbol
from .research_premium import PREMIUM_CODES, PREMIUM_NAMES
from .research_replay import detect_rule_required_packs, rule_uses_flow
from .v9_fof_crude import fetch_live_fof_crude_spot, load_dbo_daily_rows, load_merged_fx_cnh_series
from .v9_fof_t0_pulse import fetch_live_fof_t0_spot
from .v9_plus_engine import V9MacroDataStore, _MEMORY_MACRO_CACHE, get_live_spot_telemetry
from .v9_sp_oilgas_etf import fetch_live_sp_oilgas_etf_spot

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
NY_TZ = ZoneInfo("America/New_York")
_SIX_DIGIT_RE = re.compile(r"^[0-9]{6}\Z")
_DATE_RE = re.compile(r"^[0-9]{8}\Z")
_OIL_PEER_SYMBOLS: tuple[str, ...] = ("160723", "161129", "501018", "162411", "513350")
_T0_TURNAROUND_EXACT: frozenset[str] = frozenset(
    {"160723", "501018", "160416", "161129", "162411", "162719", "163208", "160216"}
)
_T0_TURNAROUND_PREFIXES: tuple[str, ...] = ("513", "5188", "1599", "1595")


def is_t0_turnaround_symbol(symbol: str) -> bool:
    """Return True for QDII/commodity/cross-border funds allowing T+0 same-day buy->sell."""
    sym = str(symbol or "").strip().upper()
    return sym in _T0_TURNAROUND_EXACT or sym.startswith(_T0_TURNAROUND_PREFIXES)


def resolve_rule_same_day_t0(
    symbol: str,
    position_sizing: Mapping[str, Any] | None = None,
) -> bool:
    """Resolve whether a symbol+sizing config permits same-day T+0 turnaround."""
    turnaround_raw = (position_sizing or {}).get("allow_same_day_turnaround", "auto")
    if isinstance(turnaround_raw, bool):
        return turnaround_raw
    text = str(turnaround_raw).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return is_t0_turnaround_symbol(symbol)

_ROLE_LABELS: dict[str, str] = {
    "primary": "主行情",
    "anchor": "核心锚点",
    "intraday_aux": "盘中联动",
    "macro_daily": "净值/日线锚",
    "flow": "L2资金流",
    "execution_aux": "盘口定价",
}

_RULE_SHORT_LABEL_OVERRIDES: dict[str, str] = {
    "preset-macd-pure-baseline": "160723·纯MACD基线",
    "preset-macd-flow-baseline": "160723·MACD大单",
    "ai-160723-v9-plus-aggressive": "160723·V9+FOF三油",
    "ai-160723-t0-multisource-v1": "160723·T+0双池多源",
    "ai-161129-v9-plus-aggressive": "161129·V9+双油沪SC",
    "ai-162411-v9-xop-aggressive": "162411·V9+XOP油气",
    "ai-501018-v9-plus-aggressive": "501018·V9+南方多油",
    "ai-513350-v9-sp-oilgas-etf": "513350·V9+标普油气T0",
}

_PROBE_LOCK = RLock()
_LAST_PROBE_TS: float = 0.0
_LAST_PROBE_LATENCY_MS: float | None = None

QUANT_DATA_SOURCE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "ashare_intraday_1m",
        "name": "A股/场内基金 1分钟实时分时与成交量",
        "category": "core_intraday",
        "category_label": "核心分时与资金流",
        "provider": "腾讯行情/同花顺 + SQLite ResearchMonitor",
        "endpoint_desc": "research_points(1m) · ResearchMonitor",
        "fetch_frequency": "盘中每 25 秒全市场同步 + 实盘秒级刷屏",
        "poll_interval_seconds": 25,
        "cache_ttl_seconds": 25,
        "trading_hours_cst": "交易日 09:30–11:30, 13:00–15:00",
        "max_intraday_stale_seconds": 180,
        "fallback_strategy": "主源断流超过 3 个交易分钟即自动触发盘中防误单熔断（禁止新开仓/追单）",
        "usage_tags": [
            {"label": "分时监控页 /finance/intraday", "kind": "module"},
            {"label": "量化回测引擎", "kind": "module"},
            {"label": "实盘信号触发", "kind": "module"},
            {"label": "160723 20m量比/5日均衡溢价/15日回撤", "kind": "feature"},
            {"label": "162411 恐慌放量量比", "kind": "feature"},
            {"label": "501018 15日回撤dd15逆势与2.20净值MACD锚", "kind": "feature"},
            {"label": "513350 T+0 VWAP偏离与2.0价格缩放MACD", "kind": "feature"},
        ],
    },
    {
        "id": "ashare_orderbook_l1",
        "name": "场内五档实时盘口与涨跌停笼子 (L1 五档)",
        "category": "core_intraday",
        "category_label": "核心分时与资金流",
        "provider": "腾讯实时盘口 qt.gtimg.cn + 同花顺直连兜底",
        "endpoint_desc": "qt.gtimg.cn 五档 + AutoTradeEngine._order_book_cache(3s)",
        "fetch_frequency": "实盘信号与挂单 3 秒热缓存 + 确认前强制刷盘口",
        "poll_interval_seconds": 3,
        "cache_ttl_seconds": 3,
        "trading_hours_cst": "交易日 09:15–11:30, 13:00–15:00",
        "max_intraday_stale_seconds": 30,
        "fallback_strategy": "盘口缺失或买1/卖1为空时禁止对手盘撮合，自动回退同花顺直连或拒绝无价裸挂",
        "usage_tags": [
            {"label": "实盘智能挂单定价", "kind": "feature"},
            {"label": "买1/卖1优价成交保护", "kind": "feature"},
            {"label": "涨跌停价格笼子", "kind": "feature"},
            {"label": "160723 100份底仓保留与五档优价", "kind": "feature"},
            {"label": "513350 T+0双向做T五档优价+100份底仓", "kind": "feature"},
            {"label": "资金暗盘实时报价", "kind": "module"},
        ],
    },
    {
        "id": "ashare_l2_fund_flow",
        "name": "A股 Level-2 暗盘/大单/散户分钟资金流",
        "category": "core_intraday",
        "category_label": "核心分时与资金流",
        "provider": "同花顺 L2 / lineFunds 接口",
        "endpoint_desc": "fund_flow_history_points & spService/lineFunds",
        "fetch_frequency": "盘中每 25 秒同步（场内基金自动回退 lineFunds 15s）",
        "poll_interval_seconds": 25,
        "cache_ttl_seconds": 25,
        "trading_hours_cst": "交易日 09:30–11:30, 13:00–15:00",
        "max_intraday_stale_seconds": 240,
        "fallback_strategy": "依赖大单/暗盘算子的规则在当日无资金流样本时自动拦截发单",
        "usage_tags": [
            {"label": "资金暗盘页 /finance/darkpool", "kind": "module"},
            {"label": "160723 MACD+大单净流共振基线", "kind": "feature"},
            {"label": "大单共振基线", "kind": "feature"},
            {"label": "开放网关: l2_darkpool_flow", "kind": "pack"},
        ],
    },
    {
        "id": "ice_brent_1m",
        "name": "ICE 布伦特原油连续 (BRN0Y / hf_OIL)",
        "category": "cross_market_crude",
        "category_label": "跨市场原油与期货",
        "provider": "新浪期货 GlobalService 1m + hf_OIL 现价 + OIL 日线",
        "endpoint_desc": "BrentMarketMonitor(20s) + hq.sinajs.cn/hf_OIL(8s) + oil_daily_2y(1h)",
        "fetch_frequency": "电子盘 20 秒落盘 1m + 实盘 8 秒现价缓存 + 1 小时日线缓存",
        "poll_interval_seconds": 20,
        "cache_ttl_seconds": 8,
        "trading_hours_cst": "周一 07:00/08:00 – 周六 05:00/06:00（每日 06:00–08:00 结算休市）",
        "max_intraday_stale_seconds": 300,
        "fallback_strategy": "1m 分钟流延迟时无缝切换新浪 hf_OIL 8 秒现价接管；主备双断时触发熔断",
        "usage_tags": [
            {"label": "油气溢价布油主图", "kind": "module"},
            {"label": "4 条 V9+ 策略共用", "kind": "feature"},
            {"label": "2 年油价分位牛熊锚", "kind": "feature"},
            {"label": "160723 12%混合锚+95%油价分位封顶", "kind": "feature"},
            {"label": "501018 87%高位多头放宽与双锚IOPV", "kind": "feature"},
            {"label": "513350 45% BRN日内价差+20日72%空头保护", "kind": "feature"},
            {"label": "开放网关: BRN0Y", "kind": "pack"},
        ],
    },
    {
        "id": "ashare_peer_lof_quote",
        "name": "同业油气 LOF 实时联动行情 (160723/161129/501018/162411/513350)",
        "category": "cross_market_crude",
        "category_label": "跨市场原油与期货",
        "provider": "本地 research_points + 新浪 A股现价 hq.sinajs.cn",
        "endpoint_desc": "research_points peer + sh501018/sz160723/sz161129/sz162411(8s)",
        "fetch_frequency": "盘中 25 秒分钟落盘 + 8 秒新浪现价实时校验",
        "poll_interval_seconds": 25,
        "cache_ttl_seconds": 8,
        "trading_hours_cst": "交易日 09:30–11:30, 13:00–15:00",
        "max_intraday_stale_seconds": 180,
        "fallback_strategy": "同业分钟条缺失时自动切换新浪 8 秒现价，双通道全断时阻断同业价差联动",
        "usage_tags": [
            {"label": "跨品种折溢价联动", "kind": "feature"},
            {"label": "160723 参照501018+161129领涨与3m防飞刀", "kind": "feature"},
            {"label": "160723 T+0双池 三同业(501018/161129/162411)联动", "kind": "feature"},
            {"label": "160723/161129 双向同业强弱联动", "kind": "feature"},
            {"label": "501018 参照160723实时涨幅差(peer_gap)", "kind": "feature"},
            {"label": "513350 参照162411日内价差(peer_gap_intra)与3m防飞刀", "kind": "feature"},
            {"label": "开放网关: uso_bno_sc_peer", "kind": "pack"},
        ],
    },
    {
        "id": "ine_sc0_futures",
        "name": "上期能源沪原油连续 (SC0 / nf_SC0)",
        "category": "cross_market_crude",
        "category_label": "跨市场原油与期货",
        "provider": "新浪内盘期货 nf_SC0 + InnerFuturesNewService",
        "endpoint_desc": "nf_SC0(8s) + sc0_min_series(5m) + sc0_daily_all(1h)",
        "fetch_frequency": "实盘 8 秒现价 + 5 分钟 K 线缓存 + 1 小时日线缓存",
        "poll_interval_seconds": 8,
        "cache_ttl_seconds": 300,
        "trading_hours_cst": "09:00–10:15, 10:30–11:30, 13:30–15:00, 夜盘 21:00–02:30",
        "max_intraday_stale_seconds": 360,
        "fallback_strategy": "10:15–10:30 及午休因果沿用休前最新价；开盘段主备全缺时降级/熔断",
        "usage_tags": [
            {"label": "内外盘原油共振", "kind": "feature"},
            {"label": "160723 沪SC0防飞刀+14:53逆势过夜", "kind": "feature"},
            {"label": "161129 双油+沪SC0内外盘共振", "kind": "feature"},
            {"label": "513350 沪SC0防飞刀+逆势V底抄底", "kind": "feature"},
            {"label": "开放网关: uso_bno_sc_peer", "kind": "pack"},
        ],
    },
    {
        "id": "us_oil_etf_uso_bno_dbo",
        "name": "美股原油 ETF 篮子 (USO / BNO / DBO)",
        "category": "cross_market_crude",
        "category_label": "跨市场原油与期货",
        "provider": "新浪美股 US_MinKService + Yahoo v8 + gb_* 收盘校验",
        "endpoint_desc": "uso/bno/dbo_daily_2y(1h) + gb_uso/gb_bno/gb_dbo(8s)",
        "fetch_frequency": "1 小时日线缓存 + 8 秒常规收盘校验合并（04:00/05:00 CST 定格）",
        "poll_interval_seconds": 3600,
        "cache_ttl_seconds": 3600,
        "trading_hours_cst": "美东 09:30–16:00（A股盘前已完成收盘定格）",
        "max_intraday_stale_seconds": 86400,
        "fallback_strategy": "新浪失败自动切换 Yahoo Chart v8 + SQLite 持久化缓存兜底，完全无日线时熔断",
        "usage_tags": [
            {"label": "FOF 三油/双油净值锚", "kind": "feature"},
            {"label": "160723 35%USO+35%BNO+30%DBO三油锚", "kind": "feature"},
            {"label": "161129 50%USO+50%BNO双油净值锚", "kind": "feature"},
            {"label": "501018 55%USO+45%BNO双锚IOPV与去泡沫校验", "kind": "feature"},
            {"label": "暴跌逆势监控", "kind": "feature"},
            {"label": "开放网关: uso_bno_sc_peer", "kind": "pack"},
        ],
    },
    {
        "id": "us_ep_xop_crack_macro",
        "name": "美股油气开采与宽基 ETF 篮子 (XOP / XLE / SPY / IWM + 盘后)",
        "category": "cross_market_crude",
        "category_label": "跨市场原油与期货",
        "provider": "新浪美股 US_MinKService + Yahoo v8 + gb_* 收盘及盘后",
        "endpoint_desc": "xop/xle/spy/iwm_daily_2y(1h) + gb_xop/xle/iwm/spy 收盘及盘后(8s)",
        "fetch_frequency": "1 小时美股日线缓存 + 8 秒常规收盘与盘后扩展报价合并",
        "poll_interval_seconds": 3600,
        "cache_ttl_seconds": 3600,
        "trading_hours_cst": "美东 09:30–16:00（A股盘前定格，含美东盘前/盘后扩展）",
        "max_intraday_stale_seconds": 86400,
        "fallback_strategy": "新浪失败自动切换 Yahoo Chart v8 + gb_* 实时收盘合并；XOP/XLE/SPY/IWM 缺日或 SPY 滞后时降级提示",
        "usage_tags": [
            {"label": "油气溢价面板 (159518/513350代理)", "kind": "module"},
            {"label": "162411 XOP 隔夜领涨与风险滤波", "kind": "feature"},
            {"label": "160723 T+0双池 XOP/XLE/SPY/IWM隔夜早盘滤波", "kind": "feature"},
            {"label": "501018 XOP美油股隔夜共振大跌防御", "kind": "feature"},
            {"label": "513350 XOP全复制(0.985)主锚与2Y分位", "kind": "feature"},
            {"label": "开放网关: xop_us_ep", "kind": "pack"},
        ],
    },
    {
        "id": "cme_energy_macro_futures",
        "name": "CME 美盘能源与股指连续期货 (WTI CL / 取暖油裂解 HO / 天然气 NG / 标普 ES)",
        "category": "cross_market_crude",
        "category_label": "跨市场原油与期货",
        "provider": "新浪期货 GlobalService 1m + GlobalFuturesService 日线 + hf_* 8s 现价",
        "endpoint_desc": "cl/ho/ng_daily_2y(1h) + fut_1m_{cl,ho,ng,es}(5m) + hf_CL/HO/NG/ES(8s)",
        "fetch_frequency": "A股盘中 8 秒现价 + 5 分钟 1m 序列 + 1 小时期货日线缓存",
        "poll_interval_seconds": 8,
        "cache_ttl_seconds": 300,
        "trading_hours_cst": "CME Globex 近 23h 连续电子盘（美东 17:00–18:00 维护休市，A股盘中实时交易）",
        "max_intraday_stale_seconds": 600,
        "fallback_strategy": "1m 序列延迟时由 8 秒 hf_* 现价接管合成领先；遇美股休市次日超过 12h 基准窗则提示节假日退化由 BRN0Y 主导",
        "usage_tags": [
            {"label": "3-2-1 裂解价差 (CL/HO)", "kind": "feature"},
            {"label": "亚盘实时领先合成 (CL/NG/ES)", "kind": "feature"},
            {"label": "160723 WTI(CL)隔夜+8s现价与HO 3日裂解", "kind": "feature"},
            {"label": "501018 WTI(CL)隔夜动量与超跌企稳确认", "kind": "feature"},
            {"label": "513350 CME 23h期货(55% sp_xop_intra日内锚)", "kind": "feature"},
            {"label": "160723/162411/501018 共用", "kind": "feature"},
            {"label": "开放网关: xop_us_ep", "kind": "pack"},
        ],
    },
    {
        "id": "fx_usdcny_usdcnh",
        "name": "在岸/离岸人民币汇率 (USD/CNY · USD/CNH)",
        "category": "fx_and_nav",
        "category_label": "汇率与官方净值",
        "provider": "新浪外汇 fx_susdcny / fx_susdcnh + USDCNY 实时",
        "endpoint_desc": "USDCNY & fx_susdcnh(8s) + fx_usdcny/cnh_series(10m)",
        "fetch_frequency": "盘中 8 秒实时汇率 + 10 分钟分时/日线序列化缓存",
        "poll_interval_seconds": 8,
        "cache_ttl_seconds": 600,
        "trading_hours_cst": "在岸 09:30–23:30 / 离岸 24 小时（周末休市）",
        "max_intraday_stale_seconds": 900,
        "fallback_strategy": "实时报价瞬断自动回退 10 分钟序列末值，主备全缺则暂停汇调 IOPV 交易",
        "usage_tags": [
            {"label": "汇调 IOPV 实时折算", "kind": "feature"},
            {"label": "160723/162411 24h离岸CNH覆盖在岸CNY", "kind": "feature"},
            {"label": "513350 离岸USD/CNH优先折算汇调IOPV", "kind": "feature"},
            {"label": "4 条 V9+ 策略共用", "kind": "feature"},
            {"label": "油气溢价汇率锚", "kind": "module"},
            {"label": "开放网关: iopv_fx", "kind": "pack"},
        ],
    },
    {
        "id": "eastmoney_fund_nav",
        "name": "天天基金官方历史净值 (NAV)",
        "category": "fx_and_nav",
        "category_label": "汇率与官方净值",
        "provider": "东方财富天天基金 pingzhongdata/{code}.js",
        "endpoint_desc": "quant_macro_cache nav_{symbol}(1h)",
        "fetch_frequency": "1 小时内存+SQLite 双层缓存（每日盘前已完成 T-2/T-1 官方定格）",
        "poll_interval_seconds": 3600,
        "cache_ttl_seconds": 3600,
        "trading_hours_cst": "每日净值披露后静态定格，盘中无盘中跳变",
        "max_intraday_stale_seconds": 86400,
        "fallback_strategy": "网络波动自动读取 SQLite 持久化历史净值；完全缺失官方净值时阻断 IOPV 计算",
        "usage_tags": [
            {"label": "QDII T-2 官方净值重锚定", "kind": "feature"},
            {"label": "501018/160723 FOF T-2 官方净值底座", "kind": "feature"},
            {"label": "160723 T-2净值→FOF三油重锚T-1", "kind": "feature"},
            {"label": "513350 标普油气ETF T-2净值→XOP重锚T-1", "kind": "feature"},
            {"label": "油气溢价净值底座", "kind": "module"},
            {"label": "开放网关: iopv_fx", "kind": "pack"},
        ],
    },
    {
        "id": "qdii_premium_panel_feed",
        "name": "QDII/油气基金实时估值面板源 (HaoETF / 东财 · 9只油气基)",
        "category": "fx_and_nav",
        "category_label": "汇率与官方净值",
        "provider": "HaoETF /qdii + 东财 push2 + Yahoo 代理估价 (PremiumMonitor)",
        "endpoint_desc": "research_settings.premium_snapshot (15s)",
        "fetch_frequency": "盘中每 15 秒抓取 9 只油气基金实时估值与溢价率（盘外维持最近快照）",
        "poll_interval_seconds": 15,
        "cache_ttl_seconds": 15,
        "trading_hours_cst": "交易日 09:10–15:05 定时估值 + 固定时段 Bark/企微推送",
        "max_intraday_stale_seconds": 120,
        "fallback_strategy": "单只基金估值失败时标记 stale 并保留上一次有效溢价对照，不阻塞 V9+ 自研因果 IOPV 主链路",
        "usage_tags": [
            {"label": "油气溢价 /finance/premium", "kind": "module"},
            {"label": "覆盖 501018/160723/161129/162411 等9只油气基", "kind": "feature"},
            {"label": "Bark/企微定时溢价推送", "kind": "feature"},
            {"label": "实盘 lof_premium/combo 模式", "kind": "feature"},
            {"label": "与 V9+ 自研 IOPV 对照", "kind": "feature"},
        ],
    },
    {
        "id": "ashare_etf_redemption",
        "name": "场内 ETF T-1 申赎净流入与份额规模",
        "category": "fx_and_nav",
        "category_label": "汇率与官方净值",
        "provider": "同花顺 ETF 资金流 etf-fund-flow.db",
        "endpoint_desc": "etf_fund_flow_daily（严格 T-1 无前瞻）",
        "fetch_frequency": "按日披露（严格 T-1 收盘更新）+ 策略评估按需缓存",
        "poll_interval_seconds": 3600,
        "cache_ttl_seconds": 3600,
        "trading_hours_cst": "交易日盘后披露前一日份额变动",
        "max_intraday_stale_seconds": 86400,
        "fallback_strategy": "仅影响使用 etf_net_flow 过滤算子的策略；缺数时该过滤条件置为未满足",
        "usage_tags": [
            {"label": "资金暗盘 ETF申赎页签", "kind": "module"},
            {"label": "开放网关: etf_redemption_flow", "kind": "pack"},
        ],
    },
)

QUANT_DATA_SOURCE_BY_ID: dict[str, dict[str, Any]] = {
    item["id"]: dict(item) for item in QUANT_DATA_SOURCE_CATALOG
}
VALID_QUANT_DATA_SOURCE_IDS: frozenset[str] = frozenset(QUANT_DATA_SOURCE_BY_ID.keys())


def _is_quant_offline() -> bool:
    return os.environ.get("THS_QUANT_OFFLINE", "").strip().lower() in {"1", "true", "yes"}


def _parse_iso_dt(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI_TZ)
    return dt.astimezone(SHANGHAI_TZ)


def _trading_minute_index(hhmm: Any) -> int | None:
    text = str(hhmm or "").strip()
    m = re.match(r"^(\d{2}):(\d{2})", text)
    if not m:
        return None
    mins = int(m.group(1)) * 60 + int(m.group(2))
    if mins < 9 * 60 + 30:
        return None
    if mins <= 11 * 60 + 30:
        return mins - (9 * 60 + 30)
    if mins < 13 * 60:
        return 120
    if mins <= 15 * 60:
        return 120 + (mins - 13 * 60)
    return 240


def _trading_minutes_lag(latest_hhmm: Any, now_hhmm: Any) -> int | None:
    latest_idx = _trading_minute_index(latest_hhmm)
    now_idx = _trading_minute_index(now_hhmm)
    if latest_idx is None or now_idx is None:
        return None
    return max(0, now_idx - latest_idx)


def _wall_minutes_lag(latest_hhmm: Any, now_hhmm: Any) -> int | None:
    m1 = re.match(r"^(\d{2}):(\d{2})", str(latest_hhmm or "").strip())
    m2 = re.match(r"^(\d{2}):(\d{2})", str(now_hhmm or "").strip())
    if not m1 or not m2:
        return None
    t1 = int(m1.group(1)) * 60 + int(m1.group(2))
    t2 = int(m2.group(1)) * 60 + int(m2.group(2))
    return t2 - t1


def _clean_symbol_list(symbols: Any) -> list[str]:
    if not isinstance(symbols, Sequence) or isinstance(symbols, (str, bytes)):
        return []
    out: list[str] = []
    for s in symbols:
        clean = str(s or "").strip().upper()
        if clean and clean not in out:
            out.append(clean)
    return out


def _format_rule_short_label(rule_id: str, rule_name: str, target_symbol: str) -> str:
    rid = str(rule_id or "").strip()
    if rid in _RULE_SHORT_LABEL_OVERRIDES:
        return _RULE_SHORT_LABEL_OVERRIDES[rid]
    sym = str(target_symbol or "").strip().upper() or "QUANT"
    cleaned = str(rule_name or "").strip()
    for prefix in (f"{sym}专享", sym, "V9+ 进取版", "V9+"):
        cleaned = cleaned.replace(prefix, "")
    cleaned = cleaned.strip(" ·-_()（）")
    if len(cleaned) > 10:
        cleaned = cleaned[:10].rstrip(" ·-_")
    return f"{sym}·{cleaned}" if cleaned else (f"{sym}·{rid[:10]}" if rid else sym)


def _build_source_entry(
    source_id: str,
    *,
    role: str,
    required_in_session: bool,
    symbols: Sequence[str] = (),
    auto_derived: bool = True,
    note: str = "",
) -> dict[str, Any]:
    meta = QUANT_DATA_SOURCE_BY_ID[source_id]
    clean_syms = _clean_symbol_list(list(symbols))
    return {
        "id": source_id,
        "name": meta["name"],
        "role": role,
        "role_label": _ROLE_LABELS.get(role, role),
        "required_in_session": bool(required_in_session),
        "symbols": clean_syms,
        "fetch_frequency": meta["fetch_frequency"],
        "fallback_strategy": meta["fallback_strategy"],
        "auto_derived": bool(auto_derived),
        "note": note.strip() or meta["endpoint_desc"],
    }


def _collect_all_rule_containers(rule_spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = []
    for key in ("buy_rule", "sell_rule"):
        item = rule_spec.get(key)
        if isinstance(item, Mapping):
            containers.append(item)
    ps = rule_spec.get("position_sizing")
    if isinstance(ps, Mapping):
        for tiers_key in ("conviction_tiers", "sell_conviction_tiers"):
            tiers = ps.get(tiers_key)
            if isinstance(tiers, Sequence) and not isinstance(tiers, (str, bytes)):
                for tier in tiers:
                    if isinstance(tier, Mapping):
                        containers.append(tier)
    return containers


def derive_rule_data_sources(rule_spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive canonical required data sources automatically from rule symbols, AST & sizing profile."""
    raw_symbols = _clean_symbol_list(rule_spec.get("symbols") or [])
    target_sym = str(rule_spec.get("target_symbol") or "").strip().upper()
    if _SIX_DIGIT_RE.fullmatch(target_sym) and target_sym not in raw_symbols:
        raw_symbols.insert(0, target_sym)
    primary_six = [s for s in raw_symbols if _SIX_DIGIT_RE.fullmatch(s)]
    if not primary_six:
        primary_six = ["160723"]
    main_sym = primary_six[0]

    containers = _collect_all_rule_containers(rule_spec)
    ps = rule_spec.get("position_sizing") if isinstance(rule_spec.get("position_sizing"), Mapping) else {}
    profile = str(ps.get("engine_profile") or "standard").strip().lower()
    anchor = str(ps.get("cross_market_anchor") or ("brent" if profile != "standard" else "")).strip().lower()
    v9_tuning = ps.get("v9_tuning") if isinstance(ps.get("v9_tuning"), Mapping) else {}
    req_packs = detect_rule_required_packs(containers, ps)

    uses_brent_cond = False
    uses_code_filter = False
    for container in containers:
        for cond in container.get("conditions") or []:
            if not isinstance(cond, Mapping):
                continue
            ctype = str(cond.get("type") or "").strip()
            if ctype.startswith("brent_"):
                uses_brent_cond = True
            elif ctype == "code_filter":
                uses_code_filter = True
    del uses_code_filter

    by_id: dict[str, dict[str, Any]] = {}

    def _merge(
        src_id: str,
        *,
        role: str,
        required_in_session: bool,
        symbols: Sequence[str] = (),
        note: str = "",
    ) -> None:
        existing = by_id.get(src_id)
        if existing is None:
            by_id[src_id] = _build_source_entry(
                src_id,
                role=role,
                required_in_session=required_in_session,
                symbols=symbols,
                auto_derived=True,
                note=note,
            )
            return
        existing["required_in_session"] = bool(existing["required_in_session"] or required_in_session)
        merged_syms = _clean_symbol_list([*existing.get("symbols", []), *symbols])
        existing["symbols"] = merged_syms
        if role == "anchor" and existing.get("role") not in {"primary", "anchor"}:
            existing["role"] = "anchor"
            existing["role_label"] = _ROLE_LABELS.get("anchor", "anchor")
        if note and note not in existing["note"]:
            existing["note"] = f"{existing['note']}；{note}" if existing["note"] else note

    is_multi_oil = bool(v9_tuning.get("multi_oil_plunge_mode"))
    min_retained = int(ps.get("min_retained_shares") or 0)
    _merge(
        "ashare_intraday_1m",
        role="primary",
        required_in_session=True,
        symbols=primary_six,
        note=(
            "标的 1m 分钟线、均价与恐慌放量量比(vol_ratio)主数据源"
            if anchor == "xop_us_ep"
            else (
                "标的 1m 分钟线、VWAP均价、20m放量量比(vol_ratio)、5日均衡溢价与15日回撤(dd15)主数据源"
                if anchor == "fof_crude_core"
                else (
                    "160723 1m/VWAP双池T+0、20m放量量比(vol_ratio)、5日均衡溢价与15日回撤(dd15)主数据源"
                    if anchor == "fof_t0_multisource"
                    else (
                        "标的 1m 分钟线、VWAP偏离(vdev)、5日中位配额折溢价与2.0价格缩放MACD主数据源"
                        if anchor == "sp_oilgas_etf"
                        else (
                            "标的 1m 分钟线、2.20净值MACD锚定与15日高点回撤(dd15≤-11%逆势)"
                            if is_multi_oil
                            else "标的 1m 分钟线与均价主数据源"
                        )
                    )
                )
            )
        ),
    )

    _merge(
        "ashare_orderbook_l1",
        role="execution_aux",
        required_in_session=False,
        symbols=primary_six,
        note=(
            f"实盘五档买一/卖一优价定价、涨跌停价格笼子与{min_retained}份底仓防卖空校验（离线回测不阻塞）"
            if min_retained > 0
            else "实盘五档买一/卖一优价定价与涨跌停价格笼子（离线回测不阻塞）"
        ),
    )

    is_v9 = profile in {"v9_plus_aggressive", "v9_plus_balanced"}
    if is_v9 or uses_brent_cond or (BRENT_SYMBOL in raw_symbols):
        _merge(
            "ice_brent_1m",
            role="anchor",
            required_in_session=True,
            symbols=[BRENT_SYMBOL],
            note=(
                "BRN0Y 1m/hf_OIL 8s现价补尾、12%混合净值锚与布伦特2年分位(≥95%多头封顶)"
                if anchor == "fof_crude_core"
                else (
                    "BRN0Y 1m/hf_OIL 8s现价补尾、12%混合净值锚与布伦特2年分位(≥90%转0%纯T+0)"
                    if anchor == "fof_t0_multisource"
                    else (
                        "BRN0Y 1m/hf_OIL 8s现价(45% sp_brn价差锚)与布伦特2年分位(20日72%空头保护)"
                        if anchor == "sp_oilgas_etf"
                        else (
                            "BRN0Y 1m/8s现价双锚与布伦特2年分位(多头上限放宽至87%)"
                            if is_multi_oil
                            else "BRN0Y 1m/现价与布伦特 2 年分位日线锚"
                        )
                    )
                )
            ),
        )

    if is_v9 or "iopv_fx" in req_packs:
        _merge(
            "fx_usdcny_usdcnh",
            role="intraday_aux",
            required_in_session=True,
            symbols=["USDCNY", "USDCNH"] if anchor in {"fof_crude_core", "fof_t0_multisource", "xop_us_ep", "sp_oilgas_etf"} else ["USDCNY"],
            note=(
                "24h离岸USD/CNH优先覆盖在岸USD/CNY（10m序列+8s现价）实时折算汇调IOPV"
                if anchor in {"fof_crude_core", "fof_t0_multisource", "xop_us_ep", "sp_oilgas_etf"}
                else "实时汇率用于计算汇调 IOPV 与折溢价率"
            ),
        )
        _merge(
            "eastmoney_fund_nav",
            role="macro_daily",
            required_in_session=True,
            symbols=[*primary_six, "162411"] if anchor == "sp_oilgas_etf" else primary_six,
            note=(
                "嘉实原油 FOF-LOF 天天基金 T-2 官方净值底座（配合 35%USO+35%BNO+30%DBO 重锚 T-1）"
                if anchor in {"fof_crude_core", "fof_t0_multisource"}
                else (
                    "标普油气 ETF 天天基金 T-2 官方净值底座（配合 0.985×XOP 全复制重锚 T-1）"
                    if anchor == "sp_oilgas_etf"
                    else (
                        "南方原油 FOF-LOF 天天基金 T-2 官方净值底座（配合 USO+BNO 桥接 T-1）"
                        if is_multi_oil
                        else "天天基金官方历史净值 T-2 锚点"
                    )
                )
            ),
        )

    if anchor in {"xop_us_ep", "sp_oilgas_etf", "fof_t0_multisource"} or "xop_us_ep" in req_packs:
        _merge(
            "us_ep_xop_crack_macro",
            role="anchor",
            required_in_session=True,
            symbols=["XOP", "XLE", "SPY", "IWM"],
            note=(
                "标普油气 XOP 全复制(0.985) T-1重锚、XOP/XLE/SPY/IWM隔夜动量与XOP 2Y分位主锚"
                if anchor == "sp_oilgas_etf"
                else (
                    "XOP/XLE/SPY/IWM 美股隔夜动量与早盘XOP弱势反T高抛滤波"
                    if anchor == "fof_t0_multisource"
                    else "XOP/XLE/SPY/IWM 美股油气与宽基 ETF 隔夜锚（含盘后扩展）"
                )
            ),
        )
        _merge(
            "cme_energy_macro_futures",
            role="intraday_aux" if anchor == "fof_t0_multisource" else "anchor",
            required_in_session=True,
            symbols=["CL", "HO"] if anchor == "fof_t0_multisource" else ["CL", "HO", "NG", "ES"],
            note=(
                "CME CL/HO裂解/NG/ES 23h期货1m+8s现价实时合成亚盘XOP日内基准(55% sp_xop_intra)"
                if anchor == "sp_oilgas_etf"
                else (
                    "CME WTI(CL) 1m实时动量(cl_intra)+hf_CL/HO 8s现价与取暖油HO 3日裂解价差联动"
                    if anchor == "fof_t0_multisource"
                    else "WTI CL / 取暖油 HO / 天然气 NG / 标普 ES 23h 亚盘领先合成与裂解价差"
                )
            ),
        )

    if anchor in {"uso_bno_sc_peer", "fof_crude_core", "fof_t0_multisource"} or "uso_bno_sc_peer" in req_packs:
        _merge(
            "us_oil_etf_uso_bno_dbo",
            role="anchor",
            required_in_session=True,
            symbols=["USO", "BNO", "DBO"] if anchor in {"fof_crude_core", "fof_t0_multisource"} else ["USO", "BNO"],
            note=(
                "35%USO+35%BNO+30%DBO 美股三油ETF日线与gb_* 8s收盘校验（88%权重重锚T-1 FOF IOPV）"
                if anchor in {"fof_crude_core", "fof_t0_multisource"}
                else "USO+BNO(+DBO) 美股原油 ETF 双油/FOF 净值锚"
            ),
        )
    if anchor in {"uso_bno_sc_peer", "fof_crude_core", "fof_t0_multisource", "sp_oilgas_etf"} or "uso_bno_sc_peer" in req_packs:
        _merge(
            "ine_sc0_futures",
            role="anchor",
            required_in_session=True,
            symbols=["SC0"],
            note=(
                "沪原油 SC0 (nf_SC0 8s/5m/日线) 日内急跌防飞刀与14:53逆势重仓过夜锚"
                if anchor == "fof_crude_core"
                else (
                    "沪原油 SC0 (nf_SC0 8s/5m/日线) 防飞刀滤波、三同业共振补涨T+0与14:53逆势过夜"
                    if anchor == "fof_t0_multisource"
                    else (
                        "沪原油 SC0 (nf_SC0 8s/5m/日线) 防飞刀滤波与逆势V底抄底锚"
                        if anchor == "sp_oilgas_etf"
                        else "沪原油 SC0 内外盘日内与隔夜共振"
                    )
                )
            ),
        )
        peer_targets = (
            ["162411"]
            if anchor == "sp_oilgas_etf"
            else (
                ["501018", "161129", "162411"]
                if (anchor == "fof_t0_multisource" and main_sym == "160723")
                else (
                    ["501018", "161129"]
                    if main_sym == "160723"
                    else (["160723", "501018"] if main_sym == "161129" else ["160723"])
                )
            )
        )
        _merge(
            "ashare_peer_lof_quote",
            role="intraday_aux",
            required_in_session=True,
            symbols=peer_targets,
            note=(
                "参照 501018(南方原油)+161129(易方达原油) 1m/8s 领涨价差(peer_lead)与3m急跌防接飞刀"
                if anchor == "fof_crude_core"
                else (
                    "参照 501018+161129+162411 三同业 1m/8s 领涨价差、162411 3m动量共振补涨T+0与防飞刀"
                    if anchor == "fof_t0_multisource"
                    else (
                        "同业标普油气 162411 1m/8s 实时涨幅差(peer_gap_intra)双向T+0增强与3m急跌防飞刀"
                        if anchor == "sp_oilgas_etf"
                        else "同业油气 LOF 相对强弱与折溢价差"
                    )
                )
            ),
        )

    if anchor == "fof_crude_core":
        _merge(
            "cme_energy_macro_futures",
            role="intraday_aux",
            required_in_session=True,
            symbols=["CL", "HO"],
            note="160723 FOF 辅助 WTI(CL)隔夜/hf_CL 8s现价与取暖油(HO) 3日3-2-1裂解价差多空修正",
        )

    peer_sym = str(v9_tuning.get("peer_symbol") or "").strip().upper()
    if _SIX_DIGIT_RE.fullmatch(peer_sym):
        _merge(
            "ashare_peer_lof_quote",
            role="intraday_aux",
            required_in_session=True,
            symbols=[peer_sym],
            note=(
                f"参照同业龙头 {peer_sym} 1m/8s 实时涨幅差(peer_gap)动态增强低吸高抛"
                if is_multi_oil
                else f"同业参照标的 {peer_sym} 实时联动行情"
            ),
        )

    if is_multi_oil:
        _merge(
            "us_oil_etf_uso_bno_dbo",
            role="anchor",
            required_in_session=True,
            symbols=["USO", "BNO"],
            note="55%USO+45%BNO 校准 T-1 双锚溢价(prem_etf)、隔夜动量与暴跌去泡沫校验",
        )
        _merge(
            "us_ep_xop_crack_macro",
            role="intraday_aux",
            required_in_session=True,
            symbols=["XOP"],
            note="美股油气(XOP)隔夜共振大跌降仓防御(≤-2.0%)与企稳逆势确认",
        )
        _merge(
            "cme_energy_macro_futures",
            role="anchor",
            required_in_session=True,
            symbols=["CL"],
            note="WTI 原油(CL)隔夜动量(macro_oil_ov)与超跌企稳逆势确认",
        )

    if rule_uses_flow(list(containers)) or "l2_darkpool_flow" in req_packs:
        _merge(
            "ashare_l2_fund_flow",
            role="flow",
            required_in_session=True,
            symbols=primary_six,
            note="规则含资金流/大单条件，盘中依赖分钟资金流样本",
        )

    if "etf_redemption_flow" in req_packs:
        _merge(
            "ashare_etf_redemption",
            role="macro_daily",
            required_in_session=False,
            symbols=primary_six,
            note="严格 T-1 场内 ETF 申赎净流入条件",
        )

    order_map = {item["id"]: idx for idx, item in enumerate(QUANT_DATA_SOURCE_CATALOG)}
    return sorted(by_id.values(), key=lambda x: order_map.get(x["id"], 999))


def derive_rule_usage_tags(rule_spec: Mapping[str, Any]) -> list[dict[str, str]]:
    """Derive concise strategy-level feature/data usage tags for UI chips & audits."""
    ps = rule_spec.get("position_sizing") if isinstance(rule_spec.get("position_sizing"), Mapping) else {}
    profile = str(ps.get("engine_profile") or "standard").strip().lower()
    raw_anchor = str(ps.get("cross_market_anchor") or ("brent" if profile != "standard" else "")).strip().lower()
    v9_tuning = ps.get("v9_tuning") if isinstance(ps.get("v9_tuning"), Mapping) else {}
    containers = _collect_all_rule_containers(rule_spec)
    tags: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(label: str, kind: str = "feature") -> None:
        clean = label.strip()
        if clean and clean not in seen:
            seen.add(clean)
            tags.append({"label": clean, "kind": kind})

    if raw_anchor == "fof_crude_core":
        _add("35%USO+35%BNO+30%DBO三油锚", "feature")
        _add("WTI(CL)+HO裂解+沪SC0共振", "feature")
        _add("参照501018/161129领涨防飞刀", "feature")
        _add("USDCNY/H双汇率IOPV", "feature")
        _add("10合1新浪8s秒级补尾", "feature")
    elif raw_anchor == "fof_t0_multisource":
        _add("85%底仓+15%专属T+0双池资金", "feature")
        _add("35%USO+35%BNO+30%DBO三油锚", "feature")
        _add("XOP/XLE/SPY/IWM+CL/HO裂解", "feature")
        _add("501018/161129/162411三同业+沪SC0共振", "feature")
        _add("USDCNY/H双汇率+13合1新浪8s补尾", "feature")
    elif raw_anchor == "uso_bno_sc_peer":
        _add("55%USO+45%BNO+沪SC0共振", "feature")
        _add("同业160723/501018强弱对比", "feature")
    elif raw_anchor == "xop_us_ep":
        _add("XOP/XLE/SPY/IWM隔夜锚", "feature")
        _add("CME 23h期货CL/HO/NG/ES裂解", "feature")
    elif raw_anchor == "sp_oilgas_etf":
        _add("XOP全复制(0.985)+5d中位配额脱敏", "feature")
        _add("T+0日内双向做T+两档正T加仓", "feature")
        _add("55%XOP日内+45%BRN混合价差", "feature")
        _add("CME裂解+沪SC0+162411同业(peer_gap_intra)", "feature")
        _add("13合1新浪8s秒级补尾", "feature")
    elif profile != "standard":
        _add("BRN0Y布伦特+2Y分位锚", "feature")

    if bool(v9_tuning.get("multi_oil_plunge_mode")):
        _add("55%USO+45%BNO双锚IOPV", "feature")
        _add("WTI(CL)+XOP隔夜共振防线", "feature")
        dd15 = v9_tuning.get("plunge_dd15_pct")
        _add(f"15日回撤dd15≤{float(dd15):g}%逆势" if dd15 is not None else "15日回撤dd15逆势", "feature")

    peer_sym = str(v9_tuning.get("peer_symbol") or "").strip().upper()
    if _SIX_DIGIT_RE.fullmatch(peer_sym):
        _add(f"同业{peer_sym}价差(peer_gap)", "feature")

    bull_cap = v9_tuning.get("high_oil_bull_cap_pct")
    if bull_cap is not None and float(bull_cap) > 0:
        _add(f"高油价上限{float(bull_cap):g}%", "feature")

    macd_anchor = v9_tuning.get("macd_price_anchor")
    if macd_anchor is not None and float(macd_anchor) > 0:
        _add(f"{float(macd_anchor):g}净值MACD锚", "feature")

    min_retained = int(ps.get("min_retained_shares") or 0)
    if min_retained > 0:
        _add(f"保留{min_retained}份底仓锁", "feature")

    if profile == "standard":
        if rule_uses_flow(list(containers)):
            _add("L2大单/暗盘资金共振", "feature")
        else:
            _add("纯1m分时MACD基线", "feature")

    return tags


def normalize_and_validate_rule_data_sources(
    raw_sources: Any,
    rule_spec: Mapping[str, Any],
    *,
    strict_declared: bool = False,
) -> list[dict[str, Any]]:
    """Validate declared data_sources and merge with AST-derived mandatory dependencies."""
    derived = derive_rule_data_sources(rule_spec)
    derived_by_id = {item["id"]: dict(item) for item in derived}

    if raw_sources is None:
        if strict_declared:
            req_ids = [item["id"] for item in derived]
            raise ValueError(f"新增量化规则必须显式声明 data_sources，需包含: {', '.join(req_ids)}")
        return derived

    if not isinstance(raw_sources, Sequence) or isinstance(raw_sources, (str, bytes)):
        raise ValueError("data_sources 必须是数据源声明数组")

    declared_by_id: dict[str, dict[str, Any]] = {}
    for idx, entry in enumerate(raw_sources):
        if isinstance(entry, str):
            src_id = entry.strip()
            role_override = None
            req_override = None
            sym_override: list[str] = []
            note_override = ""
            auto_derived_flag = False
        elif isinstance(entry, Mapping):
            src_id = str(entry.get("id") or "").strip()
            role_override = str(entry.get("role") or "").strip() or None
            req_override = entry.get("required_in_session")
            sym_override = _clean_symbol_list(entry.get("symbols") or [])
            note_override = str(entry.get("note") or "").strip()
            auto_derived_flag = (
                bool(entry.get("auto_derived"))
                if "auto_derived" in entry
                else False
            )
        else:
            raise ValueError(f"data_sources[{idx}] 必须是字符串或对象")

        if src_id not in VALID_QUANT_DATA_SOURCE_IDS:
            raise ValueError(f"未知量化数据源: {src_id or '<empty>'}")

        base = derived_by_id.get(src_id)
        if auto_derived_flag and base is not None:
            eff_role = base["role"]
            eff_note = base["note"]
        else:
            eff_role = role_override or (base["role"] if base else "intraday_aux")
            eff_note = note_override or (base["note"] if base else "")
        declared_by_id[src_id] = _build_source_entry(
            src_id,
            role=eff_role,
            required_in_session=(
                bool(req_override)
                if req_override is not None
                else (bool(base["required_in_session"]) if base else True)
            ),
            symbols=_clean_symbol_list([*(base.get("symbols", []) if base else []), *sym_override]),
            auto_derived=auto_derived_flag,
            note=eff_note,
        )

    if strict_declared:
        missing = [
            sid
            for sid, d_item in derived_by_id.items()
            if d_item.get("required_in_session", True) and sid not in declared_by_id
        ]
        if missing:
            raise ValueError(f"量化规则未完整声明依赖的数据源，缺少: {', '.join(missing)}")

    merged: dict[str, dict[str, Any]] = {**derived_by_id, **declared_by_id}
    order_map = {item["id"]: idx for idx, item in enumerate(QUANT_DATA_SOURCE_CATALOG)}
    return sorted(merged.values(), key=lambda x: order_map.get(x["id"], 999))


def _latest_sync_dt_for_symbol(
    research_store: Any,
    symbol: str,
    trade_date: str,
    points: Sequence[Mapping[str, Any]],
) -> tuple[datetime | None, str | None]:
    """Return latest DB sync/capture timestamp and any monitor last_error for symbol."""
    last_error: str | None = None
    candidates: list[datetime] = []

    if points:
        last_cap = _parse_iso_dt(points[-1].get("captured_at"))
        if last_cap is not None:
            candidates.append(last_cap)

    conn = getattr(research_store, "_connection", None)
    lock = getattr(research_store, "_lock", None)
    if conn is not None and lock is not None:
        try:
            with lock:
                mon = conn.execute(
                    "SELECT latest_trade_date, latest_time, last_sync_at, last_error FROM research_monitor_status WHERE symbol=?",
                    (symbol,),
                ).fetchone()
                day = conn.execute(
                    "SELECT collected_at FROM research_days WHERE symbol=? AND trade_date=?",
                    (symbol, trade_date),
                ).fetchone()
            if mon is not None:
                if mon["last_error"]:
                    last_error = str(mon["last_error"])
                mon_dt = _parse_iso_dt(mon["last_sync_at"])
                if mon_dt is not None:
                    candidates.append(mon_dt)
            if day is not None:
                day_dt = _parse_iso_dt(day["collected_at"])
                if day_dt is not None:
                    candidates.append(day_dt)
        except Exception:
            pass

    if not candidates:
        return None, last_error
    return max(candidates), last_error


def _read_today_symbol_latest_bar(
    research_store: Any,
    symbol: str,
    trade_date: str,
) -> tuple[str | None, int, datetime | None]:
    conn = getattr(research_store, "_connection", None)
    lock = getattr(research_store, "_lock", None)
    if conn is None or lock is None:
        return None, 0, None
    try:
        with lock:
            rows = conn.execute(
                """SELECT minute, captured_at FROM research_points
                   WHERE symbol=? AND trade_date=?
                   ORDER BY REPLACE(minute, ':', '')""",
                (symbol, trade_date),
            ).fetchall()
        if not rows:
            return None, 0, None
        last = rows[-1]
        return str(last["minute"] or ""), len(rows), _parse_iso_dt(last["captured_at"])
    except Exception:
        return None, 0, None


def _read_today_symbol_nonzero_vol_count(
    research_store: Any,
    symbol: str,
    trade_date: str,
) -> int:
    conn = getattr(research_store, "_connection", None)
    lock = getattr(research_store, "_lock", None)
    if conn is None or lock is None:
        return 0
    try:
        with lock:
            row = conn.execute(
                """SELECT COUNT(*) FROM research_points
                   WHERE symbol=? AND trade_date=? AND CAST(COALESCE(volume,'0') AS REAL) > 0""",
                (symbol, trade_date),
            ).fetchone()
        return int(row[0] or 0) if row is not None else 0
    except Exception:
        return 0


def _is_in_sc0_day_session(local_dt: datetime) -> bool:
    hhmm = local_dt.strftime("%H:%M")
    return local_dt.weekday() < 5 and (
        ("09:02" <= hhmm <= "10:15")
        or ("10:32" <= hhmm <= "11:30")
        or ("13:32" <= hhmm <= "15:00")
    )


def _check_macro_source_health(
    macro: V9MacroDataStore,
    src_id: str,
    *,
    symbol: str,
    today_str: str,
    now_dt: datetime,
    is_trading_day: bool,
    local_points_check: Any = None,
    research_store: Any = None,
    rule_profile: str = "standard",
) -> tuple[str, str]:
    """Return ('ok'|'fallback'|'missing', reason_detail) for one auxiliary/macro source during active session."""
    if src_id == "eastmoney_fund_nav":
        nav_map = macro.load_nav_map(symbol) if _SIX_DIGIT_RE.fullmatch(symbol) else {}
        valid_dates = [d for d in nav_map if str(d) < today_str and float(nav_map[d]) > 0]
        if valid_dates:
            return "ok", f"NAV {max(valid_dates)}"
        return "missing", f"{symbol} 官方历史净值 (NAV) 缺失"

    if src_id == "fx_usdcny_usdcnh":
        req_syms = {str(s).strip().upper() for s in (local_points_check or ())}
        if "USDCNH" in req_syms:
            ts_cnh, px_cnh = load_merged_fx_cnh_series(macro)
            cnh_mem = _MEMORY_MACRO_CACHE.get(("global", "fx_usdcnh_series"))
            rs_ref = getattr(macro, "research_store", None) or research_store
            cnh_db = _read_macro_cache_table(rs_ref).get("fx_usdcnh_series") if rs_ref is not None else None
            if ts_cnh and px_cnh and (cnh_mem or cnh_db):
                return "ok", f"USD/CNH+CNY {px_cnh[-1]:.4f}"
        ts, px = macro.load_fx_series()
        if ts and px:
            return "ok", f"USD/CNY {px[-1]:.4f}"
        _, fx_spot = macro.fetch_live_spot()
        if not (fx_spot and fx_spot > 0) and macro.allow_network:
            fof_spot = fetch_live_fof_crude_spot(macro) or {}
            t0_spot = fetch_live_fof_t0_spot(macro) or {}
            sp_spot = fetch_live_sp_oilgas_etf_spot(macro) or {}
            fx_spot = fof_spot.get("fx_px") or t0_spot.get("fx_px") or sp_spot.get("fx_px")
        if fx_spot and float(fx_spot) > 0:
            return "fallback", f"USD/CNY 实时现价 {float(fx_spot):.4f}"
        return "missing", "在岸/离岸人民币汇率缺失"

    if src_id == "us_oil_etf_uso_bno_dbo":
        uso_rows = macro.load_uso_daily_rows()
        bno_rows = macro.load_bno_daily_rows()
        if not uso_rows or not bno_rows:
            return "missing", "美股原油 ETF (USO/BNO) 日线缺失"
        req_syms = {str(s).strip().upper() for s in (local_points_check or ())}
        if "DBO" in req_syms:
            dbo_rows = load_dbo_daily_rows(macro)
            if not dbo_rows:
                return "missing", "美股原油 ETF (DBO) 日线缺失"
            return "ok", f"USO {uso_rows[-1][0]} / BNO {bno_rows[-1][0]} / DBO {dbo_rows[-1][0]}"
        return "ok", f"USO {uso_rows[-1][0]} / BNO {bno_rows[-1][0]}"

    if src_id == "us_ep_xop_crack_macro":
        xop_rows = macro.load_xop_daily_rows()
        if xop_rows:
            req_syms = {str(s).strip().upper() for s in (local_points_check or ())}
            missing_etf: list[str] = []
            if "XLE" in req_syms and not macro.load_xle_daily_rows():
                missing_etf.append("XLE")
            if "SPY" in req_syms and not macro.load_spy_daily_rows():
                missing_etf.append("SPY")
            if "IWM" in req_syms and not macro.load_iwm_daily_rows():
                missing_etf.append("IWM")
            if missing_etf:
                return "fallback", f"XOP {xop_rows[-1][0]}（缺 {'/'.join(missing_etf)} 日线）"
            return "ok", f"XOP {xop_rows[-1][0]}"
        return "missing", "美股 XOP/XLE/SPY/IWM 日线缓存缺失"

    if src_id == "cme_energy_macro_futures":
        req_syms = {str(s).strip().upper() for s in (local_points_check or ())} or {"CL", "HO"}
        cl_rows = macro.load_wti_daily_rows() if ("CL" in req_syms or "HO" in req_syms) else []
        ho_rows = macro.load_ho_daily_rows() if "HO" in req_syms else []
        ng_rows = macro.load_ng_daily_rows() if "NG" in req_syms else []
        cl_1m_ts, _ = macro.load_fut_1m_series("CL") if "CL" in req_syms else ([], [])
        ng_1m_ts, _ = macro.load_fut_1m_series("NG") if "NG" in req_syms else ([], [])
        spot_x = macro.fetch_live_xop_spot() if macro.allow_network else None
        spot_f = fetch_live_fof_crude_spot(macro) if macro.allow_network else None
        spot_t0 = fetch_live_fof_t0_spot(macro) if macro.allow_network else None
        spot_sp = fetch_live_sp_oilgas_etf_spot(macro) if macro.allow_network else None
        has_live_cl = bool(
            (isinstance(spot_x, Mapping) and (spot_x.get("cl_px") or 0) > 0)
            or (isinstance(spot_f, Mapping) and (spot_f.get("cl_px") or 0) > 0)
            or (isinstance(spot_t0, Mapping) and (spot_t0.get("cl_px") or 0) > 0)
            or (isinstance(spot_sp, Mapping) and (spot_sp.get("cl_px") or 0) > 0)
        )
        if cl_rows or cl_1m_ts or has_live_cl:
            missing_parts: list[str] = []
            if "HO" in req_syms and not ho_rows:
                missing_parts.append("HO")
            if "NG" in req_syms and not ng_rows and not ng_1m_ts:
                missing_parts.append("NG")
            if missing_parts:
                return "fallback", f"CL就绪，{'/'.join(missing_parts)} 缺失暂降级"
            if cl_rows and not cl_1m_ts and not has_live_cl:
                return "fallback", f"CME 日线 {cl_rows[-1][0]}（缺实时 1m/现价）"
            return "ok", f"CL {cl_rows[-1][0] if cl_rows else 'live'}"
        return "missing", "CME 能源/裂解期货 (CL/HO/NG/ES) 缓存与实时行情均缺失"

    if src_id == "ice_brent_1m":
        brn_min, brn_cnt, _ = _read_today_symbol_latest_bar(research_store, BRENT_SYMBOL, today_str)
        now_hhmm = now_dt.strftime("%H:%M")
        wall_lag = _wall_minutes_lag(brn_min, now_hhmm) if brn_min else None
        brn_local_fresh = bool(brn_cnt >= 1 and wall_lag is not None and -5 <= wall_lag <= 5)
        oil_daily_ok = True
        if rule_profile in {"v9_plus_aggressive", "v9_plus_balanced"}:
            oil_daily_ok = bool(macro.load_oil_daily_rows())
        if brn_local_fresh and oil_daily_ok:
            return "ok", f"BRN0Y {brn_min}"
        brn_spot, _ = macro.fetch_live_spot()
        if not (brn_spot and brn_spot > 0) and macro.allow_network:
            fof_spot = fetch_live_fof_crude_spot(macro) or {}
            t0_spot = fetch_live_fof_t0_spot(macro) or {}
            sp_spot = fetch_live_sp_oilgas_etf_spot(macro) or {}
            brn_spot = fof_spot.get("brn_px") or t0_spot.get("brn_px") or sp_spot.get("brn_px")
        if brn_spot and float(brn_spot) > 0 and oil_daily_ok:
            return "fallback", f"BRN0Y 分钟滞后，由 hf_OIL 现价 ${float(brn_spot):.2f} 接管"
        brent_open, _ = is_brent_market_open(now_dt)
        if not brent_open and brn_cnt >= 1 and oil_daily_ok:
            return "fallback", f"BRN0Y 结算休市沿用 {brn_min}"
        if not oil_daily_ok:
            return "missing", "BRN0Y 2年日线序列 (oil_daily_2y) 缺失"
        return "missing", "BRN0Y 分钟流与 hf_OIL 现价双通道缺失"

    if src_id == "ine_sc0_futures":
        sc_daily = macro.load_sc_daily_rows()
        sc_ts, sc_px = macro.load_sc_min_series()
        dual_spot = macro.fetch_live_dual_oil_spot() if macro.allow_network else None
        fof_spot = fetch_live_fof_crude_spot(macro) if macro.allow_network else None
        t0_spot = fetch_live_fof_t0_spot(macro) if macro.allow_network else None
        sp_spot = fetch_live_sp_oilgas_etf_spot(macro) if macro.allow_network else None
        sc_live_px = (
            (dual_spot.get("sc_px") if isinstance(dual_spot, Mapping) else None)
            or (fof_spot.get("sc_px") if isinstance(fof_spot, Mapping) else None)
            or (t0_spot.get("sc_px") if isinstance(t0_spot, Mapping) else None)
            or (sp_spot.get("sc_px") if isinstance(sp_spot, Mapping) else None)
        )
        if sc_live_px and float(sc_live_px) > 0:
            return "ok", f"SC0 {sc_live_px}"
        if sc_ts and sc_px:
            return "fallback", f"SC0 缓存分时 {sc_px[-1]:.2f}"
        if sc_daily and not _is_in_sc0_day_session(now_dt):
            return "fallback", f"SC0 休市日线 {sc_daily[-1][0]}"
        if sc_daily and not macro.allow_network:
            return "fallback", f"SC0 离线日线 {sc_daily[-1][0]}"
        return "missing", "沪原油 SC0 (nf_SC0/分钟/日线) 数据缺失"

    if src_id == "ashare_peer_lof_quote":
        peer_syms = [s for s in (local_points_check or []) if _SIX_DIGIT_RE.fullmatch(s) and s != symbol]
        if not peer_syms:
            default_peers = ("162411",) if symbol == "513350" else ("160723", "501018")
            peer_syms = [s for s in default_peers if s != symbol]
        now_hhmm = now_dt.strftime("%H:%M")
        for psym in peer_syms:
            p_min, p_cnt, p_sync = _read_today_symbol_latest_bar(research_store, psym, today_str)
            p_lag = _trading_minutes_lag(p_min, now_hhmm) if p_min else None
            p_sync_age = (now_dt - p_sync).total_seconds() if p_sync is not None else None
            if p_cnt >= 1 and (
                (p_lag is not None and p_lag <= 5)
                or (p_sync_age is not None and p_sync_age <= 180.0 and p_lag is not None and p_lag <= 15)
            ):
                return "ok", f"同业 {psym} {p_min}"
        if macro.allow_network:
            spot_d = macro.fetch_live_dual_oil_spot() or {}
            spot_f = fetch_live_fof_crude_spot(macro) or {}
            spot_t0 = fetch_live_fof_t0_spot(macro) or {}
            spot_sp = fetch_live_sp_oilgas_etf_spot(macro) or {}
            for psym in peer_syms:
                q = (
                    spot_d.get(f"peer_{psym}")
                    or spot_f.get(f"peer_{psym}")
                    or spot_t0.get(f"peer_{psym}")
                    or spot_sp.get(f"peer_{psym}")
                )
                if isinstance(q, Mapping) and (q.get("price") or 0) > 0:
                    return "fallback", f"同业 {psym} 新浪现价 {q.get('price')}"
        if not is_trading_day:
            return "ok", "非交易日"
        return "missing", f"同业参照 ({'/'.join(peer_syms)}) 分钟流与实时行情均不可用"

    return "ok", "就绪"


def check_rule_live_data_guard(
    research_store: Any,
    rule_spec: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: str,
    actual_trade_date: str | None = None,
    points: Sequence[Mapping[str, Any]] = (),
    now_dt: datetime | None = None,
    is_trading_day: bool = True,
    allow_network: bool | None = None,
) -> dict[str, Any]:
    """Enforce live trading data integrity for a quant rule before order emission.

    Returns a guard summary dict with `blocked` (bool), `status`, `code`, `reason`,
    `blocking_sources`, `fallback_sources`, `checked_at`, and `latest_minute`.
    """
    local_now = (now_dt or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    now_hhmm = local_now.strftime("%H:%M")
    now_iso = local_now.isoformat(timespec="seconds")
    latest_minute = str(points[-1].get("time") or "") if points else ""
    eff_td = str(actual_trade_date or trade_date).strip()
    try:
        latest_px = float(points[-1].get("price") or 0.0) if points else 0.0
    except (TypeError, ValueError):
        latest_px = 0.0
    has_valid_points = bool(eff_td == trade_date and points and len(points) >= 2 and latest_px > 0.0)

    if not is_trading_day or local_now.weekday() >= 5:
        return {
            "blocked": False,
            "in_continuous_session": False,
            "status": "standby_closed",
            "code": None,
            "reason": "非 A 股交易日，盘中熔断门禁处于待命状态",
            "blocking_sources": [],
            "fallback_sources": [],
            "primary_intraday_ok": has_valid_points,
            "allow_protective_exit": False,
            "checked_at": now_iso,
            "latest_minute": latest_minute,
        }

    if "09:30" <= now_hhmm < "09:32" or "13:00" <= now_hhmm < "13:02":
        return {
            "blocked": False,
            "in_continuous_session": True,
            "status": "warmup",
            "code": None,
            "reason": f"开盘首 2 分钟聚合宽限期 ({now_hhmm})",
            "blocking_sources": [],
            "fallback_sources": [],
            "primary_intraday_ok": has_valid_points,
            "allow_protective_exit": False,
            "checked_at": now_iso,
            "latest_minute": latest_minute,
        }

    in_active_window = ("09:32" <= now_hhmm <= "11:30") or ("13:02" <= now_hhmm <= "15:00")
    if not in_active_window:
        return {
            "blocked": False,
            "in_continuous_session": False,
            "status": "standby_closed",
            "code": None,
            "reason": f"非连续竞价核心窗口 ({now_hhmm})，盘中门禁待命",
            "blocking_sources": [],
            "fallback_sources": [],
            "primary_intraday_ok": has_valid_points,
            "allow_protective_exit": False,
            "checked_at": now_iso,
            "latest_minute": latest_minute,
        }

    data_sources = rule_spec.get("data_sources")
    if not isinstance(data_sources, Sequence) or isinstance(data_sources, (str, bytes)) or not data_sources:
        data_sources = derive_rule_data_sources(rule_spec)

    ps = rule_spec.get("position_sizing") if isinstance(rule_spec.get("position_sizing"), Mapping) else {}
    rule_profile = str(ps.get("engine_profile") or "standard").strip().lower()

    blocking_sources: list[str] = []
    fallback_sources: list[str] = []
    reasons: list[str] = []
    block_code: str | None = None
    checked_primary = False

    macro: V9MacroDataStore | None = None

    def _get_macro() -> V9MacroDataStore:
        nonlocal macro
        if macro is None:
            eff_net = (not _is_quant_offline()) if allow_network is None else bool(allow_network)
            macro = V9MacroDataStore(research_store, allow_network=eff_net)
        return macro

    for src in data_sources:
        if not isinstance(src, Mapping):
            continue
        src_id = str(src.get("id") or "").strip()
        if not src.get("required_in_session", True) or src_id not in VALID_QUANT_DATA_SOURCE_IDS:
            continue

        if src_id == "ashare_intraday_1m":
            checked_primary = True
            if not has_valid_points:
                blocking_sources.append(src_id)
                block_code = block_code or "PRIMARY_INTRADAY_MISSING"
                reasons.append(f"标的 {symbol} 当日({trade_date})分钟分时样本不足或日期不匹配")
                continue
            lag_min = _trading_minutes_lag(latest_minute, now_hhmm)
            sync_dt, mon_err = _latest_sync_dt_for_symbol(research_store, symbol, trade_date, points)
            sync_age = (local_now - sync_dt).total_seconds() if sync_dt is not None else None
            if lag_min is not None and lag_min > 3:
                stale_sync = sync_age is None or sync_age > 180.0
                if mon_err or stale_sync or lag_min > 15:
                    blocking_sources.append(src_id)
                    block_code = block_code or "PRIMARY_INTRADAY_STALE"
                    err_hint = f"，采集报错 {mon_err}" if mon_err else ""
                    reasons.append(
                        f"标的 {symbol} 盘中分时滞后 {lag_min} 交易分钟（最新 {latest_minute} vs 当前 {now_hhmm}{err_hint}）"
                    )
            continue

        if src_id == "ashare_l2_fund_flow":
            if not points:
                blocking_sources.append(src_id)
                block_code = block_code or "L2_FLOW_MISSING"
                reasons.append(f"标的 {symbol} 缺失分时样本，无法读取资金流")
                continue
            has_inline_flow = any(
                pt.get("large_order_amount") is not None or pt.get("large_order_net") is not None
                for pt in points[-2:]
            )
            has_l2_hist = False
            read_ff = getattr(research_store, "read_fund_flow_history", None)
            if callable(read_ff):
                try:
                    hist = read_ff(symbol, trade_date)
                    today_pts = ((hist.get("periods") or {}).get("today") or {}).get("points") or []
                    has_l2_hist = bool(today_pts)
                except Exception:
                    has_l2_hist = False
            if not has_inline_flow and not has_l2_hist:
                blocking_sources.append(src_id)
                block_code = block_code or "L2_FLOW_MISSING"
                reasons.append(f"标的 {symbol} 当日缺失大单/L2资金流分钟数据")
            continue

        status, detail = _check_macro_source_health(
            _get_macro(),
            src_id,
            symbol=symbol,
            today_str=trade_date,
            now_dt=local_now,
            is_trading_day=is_trading_day,
            local_points_check=src.get("symbols") or (),
            research_store=research_store,
            rule_profile=rule_profile,
        )
        if status == "missing":
            blocking_sources.append(src_id)
            block_code = block_code or f"SOURCE_{src_id.upper()}_MISSING"
            reasons.append(detail)
        elif status == "fallback":
            fallback_sources.append(src_id)

    blocked = bool(blocking_sources)
    primary_intraday_ok = ("ashare_intraday_1m" not in blocking_sources) and has_valid_points
    if primary_intraday_ok and not checked_primary:
        lag_min = _trading_minutes_lag(latest_minute, now_hhmm)
        sync_dt, mon_err = _latest_sync_dt_for_symbol(research_store, symbol, trade_date, points)
        sync_age = (local_now - sync_dt).total_seconds() if sync_dt is not None else None
        if lag_min is not None and lag_min > 3:
            stale_sync = sync_age is None or sync_age > 180.0
            if mon_err or stale_sync or lag_min > 15:
                primary_intraday_ok = False
    same_day_t0 = resolve_rule_same_day_t0(symbol, ps)
    flatten_hhmm = str(ps.get("close_flatten_time") or "14:53")
    allow_protective_exit = bool(blocked and primary_intraday_ok and same_day_t0)

    status_label = (
        "blocked"
        if blocked
        else ("fallback_ok" if fallback_sources else "ready")
    )
    reason_text = (
        "；".join(reasons)
        if reasons
        else (
            f"核心主源正常，{len(fallback_sources)} 项辅助源已自动切换备援通道"
            if fallback_sources
            else "规则全部依赖数据源实时就绪"
        )
    )
    if allow_protective_exit:
        reason_text = (
            f"{reason_text}（已严禁新开仓；A股1m主源正常，"
            f"保留日内T+0硬止损与{flatten_hhmm}避险平T通道）"
        )
    return {
        "blocked": blocked,
        "in_continuous_session": True,
        "status": status_label,
        "code": block_code,
        "reason": reason_text,
        "blocking_sources": blocking_sources,
        "fallback_sources": fallback_sources,
        "primary_intraday_ok": primary_intraday_ok,
        "allow_protective_exit": allow_protective_exit,
        "checked_at": now_iso,
        "latest_minute": latest_minute,
    }


def _describe_market_sessions(now_dt: datetime, is_trading_day: bool) -> list[dict[str, Any]]:
    hhmm = now_dt.strftime("%H:%M")
    wd = now_dt.weekday()
    ashare_open = bool(is_trading_day and wd < 5 and (("09:30" <= hhmm <= "11:30") or ("13:00" <= hhmm <= "15:00")))
    if not is_trading_day or wd >= 5:
        ashare_label = "休市"
    elif "09:30" <= hhmm <= "11:30" or "13:00" <= hhmm <= "15:00":
        ashare_label = "交易中"
    elif "11:30" < hhmm < "13:00":
        ashare_label = "午间休市"
    elif hhmm < "09:30":
        ashare_label = "盘前待开"
    else:
        ashare_label = "已收盘定格"

    brent_open, brent_label = is_brent_market_open(now_dt)

    sc0_open = bool(
        wd < 5 and (("09:00" <= hhmm <= "10:15") or ("10:30" <= hhmm <= "11:30") or ("13:30" <= hhmm <= "15:00"))
    ) or (
        (wd < 5 and hhmm >= "21:00") or (wd in {1, 2, 3, 4, 5} and hhmm <= "02:30")
    )
    sc0_label = "交易中" if sc0_open else ("休市小休" if wd < 5 and ("10:15" < hhmm < "10:30" or "11:30" < hhmm < "13:30") else "休市定格")

    ny_now = now_dt.astimezone(NY_TZ)
    ny_hm = ny_now.hour * 60 + ny_now.minute
    us_open = bool(ny_now.weekday() < 5 and (9 * 60 + 30 <= ny_hm <= 16 * 60))
    us_label = "美股交易中" if us_open else "已收盘定格"

    fx_ny_wd = ny_now.weekday()
    fx_open = not (
        fx_ny_wd == 5
        or (fx_ny_wd == 4 and ny_hm >= 17 * 60)
        or (fx_ny_wd == 6 and ny_hm < 17 * 60)
    )
    fx_label = "汇市交易中" if fx_open else "周末休市"

    # CME Globex (WTI CL / HO / NG / ES): Sun 18:00 ET - Fri 17:00 ET, daily break Mon-Thu 17:00-18:00 ET
    cme_wd = ny_now.weekday()
    if cme_wd == 5:
        cme_open = False
        cme_label = "周末休市"
    elif cme_wd == 6:
        cme_open = ny_hm >= 18 * 60
        cme_label = "交易中" if cme_open else "待周日18:00 ET"
    elif cme_wd == 4:
        cme_open = ny_hm < 17 * 60
        cme_label = "交易中" if cme_open else "周末收盘"
    else:
        cme_open = not (17 * 60 <= ny_hm < 18 * 60)
        cme_label = "交易中" if cme_open else "每日维护休市"

    return [
        {"market": "ashare", "label": "A股", "open": ashare_open, "state_label": ashare_label},
        {"market": "ice_brent", "label": "ICE布油", "open": brent_open, "state_label": "交易中" if brent_open else brent_label},
        {"market": "ine_sc0", "label": "沪原油SC0", "open": sc0_open, "state_label": sc0_label},
        {"market": "cme_futures", "label": "CME美期", "open": cme_open, "state_label": cme_label},
        {"market": "us_etf", "label": "美股ETF", "open": us_open, "state_label": us_label},
        {"market": "fx", "label": "外汇", "open": fx_open, "state_label": fx_label},
    ]


def _extract_premium_live_symbols(auto_trade_engine: Any) -> list[str]:
    if auto_trade_engine is None:
        return []
    store = getattr(auto_trade_engine, "auto_store", None)
    if store is None or not callable(getattr(store, "get_config", None)):
        return []
    try:
        cfg = store.get_config()
    except Exception:
        return []
    if not bool(cfg.get("enabled", True)):
        return []
    eff_fn = getattr(auto_trade_engine, "_effective_symbol_config", None)
    out: list[str] = []
    for sym in _clean_symbol_list(cfg.get("symbols") or []):
        sym_cfg = eff_fn(cfg, sym) if callable(eff_fn) else cfg
        if not sym_cfg.get("symbol_enabled", True):
            continue
        if str(sym_cfg.get("signal_source") or "") in {"lof_premium", "combo_confirm"}:
            if sym not in out:
                out.append(sym)
    return out


def _read_premium_snapshot(research_store: Any) -> tuple[dict[str, Any] | None, datetime | None]:
    conn = getattr(research_store, "_connection", None)
    lock = getattr(research_store, "_lock", None)
    if conn is None or lock is None:
        return None, None
    try:
        with lock:
            row = conn.execute(
                "SELECT value_json, updated_at FROM research_settings WHERE key='premium_snapshot'"
            ).fetchone()
        if row is None or not row["value_json"]:
            return None, None
        parsed = json.loads(str(row["value_json"]))
        if not isinstance(parsed, Mapping):
            return None, None
        dt_obj = _parse_iso_dt(parsed.get("as_of")) or _parse_iso_dt(row["updated_at"])
        return dict(parsed), dt_obj
    except Exception:
        return None, None


def _read_day_latest_closes(research_store: Any, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
    conn = getattr(research_store, "_connection", None)
    lock = getattr(research_store, "_lock", None)
    clean = _clean_symbol_list(list(symbols))
    if conn is None or lock is None or not clean:
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        placeholders = ",".join("?" for _ in clean)
        with lock:
            rows = conn.execute(
                f"""SELECT symbol, trade_date, pre_close, quote_price, collected_at
                    FROM research_days
                    WHERE symbol IN ({placeholders})
                    ORDER BY trade_date ASC""",
                tuple(clean),
            ).fetchall()
        for r in rows:
            sym = str(r["symbol"]).upper()
            out[sym] = {
                "trade_date": str(r["trade_date"] or ""),
                "pre_close": r["pre_close"],
                "quote_price": r["quote_price"],
                "collected_at": str(r["collected_at"] or ""),
            }
    except Exception:
        pass
    return out


def _probe_orderbooks_safe(auto_trade_engine: Any, symbols: Sequence[str]) -> None:
    clean_syms = [s for s in _clean_symbol_list(list(symbols)) if _SIX_DIGIT_RE.fullmatch(s)]
    if not clean_syms:
        return
    if auto_trade_engine is not None and callable(getattr(auto_trade_engine, "prewarm_order_books", None)):
        try:
            res = auto_trade_engine.prewarm_order_books(clean_syms)
            if isinstance(res, Mapping) and res:
                _MEMORY_MACRO_CACHE[("global", "probe_orderbook_l1")] = (time.time(), {k: dict(v) for k, v in res.items()})
                return
        except Exception:
            pass
    try:
        from .public_market import TencentPublicMarketProvider

        lookup_fn = getattr(auto_trade_engine, "symbol_lookup", None) if auto_trade_engine is not None else None
        identities: list[SymbolLookup] = []
        for s in clean_syms:
            looked = None
            if callable(lookup_fn):
                try:
                    looked = lookup_fn(s)
                except Exception:
                    looked = None
            if isinstance(looked, SymbolLookup):
                identities.append(looked)
            else:
                if s.startswith(("5", "6", "9")):
                    mkt = "20" if is_exchange_fund_symbol(s) else "17"
                else:
                    mkt = "36" if is_exchange_fund_symbol(s) else "33"
                identities.append(SymbolLookup(symbol=s, name=PREMIUM_NAMES.get(s, s), market=mkt))
        provider = TencentPublicMarketProvider(timeout_seconds=3.0)
        books = provider.read_order_books_batch(identities)
        if books:
            now_m = time.monotonic()
            now_t = time.time()
            _MEMORY_MACRO_CACHE[("global", "probe_orderbook_l1")] = (now_t, {k: dict(v) for k, v in books.items()})
            ob_cache = getattr(auto_trade_engine, "_order_book_cache", None) if auto_trade_engine is not None else None
            if isinstance(ob_cache, dict):
                for sym, bk in books.items():
                    ob_cache[str(sym)] = (now_m, dict(bk))
    except Exception:
        pass


def _extract_active_live_rule_bindings(auto_trade_engine: Any) -> tuple[dict[str, list[str]], list[str]]:
    """Return mapping `rule_id -> [active_live_symbols]` and sorted unique live symbols."""
    rule_to_syms: dict[str, list[str]] = {}
    live_syms: list[str] = []
    if auto_trade_engine is None:
        return rule_to_syms, live_syms
    store = getattr(auto_trade_engine, "auto_store", None)
    if store is None or not callable(getattr(store, "get_config", None)):
        return rule_to_syms, live_syms
    try:
        cfg = store.get_config()
    except Exception:
        return rule_to_syms, live_syms

    global_enabled = bool(cfg.get("enabled", True))
    cfg_syms = _clean_symbol_list(cfg.get("symbols") or [])
    profiles = cfg.get("symbol_profiles") if isinstance(cfg.get("symbol_profiles"), Mapping) else {}
    eff_fn = getattr(auto_trade_engine, "_effective_symbol_config", None)
    for sym in cfg_syms:
        if callable(eff_fn):
            try:
                sym_cfg = eff_fn(cfg, sym)
            except Exception:
                sym_cfg = cfg
        else:
            p = profiles.get(sym) if isinstance(profiles, Mapping) else {}
            sym_cfg = {**cfg, **(dict(p) if isinstance(p, Mapping) else {})}
            sym_cfg["symbol_enabled"] = bool(p.get("enabled", True)) if isinstance(p, Mapping) else True

        if not (global_enabled and sym_cfg.get("symbol_enabled", True)):
            continue
        if sym not in live_syms:
            live_syms.append(sym)
        if str(sym_cfg.get("signal_source") or "") == "quant_rule":
            rid = str(sym_cfg.get("quant_rule_id") or "").strip()
            if rid:
                rule_to_syms.setdefault(rid, [])
                if sym not in rule_to_syms[rid]:
                    rule_to_syms[rid].append(sym)
    return rule_to_syms, live_syms


def _read_macro_cache_table(research_store: Any) -> dict[str, tuple[float, Any]]:
    out: dict[str, tuple[float, Any]] = {}
    conn = getattr(research_store, "_connection", None)
    lock = getattr(research_store, "_lock", None)
    if conn is None or lock is None:
        return out
    try:
        with lock:
            rows = conn.execute("SELECT cache_key, payload_json, updated_at FROM quant_macro_cache").fetchall()
        for r in rows:
            try:
                payload = json.loads(str(r["payload_json"]))
            except Exception:
                payload = None
            out[str(r["cache_key"])] = (float(r["updated_at"]), payload)
    except Exception:
        pass
    return out


def _read_monitor_status_map(research_store: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    conn = getattr(research_store, "_connection", None)
    lock = getattr(research_store, "_lock", None)
    if conn is None or lock is None:
        return out
    try:
        with lock:
            rows = conn.execute("SELECT * FROM research_monitor_status").fetchall()
        for r in rows:
            out[str(r["symbol"]).upper()] = dict(r)
    except Exception:
        pass
    return out


def build_quant_data_sources_report(
    research_store: Any,
    quant_store: Any,
    *,
    auto_trade_engine: Any = None,
    research_monitor: Any = None,
    brent_monitor: Any = None,
    probe: bool = False,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """Build real-time health, cadence, fallback & rule-binding report for all quant data sources."""
    global _LAST_PROBE_TS, _LAST_PROBE_LATENCY_MS

    local_now = (now_dt or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    today_str = local_now.strftime("%Y%m%d")
    now_hhmm = local_now.strftime("%H:%M")
    now_epoch = time.time()

    is_tday = True
    if auto_trade_engine is not None and callable(getattr(auto_trade_engine, "is_trading_day", None)):
        try:
            is_tday = bool(auto_trade_engine.is_trading_day(local_now.date()))
        except Exception:
            is_tday = local_now.weekday() < 5
    else:
        is_tday = local_now.weekday() < 5

    probe_executed = False
    probe_latency_ms: float | None = None
    allow_net = not _is_quant_offline()
    macro = V9MacroDataStore(research_store, allow_network=allow_net)

    rules = quant_store.list_rules() if quant_store is not None else []
    live_rule_bindings, active_live_symbols = _extract_active_live_rule_bindings(auto_trade_engine)
    premium_live_symbols = _extract_premium_live_symbols(auto_trade_engine)

    # Precompute referenced A-share symbols before probe so orderbook probe covers them
    probe_syms: set[str] = {*_OIL_PEER_SYMBOLS, *active_live_symbols}
    for rule in rules:
        for s in rule.get("symbols") or []:
            clean_s = str(s).strip().upper()
            if _SIX_DIGIT_RE.fullmatch(clean_s):
                probe_syms.add(clean_s)

    if probe and allow_net:
        with _PROBE_LOCK:
            cooldown_ok = (now_epoch - _LAST_PROBE_TS) >= 5.0
            if cooldown_ok:
                started = time.perf_counter()

                def _safe_probe(fn: Any, *args: Any, **kwargs: Any) -> None:
                    try:
                        fn(*args, **kwargs)
                    except Exception:
                        pass

                _safe_probe(macro.fetch_live_spot)
                _safe_probe(macro.fetch_live_dual_oil_spot)
                _safe_probe(macro.fetch_live_xop_spot)
                _safe_probe(fetch_live_fof_crude_spot, macro)
                _safe_probe(fetch_live_fof_t0_spot, macro)
                _safe_probe(fetch_live_sp_oilgas_etf_spot, macro)
                for fn, args in (
                    (macro.load_oil_daily_rows, ()),
                    (macro.load_fx_series, ()),
                    (load_merged_fx_cnh_series, (macro,)),
                    (macro.load_sc_daily_rows, ()),
                    (macro.load_sc_min_series, ()),
                    (macro.load_uso_daily_rows, ()),
                    (macro.load_bno_daily_rows, ()),
                    (load_dbo_daily_rows, (macro,)),
                    (macro.load_xop_daily_rows, ()),
                    (macro.load_xle_daily_rows, ()),
                    (macro.load_iwm_daily_rows, ()),
                    (macro.load_spy_daily_rows, ()),
                    (macro.load_cl_daily_rows, ()),
                    (macro.load_wti_daily_rows, ()),
                    (macro.load_ho_daily_rows, ()),
                    (macro.load_ng_daily_rows, ()),
                    (macro.load_fut_1m_series, ("CL",)),
                    (macro.load_fut_1m_series, ("HO",)),
                    (macro.load_fut_1m_series, ("NG",)),
                    (macro.load_fut_1m_series, ("ES",)),
                    (_probe_orderbooks_safe, (auto_trade_engine, sorted(probe_syms))),
                ):
                    _safe_probe(fn, *args)
                for nav_sym in _OIL_PEER_SYMBOLS:
                    _safe_probe(macro.load_nav_map, nav_sym)
                probe_latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
                _LAST_PROBE_TS = time.time()
                _LAST_PROBE_LATENCY_MS = probe_latency_ms
                probe_executed = True
            else:
                probe_latency_ms = _LAST_PROBE_LATENCY_MS

    macro_cache = _read_macro_cache_table(research_store)
    mon_status = _read_monitor_status_map(research_store)
    sessions = _describe_market_sessions(local_now, is_tday)
    sess_by_key = {s["market"]: s for s in sessions}
    spot_telemetry = get_live_spot_telemetry()

    # Precompute latest bar for all referenced A-share symbols + oil peers + BRN0Y
    referenced_syms: set[str] = {BRENT_SYMBOL, *probe_syms, *premium_live_symbols}
    for rule in rules:
        for s in rule.get("symbols") or []:
            clean_s = str(s).strip().upper()
            if clean_s:
                referenced_syms.add(clean_s)
        for ds in rule.get("data_sources") or []:
            if isinstance(ds, Mapping):
                for s in ds.get("symbols") or []:
                    clean_s = str(s).strip().upper()
                    if _SIX_DIGIT_RE.fullmatch(clean_s):
                        referenced_syms.add(clean_s)

    latest_bars: dict[str, tuple[str | None, int, datetime | None]] = {}
    nonzero_vols: dict[str, int] = {}
    for sym in sorted(referenced_syms):
        latest_bars[sym] = _read_today_symbol_latest_bar(research_store, sym, today_str)
        if _SIX_DIGIT_RE.fullmatch(sym):
            nonzero_vols[sym] = _read_today_symbol_nonzero_vol_count(research_store, sym, today_str)

    # Fallback: most recent trading day in research_days for closed-market inspection
    latest_known_days: dict[str, tuple[str | None, str | None]] = {}
    conn = getattr(research_store, "_connection", None)
    lock = getattr(research_store, "_lock", None)
    if conn is not None and lock is not None:
        try:
            with lock:
                day_rows = conn.execute(
                    """SELECT symbol, MAX(trade_date) AS max_td, MAX(collected_at) AS max_col
                       FROM research_days GROUP BY symbol"""
                ).fetchall()
            for r in day_rows:
                latest_known_days[str(r["symbol"]).upper()] = (
                    str(r["max_td"]) if r["max_td"] else None,
                    str(r["max_col"]) if r["max_col"] else None,
                )
        except Exception:
            pass

    # Map source_id -> bound rules
    rules_by_source: dict[str, list[dict[str, Any]]] = {item["id"]: [] for item in QUANT_DATA_SOURCE_CATALOG}
    rule_source_deps: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        rid = str(rule["id"])
        live_syms_for_rule = live_rule_bindings.get(rid, [])
        raw_ds = rule.get("data_sources")
        ds_list = (
            list(raw_ds)
            if isinstance(raw_ds, Sequence) and not isinstance(raw_ds, (str, bytes)) and raw_ds
            else derive_rule_data_sources(rule)
        )
        rule_source_deps[rid] = ds_list
        for ds in ds_list:
            sid = str(ds.get("id") or "")
            if sid in rules_by_source:
                target_sym = str(rule.get("target_symbol") or (rule.get("symbols") or ["160723"])[0])
                role_code = str(ds.get("role") or "intraday_aux")
                rules_by_source[sid].append(
                    {
                        "rule_id": rid,
                        "name": rule["name"],
                        "short_label": _format_rule_short_label(rid, str(rule.get("name") or ""), target_sym),
                        "symbols": [str(s) for s in (rule.get("symbols") or [])],
                        "target_symbol": target_sym,
                        "role": role_code,
                        "role_label": str(ds.get("role_label") or _ROLE_LABELS.get(role_code, role_code)),
                        "consumed_symbols": _clean_symbol_list(ds.get("symbols") or []),
                        "usage_note": str(ds.get("note") or ""),
                        "required_in_session": bool(ds.get("required_in_session", True)),
                        "active_in_live_trade": bool(live_syms_for_rule),
                        "live_symbols": list(live_syms_for_rule),
                    }
                )

    brent_live = brent_monitor.latest_live_quote() if brent_monitor is not None and hasattr(brent_monitor, "latest_live_quote") else None
    mem_oil_fx = (_MEMORY_MACRO_CACHE.get(("global", "live_spot_oil_fx")) or (0.0, None))
    mem_dual = (_MEMORY_MACRO_CACHE.get(("global", "live_spot_dual_oil_bundle")) or (0.0, None))
    mem_xop = (_MEMORY_MACRO_CACHE.get(("global", "live_spot_xop_bundle")) or (0.0, None))
    mem_fof = (_MEMORY_MACRO_CACHE.get(("global", "live_spot_fof_crude_bundle")) or (0.0, None))
    mem_fof_t0 = (_MEMORY_MACRO_CACHE.get(("global", "live_spot_fof_t0_pulse_bundle")) or (0.0, None))
    mem_sp_etf = (_MEMORY_MACRO_CACHE.get(("global", "live_spot_sp_oilgas_etf_bundle")) or (0.0, None))
    mem_ob_probe = (_MEMORY_MACRO_CACHE.get(("global", "probe_orderbook_l1")) or (0.0, None))
    day_closes_map = _read_day_latest_closes(research_store, sorted(s for s in referenced_syms if _SIX_DIGIT_RE.fullmatch(s)))

    # Build health card for each source in QUANT_DATA_SOURCE_CATALOG
    sources_out: list[dict[str, Any]] = []
    ashare_in_core = ("09:32" <= now_hhmm <= "11:30") or ("13:02" <= now_hhmm <= "15:00")
    ashare_in_session = bool(is_tday and local_now.weekday() < 5 and ashare_in_core)

    def _age_str_and_sec(ts_epoch: float | None, dt_obj: datetime | None) -> tuple[int | None, str | None]:
        if ts_epoch is not None and ts_epoch > 0:
            age = max(0, int(round(now_epoch - ts_epoch)))
            iso = datetime.fromtimestamp(ts_epoch, tz=SHANGHAI_TZ).isoformat(timespec="seconds")
            return age, iso
        if dt_obj is not None:
            age = max(0, int(round((local_now - dt_obj).total_seconds())))
            return age, dt_obj.isoformat(timespec="seconds")
        return None, None

    for meta in QUANT_DATA_SOURCE_CATALOG:
        sid = meta["id"]
        bound = rules_by_source.get(sid, [])
        active_bound = [b for b in bound if b["active_in_live_trade"]]
        health_status = "healthy"
        health_label = "实时健康"
        session_state = "open"
        session_label = "盘中采集"
        last_updated_at: str | None = None
        age_seconds: int | None = None
        latest_sample = "--"
        error_msg: str | None = None
        symbol_details: list[dict[str, Any]] = []

        if sid == "ashare_intraday_1m":
            ashare_sess = sess_by_key["ashare"]
            session_state = "open" if ashare_sess["open"] else "closed"
            session_label = ashare_sess["state_label"]
            check_syms = sorted(
                {
                    *[str(s) for b in bound for s in b["symbols"] if _SIX_DIGIT_RE.fullmatch(str(s))],
                    *active_live_symbols,
                }
            )
            stale_errs: list[str] = []
            max_sync_age: int | None = None
            latest_dt_obj: datetime | None = None
            sample_parts: list[str] = []
            for sym in check_syms:
                lmin, lcnt, lcap = latest_bars.get(sym, (None, 0, None))
                mon = mon_status.get(sym) or {}
                m_err = mon.get("last_error")
                m_sync = _parse_iso_dt(mon.get("last_sync_at")) or lcap
                max_td, col_iso = latest_known_days.get(sym, (None, None))
                eff_dt = m_sync or _parse_iso_dt(col_iso)
                if eff_dt is not None and (latest_dt_obj is None or eff_dt > latest_dt_obj):
                    latest_dt_obj = eff_dt
                s_age = max(0, int(round((local_now - eff_dt).total_seconds()))) if eff_dt is not None else None
                if s_age is not None:
                    max_sync_age = s_age if max_sync_age is None else max(max_sync_age, s_age)
                sym_ok = True
                nz_vol = nonzero_vols.get(sym, 0)
                sym_note = (
                    f"{lmin or mon.get('latest_time') or '--'} ({lcnt}根·量{nz_vol})"
                    if lcnt > 0
                    else f"最新交易日 {max_td or '--'}"
                )
                if ashare_in_session:
                    lag_m = _trading_minutes_lag(lmin, now_hhmm) if lmin else None
                    if lcnt < 2:
                        sym_ok = False
                        stale_errs.append(f"{sym}今日分时不足")
                    elif lag_m is not None and lag_m > 3 and (m_err or s_age is None or s_age > 180 or lag_m > 15):
                        sym_ok = False
                        stale_errs.append(f"{sym}滞后{lag_m}m")
                elif m_err:
                    stale_errs.append(f"{sym}:{m_err}")
                symbol_details.append(
                    {
                        "symbol": sym,
                        "status": "ok" if sym_ok else "error",
                        "latest_time": lmin or str(mon.get("latest_time") or max_td or "--"),
                        "note": sym_note,
                    }
                )
                sample_parts.append(f"{sym} {lmin or mon.get('latest_time') or max_td or '--'}")
            age_seconds, last_updated_at = _age_str_and_sec(None, latest_dt_obj)
            latest_sample = " · ".join(sample_parts[:4]) if sample_parts else "无监控标的"
            if ashare_in_session:
                if stale_errs:
                    health_status = "unhealthy"
                    health_label = "盘中断流熔断"
                    error_msg = "；".join(stale_errs[:3])
                else:
                    health_status = "healthy"
                    health_label = "盘中实时正常"
            else:
                if not check_syms:
                    health_status = "idle"
                    health_label = "无绑定标的"
                elif stale_errs:
                    health_status = "degraded"
                    health_label = "近期同步异常"
                    error_msg = "；".join(stale_errs[:3])
                else:
                    health_status = "market_closed"
                    health_label = "休市定格·就绪"

        elif sid == "ashare_orderbook_l1":
            ashare_sess = sess_by_key["ashare"]
            session_state = "open" if ashare_sess["open"] else "closed"
            session_label = ashare_sess["state_label"]
            check_syms = sorted(
                {
                    *[str(s) for b in bound for s in b["symbols"] if _SIX_DIGIT_RE.fullmatch(str(s))],
                    *active_live_symbols,
                }
                or set(_OIL_PEER_SYMBOLS)
            )
            ob_cache = getattr(auto_trade_engine, "_order_book_cache", {}) if auto_trade_engine is not None else {}
            probe_books = mem_ob_probe[1] if isinstance(mem_ob_probe[1], Mapping) else {}
            mono_now = time.monotonic()
            ob_parts: list[str] = []
            fresh_cnt = 0
            fallback_day_cnt = 0
            latest_ob_ts: float | None = None
            for sym in check_syms:
                book: Mapping[str, Any] | None = None
                book_age: float | None = None
                c_entry = ob_cache.get(sym) if isinstance(ob_cache, Mapping) else None
                if isinstance(c_entry, tuple) and len(c_entry) >= 2 and isinstance(c_entry[1], Mapping):
                    book = c_entry[1]
                    book_age = max(0.0, mono_now - float(c_entry[0]))
                    ts_est = now_epoch - book_age
                    if latest_ob_ts is None or ts_est > latest_ob_ts:
                        latest_ob_ts = ts_est
                elif isinstance(probe_books.get(sym), Mapping):
                    book = probe_books[sym]
                    book_age = max(0.0, now_epoch - float(mem_ob_probe[0]))
                    if latest_ob_ts is None or float(mem_ob_probe[0]) > latest_ob_ts:
                        latest_ob_ts = float(mem_ob_probe[0])
                b1 = ((book.get("bids") or [{}])[0].get("price") if isinstance(book, Mapping) else None)
                a1 = ((book.get("asks") or [{}])[0].get("price") if isinstance(book, Mapping) else None)
                last_px = book.get("last_price") if isinstance(book, Mapping) else None
                if book is not None and (b1 or a1 or last_px):
                    fresh_cnt += 1
                    lbl_px = f"买1:{b1 or '--'}/卖1:{a1 or '--'}"
                    symbol_details.append({"symbol": sym, "status": "ok", "latest_time": lbl_px, "note": f"盘口 {int(book_age or 0)}s"})
                    ob_parts.append(f"{sym} {b1 or last_px}/{a1 or last_px}")
                else:
                    d_info = day_closes_map.get(sym) or {}
                    qp = d_info.get("quote_price") or d_info.get("pre_close")
                    if qp:
                        fallback_day_cnt += 1
                        symbol_details.append({"symbol": sym, "status": "ok" if not ashare_sess["open"] else "warn", "latest_time": str(qp), "note": f"日线 {d_info.get('trade_date') or '--'}"})
                        ob_parts.append(f"{sym}:{qp}")
                    else:
                        symbol_details.append({"symbol": sym, "status": "warn", "latest_time": "--", "note": "待采集"})
            age_seconds, last_updated_at = _age_str_and_sec(latest_ob_ts, None)
            latest_sample = " · ".join(ob_parts[:4]) if ob_parts else "按实盘发单按需拉取"
            if ashare_sess["open"]:
                if fresh_cnt == len(check_syms) and fresh_cnt > 0 and (age_seconds is None or age_seconds <= 30):
                    health_status = "healthy"
                    health_label = "五档盘口实时正常"
                elif fresh_cnt > 0 or fallback_day_cnt > 0:
                    health_status = "cached_fallback"
                    health_label = "按需刷盘口/分时兜底"
                else:
                    health_status = "degraded"
                    health_label = "待实盘预热盘口"
            else:
                if fresh_cnt > 0:
                    health_status = "market_closed"
                    health_label = "收盘五档·已预热"
                elif fallback_day_cnt > 0:
                    health_status = "market_closed"
                    health_label = "休市定格·就绪"
                else:
                    health_status = "idle"
                    health_label = "实盘交易按需采集"

        elif sid == "ashare_l2_fund_flow":
            ashare_sess = sess_by_key["ashare"]
            session_state = "open" if ashare_sess["open"] else "closed"
            session_label = ashare_sess["state_label"]
            flow_syms = sorted({str(s) for b in bound for s in b["symbols"] if _SIX_DIGIT_RE.fullmatch(str(s))})
            if not bound:
                health_status = "idle"
                health_label = "当前实盘规则未挂载"
                latest_sample = "仅 A 股 L2 / 大单规则使用"
            else:
                missing_flow: list[str] = []
                parts: list[str] = []
                for sym in flow_syms:
                    has_ff = False
                    if conn is not None and lock is not None:
                        try:
                            with lock:
                                r_ff = conn.execute(
                                    """SELECT COUNT(*), MAX(minute) FROM research_points
                                       WHERE symbol=? AND trade_date=? AND (large_order_amount IS NOT NULL OR large_order_net IS NOT NULL)""",
                                    (sym, today_str),
                                ).fetchone()
                                has_ff = bool(r_ff and int(r_ff[0] or 0) > 0)
                                m_ff = str(r_ff[1] or "--") if r_ff and has_ff else "--"
                                if not has_ff:
                                    r_l2 = conn.execute(
                                        """SELECT COUNT(*), MAX(minute) FROM fund_flow_history_points
                                           WHERE symbol=? AND trade_date=?""",
                                        (sym, today_str),
                                    ).fetchone()
                                    if r_l2 and int(r_l2[0] or 0) > 0:
                                        has_ff = True
                                        m_ff = str(r_l2[1] or "--")
                        except Exception:
                            m_ff = "--"
                    else:
                        m_ff = "--"
                    if not has_ff and ashare_in_session:
                        missing_flow.append(f"{sym}无L2流")
                    parts.append(f"{sym} {m_ff}")
                    symbol_details.append({"symbol": sym, "status": "ok" if has_ff else "warn", "latest_time": m_ff, "note": "含大单流" if has_ff else "待资金流"})
                latest_sample = " · ".join(parts[:4]) if parts else "仅个股规则按需"
                if ashare_in_session and missing_flow:
                    health_status = "unhealthy" if active_bound else "degraded"
                    health_label = "资金流样本缺失"
                    error_msg = "；".join(missing_flow)
                elif not ashare_sess["open"]:
                    health_status = "market_closed"
                    health_label = "休市待命"
                else:
                    health_status = "healthy"
                    health_label = "盘中资金流正常"

        elif sid == "ice_brent_1m":
            brent_sess = sess_by_key["ice_brent"]
            session_state = "open" if brent_sess["open"] else "closed"
            session_label = brent_sess["state_label"]
            bmin, bcnt, bcap = latest_bars.get(BRENT_SYMBOL, (None, 0, None))
            bmon = mon_status.get(BRENT_SYMBOL) or {}
            bsync = _parse_iso_dt(bmon.get("last_sync_at")) or bcap
            oil_c = macro_cache.get("oil_daily_2y")
            spot_brn = (
                (mem_oil_fx[1][0] if isinstance(mem_oil_fx[1], tuple) else None)
                or (mem_dual[1].get("brn_px") if isinstance(mem_dual[1], Mapping) else None)
                or (mem_xop[1].get("brn_px") if isinstance(mem_xop[1], Mapping) else None)
                or (mem_fof[1].get("brn_px") if isinstance(mem_fof[1], Mapping) else None)
                or (mem_fof_t0[1].get("brn_px") if isinstance(mem_fof_t0[1], Mapping) else None)
                or (mem_sp_etf[1].get("brn_px") if isinstance(mem_sp_etf[1], Mapping) else None)
                or (brent_live.get("price") if isinstance(brent_live, Mapping) else None)
            )
            spot_ts = max(mem_oil_fx[0], mem_dual[0], mem_xop[0], mem_fof[0], mem_fof_t0[0], mem_sp_etf[0])
            spot_age = (now_epoch - spot_ts) if spot_ts else None
            age_seconds, last_updated_at = _age_str_and_sec(None, bsync)
            oil_days = len(oil_c[1]) if (oil_c and isinstance(oil_c[1], Sequence)) else 0
            oil_last_d = str(oil_c[1][-1][0]) if oil_days else "--"
            latest_px_txt = f"${float(spot_brn):.2f}" if spot_brn else (f"日线${oil_c[1][-1][2]:.2f}" if oil_days else "--")
            latest_sample = f"BRN0Y {bmin or bmon.get('latest_time') or '--'} ({bcnt}m) · {latest_px_txt} · 2y日线 {oil_days}条"
            wall_lag = _wall_minutes_lag(bmin, now_hhmm) if bmin else None
            brn_1m_fresh = bool(wall_lag is not None and -5 <= wall_lag <= 5 and bcnt >= 1)
            symbol_details = [
                {"symbol": "BRN0Y·1m", "status": "ok" if (brn_1m_fresh or not brent_sess["open"]) else "warn", "latest_time": bmin or "--", "note": f"{bcnt}m"},
                {"symbol": "hf_OIL", "status": "ok" if spot_brn else "warn", "latest_time": f"${float(spot_brn):.2f}" if spot_brn else "--", "note": "8s现价"},
                {"symbol": "2y日线", "status": "ok" if oil_days > 0 else "error", "latest_time": oil_last_d, "note": f"{oil_days}条"},
            ]
            brn_tel = spot_telemetry.get("live_spot_oil_fx") or {}
            if brent_sess["open"]:
                if brn_1m_fresh and oil_days > 0:
                    health_status = "healthy"
                    health_label = "电子盘实时正常"
                elif spot_brn and (spot_age is None or spot_age <= 30.0 or brent_live is not None) and oil_days > 0:
                    health_status = "cached_fallback"
                    health_label = "hf_OIL 现价接管"
                    error_msg = f"BRN0Y 1m({bmin or '空'})延迟，已切 8s 现价"
                else:
                    health_status = "unhealthy"
                    health_label = "盘中断流告警"
                    error_msg = str(bmon.get("last_error") or brn_tel.get("last_error") or "BRN0Y 分钟与 hf_OIL 现价均不可用")
            else:
                health_status = "market_closed" if oil_days > 0 else "unhealthy"
                health_label = "结算/周末休市·就绪" if oil_days > 0 else "缺少 2y 日线"

        elif sid == "ashare_peer_lof_quote":
            ashare_sess = sess_by_key["ashare"]
            session_state = "open" if ashare_sess["open"] else "closed"
            session_label = ashare_sess["state_label"]
            peer_syms = sorted(
                {str(s) for b in bound for s in b["symbols"] if _SIX_DIGIT_RE.fullmatch(str(s))} or set(_OIL_PEER_SYMBOLS)
            )
            ok_cnt = 0
            parts = []
            latest_dt_obj = None
            for sym in peer_syms:
                lmin, lcnt, lcap = latest_bars.get(sym, (None, 0, None))
                mon = mon_status.get(sym) or {}
                m_sync = _parse_iso_dt(mon.get("last_sync_at")) or lcap
                if m_sync is not None and (latest_dt_obj is None or m_sync > latest_dt_obj):
                    latest_dt_obj = m_sync
                lag_m = _trading_minutes_lag(lmin, now_hhmm) if lmin else None
                sym_ready = lcnt > 0 and ((not ashare_in_session) or (lag_m is not None and lag_m <= 5))
                if sym_ready:
                    ok_cnt += 1
                parts.append(f"{sym}:{lmin or mon.get('latest_time') or '--'}")
                symbol_details.append({"symbol": sym, "status": "ok" if sym_ready else "warn", "latest_time": lmin or "--", "note": f"{lcnt}根"})
            age_seconds, last_updated_at = _age_str_and_sec(None, latest_dt_obj)
            latest_sample = " · ".join(parts[:4])
            has_live_peer = bool(
                (isinstance(mem_dual[1], Mapping) and (now_epoch - mem_dual[0]) < 30.0)
                or (isinstance(mem_fof[1], Mapping) and (now_epoch - mem_fof[0]) < 30.0)
                or (isinstance(mem_fof_t0[1], Mapping) and (now_epoch - mem_fof_t0[0]) < 30.0)
                or (isinstance(mem_sp_etf[1], Mapping) and (now_epoch - mem_sp_etf[0]) < 30.0)
            )
            if ashare_in_session:
                if ok_cnt == len(peer_syms) and ok_cnt > 0:
                    health_status = "healthy"
                    health_label = "同业全量正常"
                elif ok_cnt > 0 or has_live_peer:
                    health_status = "cached_fallback"
                    health_label = "新浪 8s 现价接管"
                else:
                    health_status = "unhealthy"
                    health_label = "同业行情缺失"
                    error_msg = "同业油气 LOF 本地分时与新浪现价均未就绪"
            else:
                health_status = "market_closed"
                health_label = "休市定格·就绪"

        elif sid == "ine_sc0_futures":
            sc_sess = sess_by_key["ine_sc0"]
            session_state = "open" if sc_sess["open"] else "closed"
            session_label = sc_sess["state_label"]
            sc_d = macro_cache.get("sc0_daily_all")
            sc_m = macro_cache.get("sc0_min_series")
            sc_live = (
                mem_dual[1].get("sc_px")
                if isinstance(mem_dual[1], Mapping)
                else (
                    mem_fof[1].get("sc_px")
                    if isinstance(mem_fof[1], Mapping)
                    else (
                        mem_fof_t0[1].get("sc_px")
                        if isinstance(mem_fof_t0[1], Mapping)
                        else (mem_sp_etf[1].get("sc_px") if isinstance(mem_sp_etf[1], Mapping) else None)
                    )
                )
            )
            latest_ts = max(
                [x for x in (sc_d[0] if sc_d else 0.0, sc_m[0] if sc_m else 0.0, mem_dual[0], mem_fof[0], mem_fof_t0[0], mem_sp_etf[0]) if x > 0],
                default=0.0,
            )
            age_seconds, last_updated_at = _age_str_and_sec(latest_ts or None, None)
            d_cnt = len(sc_d[1]) if (sc_d and isinstance(sc_d[1], Sequence)) else 0
            m_cnt = len((sc_m[1] or {}).get("ts") or []) if (sc_m and isinstance(sc_m[1], Mapping)) else 0
            px_show = f"{float(sc_live):.2f}" if sc_live else (f"{sc_d[1][-1][2]:.2f}" if d_cnt else "--")
            latest_sample = f"SC0 {px_show} · 5m/15m序列 {m_cnt}点 · 日线 {d_cnt}条"
            symbol_details = [
                {"symbol": "nf_SC0", "status": "ok" if sc_live else "warn", "latest_time": px_show, "note": "8s现价"},
                {"symbol": "分时序列", "status": "ok" if m_cnt > 0 else "warn", "latest_time": f"{m_cnt}点", "note": "5m缓存"},
                {"symbol": "SC0日线", "status": "ok" if d_cnt > 0 else "error", "latest_time": str(sc_d[1][-1][0]) if d_cnt else "--", "note": f"{d_cnt}条"},
            ]
            if d_cnt > 0 or m_cnt > 0 or sc_live:
                if sc_sess["open"] and not sc_live and (not sc_m or (now_epoch - sc_m[0]) > 360.0):
                    health_status = "cached_fallback"
                    health_label = "5m缓存/日线兜底"
                elif sc_sess["open"]:
                    health_status = "healthy"
                    health_label = "盘中连续正常"
                else:
                    health_status = "market_closed"
                    health_label = "休市定格·就绪"
            else:
                health_status = "unhealthy"
                health_label = "缺少 SC0 缓存"
                error_msg = "尚未缓存 sc0_daily_all / sc0_min_series"

        elif sid == "us_oil_etf_uso_bno_dbo":
            us_sess = sess_by_key["us_etf"]
            session_state = "open" if us_sess["open"] else "closed"
            session_label = us_sess["state_label"]
            uso_c = macro_cache.get("uso_daily_2y")
            bno_c = macro_cache.get("bno_daily_2y")
            dbo_c = macro_cache.get("dbo_daily_2y")
            latest_ts = max(
                [x[0] for x in (uso_c, bno_c, dbo_c) if x] + [mem_dual[0], mem_fof[0], mem_fof_t0[0]],
                default=0.0,
            )
            age_seconds, last_updated_at = _age_str_and_sec(latest_ts or None, None)
            u_cnt = len(uso_c[1]) if (uso_c and isinstance(uso_c[1], Sequence)) else 0
            b_cnt = len(bno_c[1]) if (bno_c and isinstance(bno_c[1], Sequence)) else 0
            d_cnt = len(dbo_c[1]) if (dbo_c and isinstance(dbo_c[1], Sequence)) else 0

            def _us_etf_latest(sym_key: str, c_obj: Any, c_len: int) -> tuple[str, float | None]:
                row_d = str(c_obj[1][-1][0]) if c_len > 0 else ""
                row_px = float(c_obj[1][-1][2]) if c_len > 0 else None
                for bundle in (mem_fof_t0[1], mem_fof[1], mem_dual[1]):
                    if isinstance(bundle, Mapping):
                        b_d = str(bundle.get(f"{sym_key}_reg_date") or "").strip()
                        b_px = bundle.get(f"{sym_key}_close")
                        if _DATE_RE.fullmatch(b_d) and b_px and (not row_d or b_d >= row_d):
                            row_d = b_d
                            row_px = float(b_px)
                return row_d, row_px

            u_d, u_px = _us_etf_latest("uso", uso_c, u_cnt)
            b_d, b_px = _us_etf_latest("bno", bno_c, b_cnt)
            d_d, d_px = _us_etf_latest("dbo", dbo_c, d_cnt)
            u_last = f"USO:{u_d or '--'}"
            b_last = f"BNO:{b_d or '--'}"
            d_last = f"DBO:{d_d or '--'}"
            latest_sample = f"{u_last} · {b_last} · {d_last}"
            symbol_details = [
                {
                    "symbol": lbl,
                    "status": "ok" if c_len > 0 else "warn",
                    "latest_time": f"{d_str}(${float(px_v):.2f})" if (d_str and px_v is not None) else "--",
                    "note": f"{c_len}日",
                }
                for lbl, c_len, d_str, px_v in (
                    ("USO", u_cnt, u_d, u_px),
                    ("BNO", b_cnt, b_d, b_px),
                    ("DBO", d_cnt, d_d, d_px),
                )
            ]
            dbo_required = any("DBO" in b["consumed_symbols"] for b in bound)
            if u_cnt > 0 and b_cnt > 0 and d_cnt > 0:
                health_status = "healthy" if us_sess["open"] else "market_closed"
                health_label = "盘中更新" if us_sess["open"] else "收盘定格·就绪"
            elif u_cnt > 0 and b_cnt > 0:
                health_status = "cached_fallback"
                health_label = "USO/BNO已就绪(缺DBO)" if dbo_required else "USO/BNO已就绪(DBO按需)"
                if dbo_required:
                    error_msg = "160723 FOF 依赖的 DBO 日线尚未缓存"
            else:
                health_status = "unhealthy"
                health_label = "美油 ETF 日线缺失"
                error_msg = "USO 或 BNO 日线尚未缓存"

        elif sid == "us_ep_xop_crack_macro":
            us_sess = sess_by_key["us_etf"]
            session_state = "open" if us_sess["open"] else "closed"
            session_label = us_sess["state_label"]
            etf_keys = (
                ("XOP", "xop_daily_2y"),
                ("XLE", "xle_daily_2y"),
                ("SPY", "spy_daily_2y"),
                ("IWM", "iwm_daily_2y"),
            )
            present_etf = {sym: macro_cache.get(ck) for sym, ck in etf_keys if macro_cache.get(ck) is not None}
            latest_ts = max([v[0] for v in present_etf.values()], default=0.0)
            if mem_xop[0] > latest_ts:
                latest_ts = mem_xop[0]
            if mem_fof_t0[0] > latest_ts:
                latest_ts = mem_fof_t0[0]
            if mem_sp_etf[0] > latest_ts:
                latest_ts = mem_sp_etf[0]
            age_seconds, last_updated_at = _age_str_and_sec(latest_ts or None, None)
            xop_c = present_etf.get("XOP")
            xop_d = str(xop_c[1][-1][0]) if (xop_c and isinstance(xop_c[1], Sequence) and xop_c[1]) else ""
            lagging_etfs: list[str] = []
            for sym, _ in etf_keys:
                c_obj = present_etf.get(sym)
                rows_list = c_obj[1] if (c_obj and isinstance(c_obj[1], Sequence)) else []
                last_d = str(rows_list[-1][0]) if rows_list else ""
                last_px = float(rows_list[-1][2]) if rows_list else None
                live_close = (
                    (mem_xop[1].get(f"{sym.lower()}_close") if isinstance(mem_xop[1], Mapping) else None)
                    or (mem_fof_t0[1].get(f"{sym.lower()}_close") if isinstance(mem_fof_t0[1], Mapping) else None)
                    or (mem_sp_etf[1].get(f"{sym.lower()}_close") if isinstance(mem_sp_etf[1], Mapping) else None)
                )
                eff_px = live_close or last_px
                is_lag = (not last_d) or (bool(xop_d) and last_d < xop_d)
                if is_lag:
                    lagging_etfs.append(sym)
                symbol_details.append(
                    {
                        "symbol": sym,
                        "status": "warn" if is_lag else "ok",
                        "latest_time": f"{last_d or '--'}(${float(eff_px):.2f})" if eff_px else (last_d or "--"),
                        "note": "滞后XOP" if is_lag else f"{len(rows_list)}日",
                    }
                )
            xop_ext = (
                (mem_xop[1].get("xop_ext_chg_pct") if isinstance(mem_xop[1], Mapping) else None)
                or (mem_sp_etf[1].get("xop_ext_chg_pct") if isinstance(mem_sp_etf[1], Mapping) else None)
            )
            ext_txt = f" · 盘后{float(xop_ext):+.2f}%" if xop_ext is not None else ""
            latest_sample = f"XOP {xop_d or '--'}{ext_txt} · 已缓存 {len(present_etf)}/4 ETF日线"
            if xop_c and len(present_etf) == 4 and not lagging_etfs:
                health_status = "healthy" if us_sess["open"] else "market_closed"
                health_label = "盘中更新" if us_sess["open"] else "收盘定格·4只ETF对齐"
            elif xop_c:
                health_status = "cached_fallback"
                health_label = f"部分ETF滞后/按需 ({','.join(lagging_etfs)})" if lagging_etfs else "部分ETF已缓存"
                if lagging_etfs:
                    error_msg = f"{'/'.join(lagging_etfs)} 日线滞后于 XOP ({xop_d}) 或尚未缓存"
            else:
                health_status = "unhealthy"
                health_label = "美股 XOP 日线缺失"
                error_msg = "xop_daily_2y 尚未缓存"

        elif sid == "cme_energy_macro_futures":
            cme_sess = sess_by_key["cme_futures"]
            session_state = "open" if cme_sess["open"] else "closed"
            session_label = cme_sess["state_label"]
            fut_daily_keys = ("cl_daily_2y", "ho_daily_2y", "ng_daily_2y")
            fut_1m_keys = ("fut_1m_cl", "fut_1m_ho", "fut_1m_ng", "fut_1m_es")
            present_all = {k: macro_cache.get(k) for k in (*fut_daily_keys, *fut_1m_keys) if macro_cache.get(k) is not None}
            if "cl_daily_2y" not in present_all and macro_cache.get("wti_daily_2y") is not None:
                present_all["cl_daily_2y"] = macro_cache.get("wti_daily_2y")
            latest_ts = max([*[v[0] for v in present_all.values()], mem_xop[0], mem_fof[0], mem_fof_t0[0], mem_sp_etf[0]], default=0.0)
            age_seconds, last_updated_at = _age_str_and_sec(latest_ts or None, None)

            cl_live = (
                (mem_xop[1].get("cl_px") if isinstance(mem_xop[1], Mapping) else None)
                or (mem_fof[1].get("cl_px") if isinstance(mem_fof[1], Mapping) else None)
                or (mem_fof_t0[1].get("cl_px") if isinstance(mem_fof_t0[1], Mapping) else None)
                or (mem_sp_etf[1].get("cl_px") if isinstance(mem_sp_etf[1], Mapping) else None)
            )
            ho_live = (
                (mem_xop[1].get("ho_px") if isinstance(mem_xop[1], Mapping) else None)
                or (mem_fof_t0[1].get("ho_px") if isinstance(mem_fof_t0[1], Mapping) else None)
                or (mem_sp_etf[1].get("ho_px") if isinstance(mem_sp_etf[1], Mapping) else None)
            )
            ng_live = (
                (mem_xop[1].get("ng_px") if isinstance(mem_xop[1], Mapping) else None)
                or (mem_sp_etf[1].get("ng_px") if isinstance(mem_sp_etf[1], Mapping) else None)
            )
            es_live = (
                (mem_xop[1].get("es_px") if isinstance(mem_xop[1], Mapping) else None)
                or (mem_sp_etf[1].get("es_px") if isinstance(mem_sp_etf[1], Mapping) else None)
            )
            live_by_fut = {"CL": cl_live, "HO": ho_live, "NG": ng_live, "ES": es_live}
            missing_1m: list[str] = []
            for sym_f, d_key, m_key in (
                ("CL", "cl_daily_2y", "fut_1m_cl"),
                ("HO", "ho_daily_2y", "fut_1m_ho"),
                ("NG", "ng_daily_2y", "fut_1m_ng"),
                ("ES", None, "fut_1m_es"),
            ):
                m_c = present_all.get(m_key)
                d_c = present_all.get(d_key) if d_key else None
                m_px = ((m_c[1] or {}).get("px") or [None])[-1] if (m_c and isinstance(m_c[1], Mapping)) else None
                d_px = d_c[1][-1][2] if (d_c and isinstance(d_c[1], Sequence) and d_c[1]) else None
                eff_px = live_by_fut[sym_f] or m_px or d_px
                m_cnt = len(((m_c[1] or {}).get("ts") or [])) if (m_c and isinstance(m_c[1], Mapping)) else 0
                has_stream = bool(m_cnt > 0 or live_by_fut[sym_f])
                if not has_stream:
                    missing_1m.append(sym_f)
                px_fmt = f"${float(eff_px):.2f}" if eff_px and sym_f != "ES" else (f"{float(eff_px):.1f}" if eff_px else "--")
                symbol_details.append(
                    {
                        "symbol": sym_f,
                        "status": "ok" if has_stream else "warn",
                        "latest_time": px_fmt,
                        "note": f"1m:{m_cnt}条" if m_cnt else ("日线" if d_px else "缺失"),
                    }
                )

            xop_c = macro_cache.get("xop_daily_2y")
            holiday_degraded = False
            if is_tday and ("09:20" <= now_hhmm <= "15:00") and xop_c and isinstance(xop_c[1], Sequence) and xop_c[1]:
                last_us_td = str(xop_c[1][-1][0])
                if _DATE_RE.fullmatch(last_us_td):
                    approx_us_close = datetime.strptime(last_us_td, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + timedelta(days=1, hours=5)
                    if (local_now - approx_us_close).total_seconds() > 24 * 3600:
                        holiday_degraded = True

            cl_show = f"${float(cl_live):.2f}" if cl_live else (
                f"${float(present_all['cl_daily_2y'][1][-1][2]):.2f}"
                if ("cl_daily_2y" in present_all and present_all["cl_daily_2y"][1])
                else "--"
            )
            latest_sample = f"CL {cl_show} · 日线 {sum(1 for k in fut_daily_keys if k in present_all)}/3 · 1m {sum(1 for k in fut_1m_keys if k in present_all)}/4"
            xop_tel = spot_telemetry.get("live_spot_xop_bundle") or {}
            if holiday_degraded:
                health_status = "cached_fallback"
                health_label = "美股休市·>12h基准退化"
                error_msg = "美股休市次日距上个美股收盘超 24h，亚盘期货日内锚退化为 BRN0Y 主导"
            elif cme_sess["open"]:
                if len(present_all) >= 6 and not missing_1m:
                    health_status = "healthy"
                    health_label = "CME 23h实时正常"
                elif present_all or any(v is not None for v in live_by_fut.values()):
                    health_status = "cached_fallback"
                    health_label = f"部分期货降级 ({','.join(missing_1m)})" if missing_1m else "部分子序列缓存"
                    if xop_tel.get("last_error"):
                        error_msg = f"XOP/期货实时包报错: {xop_tel['last_error']}"
                else:
                    health_status = "unhealthy"
                    health_label = "CME 期货缓存缺失"
                    error_msg = str(xop_tel.get("last_error") or "CL/HO/NG/ES 日线与 1m 缓存均为空")
            else:
                health_status = "market_closed" if present_all else "unhealthy"
                health_label = "维护/周末休市·就绪" if present_all else "缺少 CME 期货缓存"

        elif sid == "fx_usdcny_usdcnh":
            fx_sess = sess_by_key["fx"]
            session_state = "open" if fx_sess["open"] else "closed"
            session_label = fx_sess["state_label"]
            cny_c = macro_cache.get("fx_usdcny_series")
            cnh_c = macro_cache.get("fx_usdcnh_series")
            spot_cny = (
                (mem_oil_fx[1][1] if isinstance(mem_oil_fx[1], tuple) else None)
                or (mem_dual[1].get("fx_px") if isinstance(mem_dual[1], Mapping) else None)
                or (mem_fof[1].get("fx_cny_px") if isinstance(mem_fof[1], Mapping) else None)
                or (mem_fof_t0[1].get("fx_cny_px") if isinstance(mem_fof_t0[1], Mapping) else None)
                or (mem_xop[1].get("fx_cny_px") if isinstance(mem_xop[1], Mapping) else None)
                or (mem_sp_etf[1].get("fx_cny_px") if isinstance(mem_sp_etf[1], Mapping) else None)
            )
            spot_cnh = (
                (mem_fof[1].get("fx_cnh_px") if isinstance(mem_fof[1], Mapping) else None)
                or (mem_fof[1].get("fx_px") if isinstance(mem_fof[1], Mapping) else None)
                or (mem_fof_t0[1].get("fx_cnh_px") if isinstance(mem_fof_t0[1], Mapping) else None)
                or (mem_fof_t0[1].get("fx_px") if isinstance(mem_fof_t0[1], Mapping) else None)
                or (mem_xop[1].get("fx_cnh_px") if isinstance(mem_xop[1], Mapping) else None)
                or (mem_sp_etf[1].get("fx_cnh_px") if isinstance(mem_sp_etf[1], Mapping) else None)
            )
            latest_ts = max(
                [x for x in (cny_c[0] if cny_c else 0.0, cnh_c[0] if cnh_c else 0.0, mem_oil_fx[0], mem_dual[0], mem_xop[0], mem_fof[0], mem_fof_t0[0], mem_sp_etf[0]) if x > 0],
                default=0.0,
            )
            age_seconds, last_updated_at = _age_str_and_sec(latest_ts or None, None)
            cny_val = spot_cny or ((cny_c[1].get("px") or [None])[-1] if (cny_c and isinstance(cny_c[1], Mapping)) else None)
            cnh_val = spot_cnh or ((cnh_c[1].get("px") or [None])[-1] if (cnh_c and isinstance(cnh_c[1], Mapping)) else None)
            latest_sample = f"USD/CNY {f'{float(cny_val):.4f}' if cny_val else '--'} · USD/CNH {f'{float(cnh_val):.4f}' if cnh_val else '--'}"
            symbol_details = [
                {"symbol": "USDCNY", "status": "ok" if cny_val else "warn", "latest_time": f"{float(cny_val):.4f}" if cny_val else "--", "note": "在岸"},
                {"symbol": "USDCNH", "status": "ok" if cnh_val else "warn", "latest_time": f"{float(cnh_val):.4f}" if cnh_val else "--", "note": "离岸24h"},
            ]
            if cny_c and (spot_cny or cnh_c):
                health_status = "healthy" if fx_sess["open"] else "market_closed"
                health_label = "在岸+离岸正常" if fx_sess["open"] else "休市定格·就绪"
            elif cny_c or spot_cny or spot_cnh:
                health_status = "cached_fallback"
                health_label = "单通道序列兜底"
            else:
                health_status = "unhealthy"
                health_label = "汇率序列缺失"
                error_msg = "fx_usdcny_series 与实时汇率均为空"

        elif sid == "eastmoney_fund_nav":
            session_state = "closed"
            session_label = "盘前静态定格"
            nav_keys = sorted(k for k in macro_cache if k.startswith("nav_"))
            latest_ts = max([macro_cache[k][0] for k in nav_keys], default=0.0)
            age_seconds, last_updated_at = _age_str_and_sec(latest_ts or None, None)
            parts = []
            lagging_navs: list[str] = []
            for nk in nav_keys[:6]:
                sym = nk[4:]
                m = macro_cache[nk][1] if isinstance(macro_cache[nk][1], Mapping) else {}
                last_d = max(m.keys()) if m else "--"
                last_v = m.get(last_d) if last_d != "--" else None
                lag_days = 0
                if _DATE_RE.fullmatch(str(last_d)):
                    try:
                        lag_days = (local_now.date() - datetime.strptime(str(last_d), "%Y%m%d").date()).days
                    except ValueError:
                        lag_days = 0
                is_stale_nav = (not last_v) or (lag_days > 5)
                if is_stale_nav:
                    lagging_navs.append(f"{sym}({last_d})")
                parts.append(f"{sym}:{last_d}({last_v:.4f})" if last_v else f"{sym}:--")
                symbol_details.append(
                    {
                        "symbol": sym,
                        "status": "warn" if is_stale_nav else "ok",
                        "latest_time": last_d,
                        "note": f"{len(m)}日·滞后{lag_days}天" if lag_days > 5 else f"{len(m)}日净值",
                    }
                )
            latest_sample = " · ".join(parts) if parts else "尚未缓存官方净值"
            if not nav_keys:
                health_status = "unhealthy"
                health_label = "净值缓存为空"
                error_msg = "尚未从天天基金加载历史官方净值"
            elif lagging_navs:
                health_status = "degraded"
                health_label = "净值披露滞后 >5日"
                error_msg = f"净值滞后超过 5 个自然日: {', '.join(lagging_navs)}"
            else:
                health_status = "market_closed"
                health_label = f"已缓存 {len(nav_keys)} 只基金·就绪"

        elif sid == "qdii_premium_panel_feed":
            ashare_sess = sess_by_key["ashare"]
            session_state = "open" if ashare_sess["open"] else "closed"
            session_label = "15s 面板估值" if ashare_sess["open"] else "最近估值快照"
            snap, snap_dt = _read_premium_snapshot(research_store)
            active_bound = [{"rule_id": f"live-{s}", "active_in_live_trade": True} for s in premium_live_symbols] if premium_live_symbols else []
            rows_p = [r for r in ((snap or {}).get("rows") or []) if isinstance(r, Mapping)]
            age_seconds, last_updated_at = _age_str_and_sec(None, snap_dt)
            valid_rows: list[Mapping[str, Any]] = []
            stale_codes: list[str] = []
            prem_160723 = "--"
            prem_162411 = "--"
            prem_501018 = "--"
            for r in rows_p:
                code = str(r.get("code") or "").strip()
                prem_r = r.get("premium_rate")
                is_stale_row = bool(r.get("stale")) or bool(r.get("error")) or (prem_r is None)
                if code == "160723" and prem_r is not None:
                    prem_160723 = f"{float(prem_r) * 100:+.2f}%"
                elif code == "162411" and prem_r is not None:
                    prem_162411 = f"{float(prem_r) * 100:+.2f}%"
                elif code == "501018" and prem_r is not None:
                    prem_501018 = f"{float(prem_r) * 100:+.2f}%"
                if is_stale_row:
                    stale_codes.append(code)
                else:
                    valid_rows.append(r)
                symbol_details.append(
                    {
                        "symbol": code,
                        "status": "warn" if is_stale_row else "ok",
                        "latest_time": f"{float(prem_r) * 100:+.2f}%" if prem_r is not None else "--",
                        "note": str(r.get("error") or r.get("estimate_source") or "ok"),
                    }
                )
            ok_cnt = len(valid_rows)
            tot_cnt = len(rows_p)
            latest_sample = (
                f"{ok_cnt}/{tot_cnt}只有效 · 160723 {prem_160723} · 162411 {prem_162411} · 501018 {prem_501018}"
                if tot_cnt > 0
                else "尚未生成 premium_snapshot"
            )
            if tot_cnt == 0:
                health_status = "unhealthy" if premium_live_symbols else "degraded"
                health_label = "暂无溢价快照"
                error_msg = "research_settings.premium_snapshot 为空"
            elif ashare_in_session and ((age_seconds is not None and age_seconds > 180) or ok_cnt == 0):
                health_status = "unhealthy" if premium_live_symbols else "degraded"
                health_label = "估值快照滞后"
                error_msg = f"最近快照已过 {age_seconds or 0}s，有效 {ok_cnt}/{tot_cnt}"
            elif stale_codes:
                health_status = "cached_fallback"
                health_label = f"{ok_cnt}/{tot_cnt}只有效 ({len(stale_codes)}只代理异常)"
                error_msg = f"{','.join(stale_codes)} 代理估价失败 (PREMIUM_SOURCE_UNAVAILABLE)"
            elif ashare_sess["open"]:
                health_status = "healthy"
                health_label = f"全量 {ok_cnt}/{tot_cnt} 实时正常"
            else:
                health_status = "market_closed"
                health_label = f"收盘快照 {ok_cnt}/{tot_cnt} 就绪"

        elif sid == "ashare_etf_redemption":
            session_state = "closed"
            session_label = "T-1 盘后更新"
            etf_db_path = getattr(getattr(research_store, "path", None), "parent", None)
            etf_exists = bool(etf_db_path and (etf_db_path / "etf-fund-flow.db").is_file())
            latest_sample = "etf-fund-flow.db 已挂载（严格 T-1）" if etf_exists else "etf-fund-flow.db 未挂载"
            if not bound:
                health_status = "idle"
                health_label = "当前无策略挂载"
            else:
                health_status = "market_closed" if etf_exists else "degraded"
                health_label = "T-1 库就绪" if etf_exists else "缺少 ETF 申赎库"

        sources_out.append(
            {
                **meta,
                "health_status": health_status,
                "health_label": health_label,
                "session_state": session_state,
                "session_label": session_label,
                "last_updated_at": last_updated_at,
                "age_seconds": age_seconds,
                "latest_sample": latest_sample,
                "error": error_msg,
                "active_in_live_trade": bool(active_bound),
                "bound_rules": bound,
                "symbol_details": symbol_details,
            }
        )

    src_health_map = {s["id"]: s for s in sources_out}
    last_guards = (
        dict(getattr(auto_trade_engine, "_last_data_guard_by_symbol", {}) or {})
        if auto_trade_engine is not None
        else {}
    )

    rules_out: list[dict[str, Any]] = []
    guarded_rules_count = 0
    ready_rules_count = 0
    for rule in rules:
        rid = str(rule["id"])
        ps = rule.get("position_sizing") if isinstance(rule.get("position_sizing"), Mapping) else {}
        live_syms = live_rule_bindings.get(rid, [])
        ds_list = rule_source_deps.get(rid, [])
        blocking: list[str] = []
        fallback_used: list[str] = []
        for ds in ds_list:
            sid = str(ds.get("id") or "")
            sh = src_health_map.get(sid)
            if not sh:
                continue
            if ds.get("required_in_session", True) and sh["health_status"] == "unhealthy":
                blocking.append(sid)
            elif sh["health_status"] == "cached_fallback":
                fallback_used.append(sid)
        live_guard_blocks = [
            last_guards[sym]["reason"]
            for sym in live_syms
            if sym in last_guards and last_guards[sym].get("blocked")
        ]
        target_sym = rule.get("target_symbol") or (rule.get("symbols") or ["160723"])[0]
        primary_src_ok = ("ashare_intraday_1m" not in blocking) and (
            (src_health_map.get("ashare_intraday_1m") or {}).get("health_status") == "healthy"
        )
        rule_same_day_t0 = resolve_rule_same_day_t0(str(target_sym), ps)
        allow_prot = bool(
            (blocking or live_guard_blocks)
            and rule_same_day_t0
            and (
                any(bool((last_guards.get(sym) or {}).get("allow_protective_exit")) for sym in live_syms)
                or (not live_guard_blocks and primary_src_ok)
            )
        )
        if blocking or live_guard_blocks:
            guard_status = "blocked"
            guard_reason = "；".join(live_guard_blocks) or f"缺失必需数据源: {', '.join(blocking)}"
            if allow_prot and "保留日内T+0硬止损" not in guard_reason:
                guard_reason = (
                    f"{guard_reason}（已严禁新开仓；A股1m主源正常，"
                    f"保留日内T+0硬止损与{str(ps.get('close_flatten_time') or '14:53')}避险平T通道）"
                )
            guarded_rules_count += 1
        elif fallback_used:
            guard_status = "fallback_ok"
            guard_reason = f"辅助源切换备援通道 ({', '.join(fallback_used)})，策略可正常执行"
            ready_rules_count += 1
        elif ashare_in_session:
            guard_status = "ready"
            guard_reason = "盘中全部数据源实时正常"
            ready_rules_count += 1
        else:
            guard_status = "standby_closed"
            guard_reason = "非盘中核心时段 · 全部依赖源已就绪"
            ready_rules_count += 1

        v9_tuning = ps.get("v9_tuning") if isinstance(ps.get("v9_tuning"), Mapping) else {}
        raw_anchor = str(
            ps.get("cross_market_anchor")
            or ("brent" if ps.get("engine_profile") not in {None, "standard"} else "")
        ).strip()
        if bool(v9_tuning.get("multi_oil_plunge_mode")) and raw_anchor in {"", "brent"}:
            raw_anchor = "brent_multi_oil_plunge"
        rules_out.append(
            {
                "id": rid,
                "name": rule["name"],
                "short_label": _format_rule_short_label(rid, str(rule.get("name") or ""), str(target_sym)),
                "source": rule["source"],
                "symbols": list(rule.get("symbols") or []),
                "target_symbol": target_sym,
                "engine_profile": str(ps.get("engine_profile") or "standard"),
                "cross_market_anchor": raw_anchor,
                "active_in_live_trade": bool(live_syms),
                "live_symbols": live_syms,
                "guard_status": guard_status,
                "guard_reason": guard_reason,
                "blocking_sources": blocking,
                "allow_protective_exit": allow_prot,
                "usage_tags": derive_rule_usage_tags(rule),
                "data_sources": ds_list,
            }
        )

    counts = {
        "total_sources": len(sources_out),
        "healthy_count": sum(1 for s in sources_out if s["health_status"] == "healthy"),
        "fallback_count": sum(1 for s in sources_out if s["health_status"] == "cached_fallback"),
        "closed_ready_count": sum(1 for s in sources_out if s["health_status"] == "market_closed"),
        "degraded_count": sum(1 for s in sources_out if s["health_status"] == "degraded"),
        "unhealthy_count": sum(1 for s in sources_out if s["health_status"] == "unhealthy"),
        "idle_count": sum(1 for s in sources_out if s["health_status"] == "idle"),
        "total_rules": len(rules_out),
        "ready_rules_count": ready_rules_count,
        "guarded_rules_count": guarded_rules_count,
        "active_live_symbols": active_live_symbols,
    }

    return {
        "evaluated_at": local_now.isoformat(timespec="seconds"),
        "probe_executed": probe_executed,
        "probe_latency_ms": probe_latency_ms,
        "in_ashare_session": ashare_in_session,
        "market_sessions": sessions,
        "summary": counts,
        "sources": sources_out,
        "rules": rules_out,
        "live_guards": last_guards,
    }
