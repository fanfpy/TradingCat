#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
长桥 API 客户端 — 中文版
========================

认证方式：
  - 仅使用长桥 Python SDK 的 Legacy API Key 三凭证
  - 从进程环境或项目根目录 .env 读取，不读取 CLI OAuth token

使用方式：
  from longbridge_client import LongbridgeClient
  
  client = LongbridgeClient()              # 自动识别环境并认证
  data = client.quote("TSLA.US")           # 单行情
  datas = client.quotes(["TSLA.US", "NVDA.US"])  # 多路行情
  pos = client.positions()                 # 持仓
  orders = client.orders()                 # 订单
  cash = client.cash_flow()                # 资金流水
  result = client.buy("TSLA.US", 10, price=415.50)   # 买入
  result = client.sell("SPCX.US", 10, trigger_price=152, stop_loss=True)  # 止损卖出
  result = client.order_cancel("ORDER_ID")  # 撤单

查帮助：
  LongbridgeClient.help()                  # 中文帮助总览
  LongbridgeClient.help("buy")             # 买卖帮助
  LongbridgeClient.help("stop_loss")       # 止损帮助
"""

import os
import sys
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from decimal import Decimal

# ============ 环境自适应层 ============

class EnvironmentAdapter:
    """加载 SDK API Key 凭证；绝不读取 CLI OAuth token。"""

    CREDENTIAL_KEYS = (
        "LONGBRIDGE_APP_KEY",
        "LONGBRIDGE_APP_SECRET",
        "LONGBRIDGE_ACCESS_TOKEN",
    )
    QUOTE_CREDENTIAL_KEYS = tuple(k.replace("LONGBRIDGE_", "LONGBRIDGE_QUOTE_")
                                  for k in CREDENTIAL_KEYS)
    TRADE_CREDENTIAL_KEYS = tuple(k.replace("LONGBRIDGE_", "LONGBRIDGE_TRADE_")
                                  for k in CREDENTIAL_KEYS)
    SDK_OPTION_KEYS = (
        "LONGBRIDGE_LANGUAGE",
        "LONGBRIDGE_HTTP_URL",
        "LONGBRIDGE_QUOTE_WS_URL",
        "LONGBRIDGE_TRADE_WS_URL",
        "LONGBRIDGE_ENABLE_OVERNIGHT",
        "LONGBRIDGE_PUSH_CANDLESTICK_MODE",
        "LONGBRIDGE_PRINT_QUOTE_PACKAGES",
        "LONGBRIDGE_LOG_PATH",
        "LONGBRIDGE_PAPERTRADING",
    )
    ENV_KEYS = (CREDENTIAL_KEYS + QUOTE_CREDENTIAL_KEYS + TRADE_CREDENTIAL_KEYS
                + SDK_OPTION_KEYS + ("TRADINGCAT_REQUIRE_SEPARATE_CREDENTIALS",))
    
    @staticmethod
    def detect() -> str:
        """检测运行环境: "docker" / "local" """
        if os.path.exists("/.dockerenv") or os.environ.get("DOCKER_CONTAINER"):
            return "docker"
        return "local"
    
    @staticmethod
    def load_credentials(env_file: Optional[str] = None,
                         scope: str = "both") -> dict:
        """从环境变量或 .env 加载 SDK 三凭证。

        优先级：已导出的环境变量 > ``TRADINGCAT_ENV_FILE`` > 项目根目录
        ``.env``。传入 ``env_file`` 主要用于测试或嵌入式调用。这里只读取三
        个明确的凭证字段，不扫描 ``~/.longbridge/openapi/tokens``，从而不会
        把 OAuth access token 误当成 SDK 的 Legacy API Key token。
        """
        selected = env_file or os.environ.get("TRADINGCAT_ENV_FILE")
        path = Path(selected).expanduser() if selected else Path(__file__).resolve().parents[1] / ".env"
        EnvironmentAdapter._load_env_file(path)
        if scope not in ("quote", "trade", "both"):
            raise ValueError("Longbridge credential scope 必须是 quote/trade/both")
        prefix = "" if scope == "both" else f"{scope.upper()}_"
        scoped = {
            "app_key": os.environ.get(f"LONGBRIDGE_{prefix}APP_KEY", ""),
            "app_secret": os.environ.get(f"LONGBRIDGE_{prefix}APP_SECRET", ""),
            "access_token": os.environ.get(f"LONGBRIDGE_{prefix}ACCESS_TOKEN", ""),
        }
        if scope != "both" and not all(scoped.values()):
            if os.environ.get("TRADINGCAT_REQUIRE_SEPARATE_CREDENTIALS") == "1":
                raise RuntimeError(f"已要求凭证隔离，但 LONGBRIDGE_{prefix}* 三凭证不完整")
            scoped = {
                "app_key": os.environ.get("LONGBRIDGE_APP_KEY", ""),
                "app_secret": os.environ.get("LONGBRIDGE_APP_SECRET", ""),
                "access_token": os.environ.get("LONGBRIDGE_ACCESS_TOKEN", ""),
            }
        return scoped

    @staticmethod
    def _load_env_file(path: Path) -> None:
        """加载允许的凭证字段；不覆盖进程里已经设置的值。"""
        if not path.is_file():
            return
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key not in EnvironmentAdapter.ENV_KEYS:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                os.environ.setdefault(key, value)
        except OSError as exc:
            raise RuntimeError(f"无法读取长桥 SDK 配置文件 {path}: {exc}") from exc


# ============ 类型转换辅助 ============

_ORDER_TYPE_MAP = {
    "LO": "LO", "MIT": "MIT", "LIT": "LIT",
    "TSLPAMT": "TSLPAMT", "TSLPPCT": "TSLPPCT",
}

_SIDE_MAP = {"buy": "Buy", "sell": "Sell"}

_TIF_MAP = {
    "Day": "Day", "GTC": "GoodTilCanceled",
    "GTD": "GoodTilDate", "IOC": "IOC", "FOK": "FOK",
}

_RTH_MAP = {"ANY_TIME": "AnyTime", "RTH_ONLY": "RTHOnly"}


def _parse_order_type(t: Optional[str]) -> Optional[str]:
    """解析订单类型字符串 (大小写不敏感)"""
    if t is None:
        return None
    return _ORDER_TYPE_MAP.get(t.upper())


def _parse_side(side: str) -> str:
    """解析买卖方向字符串 (大小写不敏感)"""
    s = side.lower()
    if s not in _SIDE_MAP:
        raise ValueError(f"无效的买卖方向: {side}，有效值: buy, sell")
    return _SIDE_MAP[s]


def _parse_time_in_force(tif: Optional[str]) -> Optional[str]:
    """解析订单时效字符串"""
    if tif is None:
        return None
    return _TIF_MAP.get(tif.upper())


def _parse_outside_rth(rth: Optional[str]) -> Optional[str]:
    """解析盘后交易设置"""
    if rth is None:
        return None
    return _RTH_MAP.get(rth.upper())


def _clean_enum(val) -> str:
    """将 SDK 枚举值转为干净字符串
    
    示例:
        OrderType.MIT → MIT
        OrderSide.Sell → Sell
    """
    s = str(val)
    for prefix in ['OrderType.', 'OrderSide.', 'OrderStatus.', 
                   'TimeInForceType.', 'CashFlowDirection.', 'BalanceType.']:
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def _normalize_order(o) -> dict:
    """将 SDK 订单对象标准化为干净的 dict"""
    return {
        'order_id': str(o.order_id) if hasattr(o, 'order_id') else '',
        'symbol': o.symbol if hasattr(o, 'symbol') else '',
        'order_type': _clean_enum(o.order_type) if hasattr(o, 'order_type') else '',
        'side': _clean_enum(o.side) if hasattr(o, 'side') else '',
        'status': _clean_enum(o.status) if hasattr(o, 'status') else '',
        'quantity': int(o.quantity) if hasattr(o, 'quantity') else 0,
        'executed_quantity': int(o.executed_quantity) if hasattr(o, 'executed_quantity') else 0,
        'price': float(o.price) if hasattr(o, 'price') and o.price else 0.0,
        'trigger_price': float(o.trigger_price) if hasattr(o, 'trigger_price') and o.trigger_price else 0.0,
        'time_in_force': _clean_enum(o.time_in_force) if hasattr(o, 'time_in_force') and o.time_in_force else '',
        'remark': str(o.remark) if hasattr(o, 'remark') and o.remark else '',
    }


# ============ SDK 加载 ============

_SDK_AVAILABLE = False
_Config = None
_QuoteContext = None
_TradeContext = None
_OrderType = None
_OrderSide = None
_TimeInForceType = None
_OutsideRTH = None
_Market = None


def _load_sdk():
    """延迟加载 longbridge SDK"""
    global _SDK_AVAILABLE, _Config, _QuoteContext, _TradeContext
    global _OrderType, _OrderSide, _TimeInForceType, _OutsideRTH
    global _Market
    
    if _SDK_AVAILABLE:
        return True
    
    try:
        import longbridge.openapi as sdk
        from longbridge.openapi import (
            Config, QuoteContext, TradeContext,
            OrderType, OrderSide, TimeInForceType, OutsideRTH, Market,
        )
        _Config = Config
        _QuoteContext = QuoteContext
        _TradeContext = TradeContext
        _OrderType = OrderType
        _OrderSide = OrderSide
        _TimeInForceType = TimeInForceType
        _OutsideRTH = OutsideRTH
        _Market = Market
        _SDK_AVAILABLE = True
        return True
    except ImportError:
        return False


# ============ 主客户端类 ============

class LongbridgeClient:
    """长桥 API 客户端 — 中文版
    
    使用 Python SDK + Legacy API Key 三凭证，不触发 CLI OAuth 登录。
    """
    
    def __init__(self, app_key: str = None, app_secret: str = None,
                 access_token: str = None, scope: str = "both"):
        """初始化客户端，自动认证
        
        参数:
            app_key: 长桥 App Key (可空，自动从环境加载)
            app_secret: 长桥 App Secret (可空)
            access_token: 长桥 Access Token (可空)
        
        异常:
            RuntimeError: 无法认证或 SDK 未安装时抛出
        """
        if scope not in ("quote", "trade", "both"):
            raise ValueError("scope 必须是 quote/trade/both")
        self.scope = scope
        explicit_credentials = any(
            value is not None for value in (app_key, app_secret, access_token))
        creds = EnvironmentAdapter.load_credentials(scope=scope)
        self._app_key = app_key or creds.get("app_key", "")
        self._app_secret = app_secret or creds.get("app_secret", "")
        self._access_token = access_token or creds.get("access_token", "")
        
        label_prefix = "" if scope == "both" else f"{scope.upper()}_"
        missing = [name for name, value in (
            (f"LONGBRIDGE_{label_prefix}APP_KEY", self._app_key),
            (f"LONGBRIDGE_{label_prefix}APP_SECRET", self._app_secret),
            (f"LONGBRIDGE_{label_prefix}ACCESS_TOKEN", self._access_token),
        ) if not value or value.startswith("your_")]
        if missing:
            raise RuntimeError(
                "长桥 SDK API Key 凭证缺失或仍为示例值: " + ", ".join(missing) + "。\n"
                "请在项目 .env 或进程环境中设置 App Key / App Secret / Legacy Access Token；"
                "不要使用 longbridge CLI OAuth token。"
            )
        
        if not _load_sdk():
            raise RuntimeError(
                "longbridge Python SDK 未安装。请运行: pip install longbridge==0.2.74")
        
        # 项目固定 SDK 0.2.74，直接使用其 API Key 构造器，不进入 CLI/OAuth。
        config_kwargs = {
            "app_key": self._app_key,
            "app_secret": self._app_secret,
            "access_token": self._access_token,
        }
        for env_key, argument in (
            ("LONGBRIDGE_HTTP_URL", "http_url"),
            ("LONGBRIDGE_QUOTE_WS_URL", "quote_ws_url"),
            ("LONGBRIDGE_TRADE_WS_URL", "trade_ws_url"),
        ):
            if os.environ.get(env_key):
                config_kwargs[argument] = os.environ[env_key]
        self._config = _Config(**config_kwargs)
        # 最小权限：行情进程不创建 TradeContext；执行进程不创建 QuoteContext。
        self._quote_ctx = _QuoteContext(self._config) if scope in ("quote", "both") else None
        self._trade_ctx = _TradeContext(self._config) if scope in ("trade", "both") else None
        self._env = EnvironmentAdapter.detect()

    def _require_quote_ctx(self):
        if self._quote_ctx is None:
            raise RuntimeError("当前 LongbridgeClient(scope='trade') 禁止行情 API")
        return self._quote_ctx

    def _require_trade_ctx(self):
        if self._trade_ctx is None:
            raise RuntimeError("当前 LongbridgeClient(scope='quote') 禁止交易/账户 API")
        return self._trade_ctx

    # ============ 行情 ============
    
    def quote(self, symbol: str) -> Optional[dict]:
        """获取单只标的实时行情
        
        参数:
            symbol: 标的代码，如 ``"TSLA.US"`` 或 ``".VIX.US"``
        
        返回:
            行情 dict，含 symbol/name/current_price/volume/change_pct 等。失败返回 None。
        """
        try:
            data = self._quote_ctx.quote([symbol])
            if data and len(data) > 0:
                return self._normalize_quote(data[0])
            return None
        except Exception as e:
            print(f"[错误] 查询 {symbol} 行情失败: {e}", file=sys.stderr)
            return None
    
    def quotes(self, symbols: List[str]) -> List[dict]:
        """获取多只标的实时行情
        
        直接调用 SDK QuoteContext.quote。
        """
        try:
            data = self._quote_ctx.quote(symbols)
            return [self._normalize_quote(q) for q in data] if data else []
        except Exception as e:
            print(f"[错误] 批量行情查询失败: {e}", file=sys.stderr)
            return []

    def depth(self, symbol: str) -> dict:
        """获取盘口深度，返回 ``{symbol, asks, bids}``。"""
        try:
            resp = self._quote_ctx.depth(symbol)

            def normalize_levels(levels):
                return [{
                    "price": float(getattr(level, "price", 0) or 0),
                    "volume": int(getattr(level, "volume", 0) or 0),
                    "order_num": int(getattr(level, "order_num", 0) or 0),
                } for level in (levels or [])]

            return {
                "symbol": symbol,
                "asks": normalize_levels(getattr(resp, "asks", [])),
                "bids": normalize_levels(getattr(resp, "bids", [])),
            }
        except Exception as e:
            print(f"[错误] 获取 {symbol} 盘口深度失败: {e}", file=sys.stderr)
            return {}
    
    def _normalize_quote(self, q) -> dict:
        """标准化行情数据"""
        cp = float(q.last_done) if hasattr(q, 'last_done') and q.last_done else (
            float(q.prev_close) if hasattr(q, 'prev_close') else 0
        )
        return {
            "symbol": q.symbol if hasattr(q, 'symbol') else '',
            "name": q.name if hasattr(q, 'name') else '',
            "current_price": cp,
            "last": cp,  # 向后兼容：portfolio_analyzer等脚本用 q.get('last')
            "prev_close": float(q.prev_close) if hasattr(q, 'prev_close') else 0,
            "high": float(q.high) if hasattr(q, 'high') else 0,
            "low": float(q.low) if hasattr(q, 'low') else 0,
            "open": float(q.open) if hasattr(q, 'open') else 0,
            "volume": int(q.volume) if hasattr(q, 'volume') else 0,
            "turnover": float(q.turnover) if hasattr(q, 'turnover') else 0,
            "change_value": float(q.change_value) if hasattr(q, 'change_value') else 0,
            "change_pct": float(q.change_rate) if hasattr(q, 'change_rate') else 0,
            "timestamp": str(q.timestamp) if hasattr(q, 'timestamp') else '',
        }
    
    # ============ 持仓 & 资产 ============
    
    def positions(self, strict: bool = False) -> List[dict]:
        """获取当前持仓列表；strict=True 时查询失败直接抛错供交易状态同步 fail closed。"""
        try:
            resp = self._trade_ctx.stock_positions()
            positions = []
            for channel in resp.channels:
                for pos in channel.positions:
                    positions.append({
                        'symbol': pos.symbol,
                        'quantity': str(int(pos.quantity)),
                        'available_quantity': str(int(pos.available_quantity)),
                        'cost_price': str(float(pos.cost_price)),
                        'currency': pos.currency if hasattr(pos, 'currency') else 'USD',
                        'market': channel.account_channel if hasattr(channel, 'account_channel') else '',
                    })
            # 补充实时行情 (last_price, unrealized_pnl)
            if positions:
                symbols = [p['symbol'] for p in positions]
                try:
                    quotes_list = self.quotes(symbols)
                    # 转为dict (key=symbol)
                    quotes_dict = {}
                    for q in quotes_list:
                        sym = q.get('symbol', '')
                        if sym:
                            quotes_dict[sym] = q
                    for p in positions:
                        sym = p['symbol']
                        if sym in quotes_dict:
                            q = quotes_dict[sym]
                            p['last_price'] = str(float(q.get('last', 0)))
                            # 计算unrealized_pnl_pct
                            cost = float(p['cost_price'])
                            last = float(p['last_price'])
                            if cost > 0:
                                p['unrealized_pnl_pct'] = str(round((last - cost) / cost * 100, 2))
                            else:
                                p['unrealized_pnl_pct'] = '0'
                        else:
                            p['last_price'] = '0'
                            p['unrealized_pnl_pct'] = '0'
                except Exception as e:
                    print(f"[警告] 获取行情失败: {e}", file=sys.stderr)
                    for p in positions:
                        p['last_price'] = '0'
                        p['unrealized_pnl_pct'] = '0'
            return positions
        except Exception as e:
            print(f"[错误] 获取持仓失败: {e}", file=sys.stderr)
            if strict:
                raise
            return []
    
    def assets(self) -> Optional[dict]:
        """获取账户资产概览 (对应 CLI: assets)"""
        try:
            resp = self._trade_ctx.account_balance()
            if resp and len(resp) > 0:
                bal = resp[0]
                return {
                    'total_cash': str(float(bal.total_cash)) if hasattr(bal, 'total_cash') else '0',
                    'max_finance_amount': str(float(bal.max_finance_amount)) if hasattr(bal, 'max_finance_amount') else '0',
                    'net_assets': str(float(bal.net_assets)) if hasattr(bal, 'net_assets') else '0',
                    'currency': str(bal.currency) if hasattr(bal, 'currency') else 'HKD',
                }
            return None
        except Exception as e:
            print(f"[错误] 获取资产失败: {e}", file=sys.stderr)
            return None
    
    def exchange_rates(self) -> dict:
        """获取港元/美元汇率

        注意: SDK 不提供汇率 API，使用固定汇率作为 fallback。
        实际交易中请以券商汇率为准。

        返回: {"HKD/USD": float, "USD/HKD": float}
        """
        # 固定汇率 fallback (SDK 无 exchange_rate API)
        usd_per_hkd = 0.1282
        return {"HKD/USD": usd_per_hkd, "USD/HKD": round(1.0 / usd_per_hkd, 4)}
    
    def vix(self) -> Optional[float]:
        """获取 VIX 恐慌指数"""
        try:
            data = self._quote_ctx.quote([".VIX.US"])
            if data and len(data) > 0:
                return float(data[0].last_done) if data[0].last_done else None
            return None
        except Exception:
            return None
    
    def market_sentiment(self) -> dict:
        """获取市场情绪指标 (VIX + SPY/QQQ 涨跌幅)"""
        sentiment = {"vix": None, "spy_pct": 0.0, "qqq_pct": 0.0, "regime": "未知"}
        try:
            sentiment["vix"] = self.vix()
            quotes = self.quotes(["SPY.US", "QQQ.US"])
            for q in quotes:
                if q.get("symbol") == "SPY.US":
                    sentiment["spy_pct"] = q.get("change_pct", 0)
                elif q.get("symbol") == "QQQ.US":
                    sentiment["qqq_pct"] = q.get("change_pct", 0)
            
            vix = sentiment["vix"]
            if vix:
                if vix < 15:
                    sentiment["regime"] = "低波"
                elif vix < 20:
                    sentiment["regime"] = "正常"
                elif vix < 25:
                    sentiment["regime"] = "警惕"
                else:
                    sentiment["regime"] = "恐慌"
        except Exception:
            pass
        return sentiment
    
    # ============ K线数据 ============
    
    def kline(self, symbol: str, days: int = 30, period: str = "day") -> List[dict]:
        """获取历史K线数据 (对应 CLI: kline)"""
        try:
            from datetime import datetime, timedelta
            from longbridge.openapi import Period, AdjustType
            end = datetime.now()
            start = end - timedelta(days=days + 10)
            
            period_map = {
                "day": Period.Day, "week": Period.Week, "month": Period.Month,
                "1m": Period.Min_1, "5m": Period.Min_5, "15m": Period.Min_15,
                "30m": Period.Min_30, "60m": Period.Min_60,
            }
            lb_period = period_map.get(period, Period.Day)
            
            resp = self._quote_ctx.history_candlesticks_by_date(
                symbol=symbol, period=lb_period, adjust_type=AdjustType.NoAdjust,
                start=start.date(), end=end.date()
            )
            klines = []
            for c in resp:
                klines.append({
                    'timestamp': str(c.timestamp) if hasattr(c, 'timestamp') else '',
                    'open': float(c.open) if hasattr(c, 'open') else 0,
                    'high': float(c.high) if hasattr(c, 'high') else 0,
                    'low': float(c.low) if hasattr(c, 'low') else 0,
                    'close': float(c.close) if hasattr(c, 'close') else 0,
                    'volume': int(c.volume) if hasattr(c, 'volume') else 0,
                    'turnover': float(c.turnover) if hasattr(c, 'turnover') else 0,
                })
            return klines[-days:] if len(klines) > days else klines
        except Exception as e:
            print(f"[错误] 获取 {symbol} K线失败: {e}", file=sys.stderr)
            return []
    
    def kline_by_count(self, symbol: str, count: int = 260, period: str = "day",
                       adjust: str = "forward") -> List[dict]:
        """获取最近 N 根 K线（技术指标计算专用）。

        adjust: "forward"（前复权，默认）/ "none"（未复权）。
        前复权保证跨拆股/合股的价格连续，趋势指标不受跳变污染。

        count <= 1000：SDK candlesticks 一次拉取。
        count > 1000：用 SDK history_candlesticks_by_date 按年度拉取后合并去重。
        """
        try:
            from datetime import datetime, timedelta
            from longbridge.openapi import Period, AdjustType
            period_map = {
                "day": Period.Day, "week": Period.Week, "month": Period.Month,
                "1m": Period.Min_1, "5m": Period.Min_5, "15m": Period.Min_15,
                "30m": Period.Min_30, "60m": Period.Min_60,
            }
            lb_period = period_map.get(period, Period.Day)
            adj = AdjustType.ForwardAdjust if adjust == "forward" else AdjustType.NoAdjust

            if count > 1000:
                return self._kline_by_sdk_years(symbol, count, lb_period, adj, period)

            resp = self._quote_ctx.candlesticks(
                symbol=symbol, period=lb_period, count=count,
                adjust_type=adj,
            )
            resp = resp[-count:] if len(resp) > count else resp
            return [{
                'timestamp': str(c.timestamp) if hasattr(c, 'timestamp') else '',
                'open': float(c.open) if hasattr(c, 'open') else 0,
                'high': float(c.high) if hasattr(c, 'high') else 0,
                'low': float(c.low) if hasattr(c, 'low') else 0,
                'close': float(c.close) if hasattr(c, 'close') else 0,
                'volume': int(c.volume) if hasattr(c, 'volume') else 0,
                'turnover': float(c.turnover) if hasattr(c, 'turnover') else 0,
            } for c in resp]
        except Exception as e:
            print(f"[错误] 获取 {symbol} K线失败: {e}", file=sys.stderr)
            return []

    def _kline_by_sdk_years(self, symbol: str, count: int, lb_period,
                            adjust_type, period: str) -> List[dict]:
        """用 SDK 按年度拉取长历史，合并去重后取最近 count 根。"""
        import math
        from datetime import date, datetime

        bars_per_year = {"day": 250, "week": 52, "month": 12}.get(period)
        if bars_per_year is None:
            print("[错误] 超过 1000 根的 SDK 分段拉取只支持 day/week/month", file=sys.stderr)
            return []
        years_needed = int(math.ceil(count / float(bars_per_year))) + 1
        now = datetime.now()
        start_year = now.year - years_needed + 1
        if start_year < 2000:
            start_year = 2000

        by_date: Dict[str, dict] = {}
        for year in range(start_year, now.year + 1):
            try:
                resp = self._quote_ctx.history_candlesticks_by_date(
                    symbol=symbol,
                    period=lb_period,
                    adjust_type=adjust_type,
                    start=date(year, 1, 1),
                    end=date(year, 12, 31),
                )
            except Exception as e:
                print(f"[错误] SDK 拉取 {symbol} {year} K线失败: {e}", file=sys.stderr)
                continue
            for c in (resp or []):
                timestamp = str(getattr(c, "timestamp", ""))
                if not timestamp:
                    continue
                by_date[timestamp] = {
                    "timestamp": timestamp,
                    "open": float(getattr(c, "open", 0) or 0),
                    "high": float(getattr(c, "high", 0) or 0),
                    "low": float(getattr(c, "low", 0) or 0),
                    "close": float(getattr(c, "close", 0) or 0),
                    "volume": int(getattr(c, "volume", 0) or 0),
                    "turnover": float(getattr(c, "turnover", 0) or 0),
                }

        rows = [by_date[d] for d in sorted(by_date)]
        rows = rows[-count:] if len(rows) > count else rows
        if not rows:
            print(f"[错误] SDK 未拉到 {symbol} 任何K线", file=sys.stderr)
        return rows
    
    def kline_for_indicators(self, symbol: str, is_leverage: bool = False) -> Optional[List[dict]]:
        """获取用于技术指标计算的 K 线数据（260根）"""
        count = 260 if not is_leverage else 200
        return self.kline_by_count(symbol, count=count, period="day")
    
    # ============ 订单 & 交易 ============
    
    def order(self,
              side: str,
              symbol: str,
              qty: int,
              *,
              order_type: str = "LO",
              price: float = None,
              trigger_price: float = None,
              time_in_force: str = "Day",
              outside_rth: str = "ANY_TIME",
              expire_date: str = None,
              trailing_amount: float = None,
              trailing_percent: float = None,
              remark: str = "") -> Optional[dict]:
        """统一下单入口 (对应 CLI: order buy|sell)"""
        try:
            # Python enum 不支持 ClassName["MEMBER"] 下标访问，用 getattr
            order_type_enum = getattr(_OrderType, order_type.upper(), None) if _OrderType else None
            side_enum = getattr(_OrderSide, side.capitalize(), None) if _OrderSide else None
            # TimeInForceType: Day, GoodTilCanceled, GoodTilDate
            tif_map = {"Day": "Day", "GTC": "GoodTilCanceled", "GTD": "GoodTilDate"}
            tif_key = tif_map.get(time_in_force, time_in_force)
            tif_enum = getattr(_TimeInForceType, tif_key, None) if _TimeInForceType else None
            # OutsideRTH: AnyTime, RTHOnly
            rth_map = {"ANY_TIME": "AnyTime", "RTH_ONLY": "RTHOnly"}
            rth_key = rth_map.get(outside_rth.upper(), outside_rth)
            rth_enum = getattr(_OutsideRTH, rth_key, None) if _OutsideRTH else None
            
            kwargs = {
                "symbol": symbol,
                "order_type": order_type_enum,
                "side": side_enum,
                "submitted_quantity": qty,
                "time_in_force": tif_enum,
                "outside_rth": rth_enum,
                "remark": remark or "",
            }
            
            if price is not None:
                kwargs["submitted_price"] = Decimal(str(price))
            if trigger_price is not None:
                kwargs["trigger_price"] = Decimal(str(trigger_price))
            if expire_date:
                kwargs["expire_date"] = expire_date
            if trailing_amount is not None:
                kwargs["trailing_amount"] = Decimal(str(trailing_amount))
            if trailing_percent is not None:
                kwargs["trailing_percent"] = str(trailing_percent)
            
            result = self._trade_ctx.submit_order(**kwargs)
            
            order_id = str(result.order_id) if hasattr(result, 'order_id') else "未知"
            status = _clean_enum(result.status) if hasattr(result, 'status') else "Unknown"
            
            side_cn = "买入" if side.lower() == "buy" else "卖出"
            type_cn = {
                "LO": "限价", "MIT": "触价", "LIT": "触价限价",
                "TSLPAMT": "追踪止损(差额)", "TSLPPCT": "追踪止损(百分比)",
            }.get(order_type.upper(), order_type)
            
            print(f"✅ 订单提交成功!")
            print(f"  订单号:   {order_id}")
            print(f"  标的:     {symbol}")
            print(f"  方向:     {side_cn}")
            print(f"  类型:     {type_cn}")
            print(f"  数量:     {qty}")
            print(f"  状态:     {status}")
            
            return {
                "order_id": order_id,
                "symbol": symbol,
                "side": side.lower(),
                "quantity": qty,
                "order_type": order_type.upper(),
                "price": price,
                "trigger_price": trigger_price,
                "status": status,
                "success": True,
            }
            
        except Exception as e:
            print(f"❌ 订单提交失败: {e}", file=sys.stderr)
            return None
    
    def buy(self, symbol: str, qty: int, *, price: float = None,
            time_in_force: str = "Day", outside_rth: str = "ANY_TIME",
            remark: str = "") -> Optional[dict]:
        """买入 (限价单快捷方式)"""
        return self.order(
            side="buy", symbol=symbol, qty=qty,
            order_type="LO", price=price,
            time_in_force=time_in_force, outside_rth=outside_rth, remark=remark or "",
        )
    
    def sell(self, symbol: str, qty: int, *, price: float = None,
             stop_loss: bool = False, trigger_price: float = None,
             time_in_force: str = "Day", outside_rth: str = "ANY_TIME",
             remark: str = "") -> Optional[dict]:
        """卖出 (限价单快捷方式，或止损单)"""
        if stop_loss:
            return self.order(
                side="sell", symbol=symbol, qty=qty,
                order_type="MIT", trigger_price=trigger_price,
                time_in_force="GTC", outside_rth="ANY_TIME",
                remark=remark or "止损单",
            )
        else:
            return self.order(
                side="sell", symbol=symbol, qty=qty,
                order_type="LO", price=price,
                time_in_force=time_in_force, outside_rth=outside_rth, remark=remark or "",
            )
    
    def stop_loss(self, symbol: str, qty: int, trigger_price: float, *, remark: str = "") -> Optional[dict]:
        """止损单快捷方式 (MIT + GTC + AnyTime)"""
        return self.order(
            side="sell", symbol=symbol, qty=qty,
            order_type="MIT", trigger_price=float(trigger_price),
            time_in_force="GTC", outside_rth="ANY_TIME",
            remark=remark or f"止损-{symbol}",
        )

    def take_profit(self, symbol: str, qty: int, trigger_price: float, *, remark: str = "") -> Optional[dict]:
        """止盈单快捷方式"""
        return self.stop_loss(symbol, qty, trigger_price, remark=remark or f"止盈-{symbol}")
    
    def order_cancel(self, order_id: str) -> bool:
        """撤单"""
        try:
            self._trade_ctx.cancel_order(order_id)
            print("✅ 撤单成功: " + order_id)
            return True
        except Exception as e:
            print(f"❌ 撤单失败 {order_id}: {e}", file=sys.stderr)
            return False
    
    def order_query(self, order_id: str) -> Optional[dict]:
        """查询单个订单状态"""
        try:
            # 新版 OpenAPI 提供按 order_id 查询详情，优先使用以避免扫描列表。
            if hasattr(self._trade_ctx, "order_detail"):
                detail = self._trade_ctx.order_detail(order_id)
                if detail is not None:
                    return _normalize_order(detail)
            today = self._trade_ctx.today_orders()
            for o in today:
                if hasattr(o, 'order_id') and str(o.order_id) == order_id:
                    return _normalize_order(o)
            
            from datetime import datetime, timedelta
            now = datetime.now()
            start = now - timedelta(days=30)
            history = self._trade_ctx.history_orders(
                start_at=start.strftime("%Y-%m-%d %H:%M:%S"),
                end_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            )
            for o in history:
                if hasattr(o, 'order_id') and str(o.order_id) == order_id:
                    return _normalize_order(o)
            return None
        except Exception as e:
            print(f"[错误] 查询订单 {order_id} 失败: {e}", file=sys.stderr)
            return None

    def set_order_changed_callback(self, callback) -> bool:
        """注册 SDK 订单变更推送，回调参数标准化为 dict。

        底层连接仍由已认证的 Python SDK TradeContext 管理，不调用 CLI OAuth。
        返回 False 表示当前 SDK 版本不支持订单推送。
        """
        setter = getattr(self._trade_ctx, "set_on_order_changed", None)
        if setter is None:
            return False

        def _on_changed(event):
            normalized = _normalize_order(event)
            submitted_quantity = getattr(event, "submitted_quantity", None)
            if submitted_quantity is not None:
                normalized["quantity"] = int(submitted_quantity)
            for source in ("executed_price", "executed_quantity", "updated_at",
                           "last_price", "last_share", "msg"):
                value = getattr(event, source, None)
                if value is not None:
                    normalized[source] = str(value)
            callback(normalized)

        setter(_on_changed)
        return True
    
    def order_replace(self, old_order_id: str, **kwargs) -> Optional[dict]:
        """改单（撤原单 + 提交新单）"""
        old_order = self.order_query(old_order_id)
        if not old_order:
            print(f"❌ 找不到原订单 {old_order_id}", file=sys.stderr)
            return None
        
        if not self.order_cancel(old_order_id):
            return None
        
        return self.order(
            side=old_order.get("side", "sell"),
            symbol=kwargs.get("symbol", old_order.get("symbol", "")),
            qty=kwargs.get("qty", old_order.get("quantity", 0)),
            order_type=kwargs.get("order_type", old_order.get("order_type", "LO")),
            price=kwargs.get("price"),
            trigger_price=kwargs.get("trigger_price"),
            time_in_force=kwargs.get("time_in_force", old_order.get("time_in_force", "Day")),
            remark=kwargs.get("remark", ""),
        )
    
    # ============ 订单/成交查询 ============
    
    def orders(self, days: int = 1, side: str = None, symbol: str = None,
               strict: bool = False) -> List[dict]:
        """查询订单列表；strict=True 时查询失败直接抛错供交易状态同步 fail closed。"""
        all_orders = []
        try:
            if days <= 1:
                orders_list = self._trade_ctx.today_orders()
            else:
                from datetime import datetime, timedelta
                now = datetime.now()
                start = now - timedelta(days=days)
                orders_list = self._trade_ctx.history_orders(
                    start_at=start,
                    end_at=now,
                )
            
            for o in orders_list:
                od = _normalize_order(o)
                if side and od.get("side", "").lower() != side.capitalize():
                    continue
                if symbol and od.get("symbol") != symbol:
                    continue
                all_orders.append(od)
        except Exception as e:
            print(f"[错误] 查询订单失败: {e}", file=sys.stderr)
            if strict:
                raise
        
        return all_orders
    
    def executions(self, days: int = 30, symbol: str = None) -> List[dict]:
        """查询成交记录 (对应 CLI: executions)"""
        result = []
        try:
            from datetime import datetime, timedelta
            now = datetime.now()
            start = now - timedelta(days=days)
            
            # SDK 实际方法: history_executions(symbol, start_at, end_at) - 需要 datetime 对象
            resp = self._trade_ctx.history_executions(
                symbol=symbol,
                start_at=start,
                end_at=now,
            )
            
            for t in resp:
                result.append({
                    'order_id': str(t.order_id) if hasattr(t, 'order_id') else '',
                    'trade_id': str(t.trade_id) if hasattr(t, 'trade_id') else '',
                    'symbol': t.symbol if hasattr(t, 'symbol') else '',
                    'quantity': int(t.quantity) if hasattr(t, 'quantity') else 0,
                    'price': float(t.price) if hasattr(t, 'price') else 0,
                    'trade_time': str(t.trade_done_at) if hasattr(t, 'trade_done_at') else '',
                })
        except Exception as e:
            print(f"[错误] 查询成交失败: {e}", file=sys.stderr)
        
        return result
    
    def stop_orders(self, symbol: str = None) -> List[dict]:
        """查询当前活跃的 MIT 卖出止损单。

        长桥把未触发的 GTC MIT 单放在 ``today_orders`` 中，并可能将状态
        返回为 ``VarietiesNotReported``。历史订单只包含终态订单，不能用于
        判断当前保护是否存在。
        """
        active_statuses = {
            "NotSubmitted", "WaitingSubmit", "Submitted",
            "PendingNew", "PendingReplace", "PendingCancel",
            "VarietiesNotReported",
        }
        result = []
        try:
            all_orders = self._trade_ctx.today_orders(symbol=symbol)
            for o in all_orders:
                order_dict = _normalize_order(o)
                if (
                    order_dict.get("order_type") == "MIT"
                    and order_dict.get("side") == "Sell"
                    and order_dict.get("status") in active_statuses
                ):
                    result.append(order_dict)
        except Exception as e:
            print(f"[错误] 查询止损单失败: {e}", file=sys.stderr)
        return result
    
    def cash_flow(self, days: int = 30) -> List[dict]:
        """查询资金流水 (对应 CLI: cash-flow)"""
        result = []
        try:
            from datetime import datetime, timedelta
            now = datetime.now()
            start = now - timedelta(days=days)
            
            resp = self._trade_ctx.cash_flow(
                start_at=start,
                end_at=now,
            )
            for f in resp:
                result.append({
                    'name': str(f.transaction_flow_name) if hasattr(f, 'transaction_flow_name') else '',
                    'direction': _clean_enum(f.direction) if hasattr(f, 'direction') else '',
                    'business_type': _clean_enum(f.business_type) if hasattr(f, 'business_type') else '',
                    'amount': float(f.balance) if hasattr(f, 'balance') else 0,
                    'currency': str(f.currency) if hasattr(f, 'currency') else '',
                    'time': str(f.business_time) if hasattr(f, 'business_time') else '',
                    'symbol': str(f.symbol) if hasattr(f, 'symbol') else '',
                })
        except Exception as e:
            print(f"[错误] 查询资金流水失败: {e}", file=sys.stderr)
        
        return result
    
    # ============ 基本数据 ============
    
    def static_info(self, symbol: str) -> Optional[dict]:
        """获取标的基本信息
        
        参数:
            symbol: 标的代码
        
        返回:
            基本信息 dict 或 None
        """
        try:
            response = self._require_quote_ctx().static_info([symbol])
            resp = response[0] if response else None
            if resp is not None:
                return {
                    'symbol': getattr(resp, 'symbol', symbol),
                    'name': (getattr(resp, 'name_en', '') or
                             getattr(resp, 'name_cn', '') or
                             getattr(resp, 'name_hk', '')),
                    'exchange': resp.exchange if hasattr(resp, 'exchange') else '',
                    'currency': resp.currency if hasattr(resp, 'currency') else '',
                    'lot_size': int(resp.lot_size) if hasattr(resp, 'lot_size') else 0,
                    'total_shares': int(resp.total_shares) if hasattr(resp, 'total_shares') else 0,
                    'circulating_shares': int(getattr(resp, 'circulating_shares', 0) or 0),
                    'eps': float(getattr(resp, 'eps', 0) or 0),
                    'eps_ttm': float(getattr(resp, 'eps_ttm', 0) or 0),
                    'bps': float(getattr(resp, 'bps', 0) or 0),
                    # SDK 字段名为 dividend_yield，但旧版接口实际表示每股股息；
                    # 归一化后避免因子层误当成比例。
                    'dividend_per_share': float(
                        getattr(resp, 'dividend_yield', 0) or 0),
                }
            return None
        except Exception as e:
            print(f"[错误] 获取 {symbol} 基本信息失败: {e}", file=sys.stderr)
            return None

    def trading_calendar(self, market: str, start, end) -> List[dict]:
        """读取官方 SDK 交易日历，返回范围内每天的开闭市标记。"""
        market_enum = getattr(_Market, market.upper(), None) if _Market else None
        if market_enum is None:
            raise ValueError(f"不支持的市场: {market}")
        response = self._require_quote_ctx().trading_days(market_enum, start, end)
        full = (getattr(response, "trading_days", None)
                or getattr(response, "trade_days", None)
                or getattr(response, "trade_day", None) or [])
        half = (getattr(response, "half_trading_days", None)
                or getattr(response, "half_trade_days", None)
                or getattr(response, "half_trade_day", None) or [])
        full_dates = {str(item) for item in full}
        half_dates = {str(item) for item in half}
        from datetime import timedelta
        rows = []
        current = start
        while current <= end:
            key = str(current)
            rows.append({"trade_date": key,
                         "is_open": key in full_dates or key in half_dates,
                         "half_day": key in half_dates})
            current += timedelta(days=1)
        return rows

    def margin_ratio(self, symbol: str) -> Optional[dict]:
        """查询标的保证金比例
        
        参数:
            symbol: 标的代码
        
        返回:
            保证金信息 dict 或 None
        """
        try:
            resp = self._trade_ctx.margin_ratio(symbol)
            if resp:
                return {
                    'symbol': symbol,
                    'initial_margin_ratio': float(getattr(resp, 'initial_margin_ratio', 0)),
                    'maintenance_margin_ratio': float(getattr(resp, 'maintenance_margin_ratio', 0)),
                }
            return None
        except Exception as e:
            print(f"[错误] 查询保证金比例失败: {e}", file=sys.stderr)
            return None
    
    def estimate_buy_qty(self, symbol: str, price: float = None) -> Optional[dict]:
        """估算最大可买数量
        
        参数:
            symbol: 标的代码
            price: 限价价格 (可选)
        
        返回:
            {"buy_qty": int, "symbol": str, "price": float}
        """
        try:
            from longbridge.openapi import OrderType, OrderSide
            resp = self._trade_ctx.estimate_max_purchase_quantity(
                symbol=symbol,
                order_type=OrderType.LO,
                side=OrderSide.Buy,
                price=Decimal(str(price)) if price else None,
            )
            return {
                'symbol': symbol,
                'buy_qty': int(getattr(resp, 'cash_max_quantity', 0) or getattr(resp, 'max_quantity', 0)),
                'price': price,
            }
        except Exception as e:
            print(f"[错误] 估算可买数量失败: {e}", file=sys.stderr)
            return None
    
    def watchlist(self, strict: bool = False) -> List[str]:
        """获取自选股列表；strict=True 时 SDK 查询失败直接抛错。"""
        try:
            resp = self._quote_ctx.watchlist()
            symbols = []
            for group in resp:
                for security in getattr(group, 'securities', []):
                    symbols.append(security.symbol)
            return symbols
        except Exception as e:
            print(f"[错误] 获取自选股失败: {e}", file=sys.stderr)
            if strict:
                raise
            return []


    # ------------------------------------------------------------------
    # 向后兼容 (Legacy API Shims)
    # ------------------------------------------------------------------

    def get_positions(self) -> List[dict]:
        """兼容旧 API"""
        return self.positions()

    def get_assets(self) -> Optional[dict]:
        """兼容旧 API"""
        return self.assets()

    def get_exchange_rates(self) -> dict:
        """兼容旧 API"""
        return self.exchange_rates()

    def get_quotes(self, symbols: List[str]) -> dict:
        """兼容旧 API — 返回dict，key=symbol"""
        quotes_list = self.quotes(symbols)
        result = {}
        if quotes_list:
            for q in quotes_list:
                sym = q.get('symbol', '')
                if sym:
                    result[sym] = q
        return result

    def get_quote(self, symbol: str) -> Optional[dict]:
        """获取单个标的行情"""
        quotes = self.get_quotes([symbol])
        return quotes.get(symbol)

    def get_kline(self, symbol: str, days: int = 60, count: int = None, **kwargs) -> List[dict]:
        """兼容旧 API"""
        if count is not None:
            return self.kline_by_count(symbol, count=count)
        return self.kline(symbol, days=days)

    def get_kline_for_indicators(self, symbol: str, **kwargs) -> Optional[List[dict]]:
        """兼容旧 API"""
        return self.kline_for_indicators(symbol)

    def get_kline_by_count(self, symbol: str, count: int = 260, **kwargs) -> List[dict]:
        """兼容旧 API"""
        return self.kline_by_count(symbol, count=count)

    def get_stop_orders(self) -> List[dict]:
        """兼容旧 API"""
        return self.stop_orders()

    def get_stop_loss(self) -> List[dict]:
        """获取活跃止损单 (兼容旧API)。"""
        return self.stop_orders()

    def submit_stop_loss(self, symbol: str, quantity: int, trigger_price: float) -> dict:
        """提交 MIT + GTC 止损单。"""
        result = self.stop_loss(symbol, quantity, trigger_price)
        if result and result.get("success"):
            return {"success": True, "order_id": result.get("order_id", "")}
        return {"success": False, "error": "submit_order returned no successful result"}

    def get_history_orders(self, **kwargs) -> List[dict]:
        """兼容旧 API"""
        return self.orders(days=90)

    def get_vix(self) -> float:
        """获取VIX现货价格 (通过VIX.US ETF近似)"""
        try:
            quote = self.quotes(['VIX.US'])
            if quote and 'VIX.US' in quote:
                return float(quote['VIX.US'].get('last', 0))
            # 备选: 用VIX期货
            quote = self.quotes(['VIXY.US'])
            if quote and 'VIXY.US' in quote:
                return float(quote['VIXY.US'].get('last', 0))
            return 17.0  # 默认值
        except Exception:
            return 17.0  # 失败时返回中性的VIX值

    # ------------------------------------------------------------------
    # 中文帮助系统
    # ------------------------------------------------------------------

    @classmethod
    def help(cls, topic: str = None):
        """输出中文帮助

        参数:
            topic: 帮助主题
                - None: 总览
                - "buy": 买入帮助
                - "sell": 卖出帮助
                - "stop_loss": 止损帮助
                - "orders": 订单管理
                - "quote": 行情查询
                - "kline": K线数据
                - "positions": 持仓资产
                - "vix": 市场情绪
                - "local": 本地使用指南
        """
        helps = {
            None: u"""
╔══════════════════════════════════════════════════════════════╗
║          长桥 API 客户端 — 中文帮助总览                     ║
╠══════════════════════════════════════════════════════════════╣
║ SDK 认证:                                                    ║
║   项目 .env / 环境变量中的 Legacy API Key 三凭证            ║
║                                                              ║
║ 行情:                                                       ║
║   quote(symbol)            单只行情 (CLI: quote)            ║
║   quotes(symbols)          多路行情                          ║
║   vix()                    VIX 恐慌指数                      ║
║   market_sentiment()       市场情绪 (VIX+SPY+QQQ)           ║
║                                                              ║
║ 持仓/资产:                                                   ║
║   positions()              持仓列表 (CLI: positions)        ║
║   assets()                 账户资产 (CLI: assets)            ║
║   exchange_rates()         港元/美元汇率                     ║
║                                                              ║
║ K线数据:                                                     ║
║   kline(symbol, days, period)         历史K线               ║
║   kline_by_count(symbol, count, period)  最近N根K线         ║
║   kline_for_indicators(symbol)   260根日线(技术指标)       ║
║                                                              ║
║ 交易:                                                       ║
║   buy(symbol, qty, price=)         限价买入                 ║
║   sell(symbol, qty, price=)        限价卖出                  ║
║   stop_loss(symbol, qty, trigger_price)   止损单             ║
║   order_cancel(order_id)           撤单                      ║
║   order_query(order_id)            查询订单状态              ║
║   order_replace(old_id, **kwargs)   改单                    ║
║                                                              ║
║ 查询:                                                       ║
║   orders(days=, side=, symbol=)    订单列表                  ║
║   executions(days=, symbol=)       成交记录                  ║
║   stop_orders(symbol=)             活跃止损单                ║
║   cash_flow(days=)                 资金流水                  ║
║                                                              ║
║ 工具:                                                       ║
║   static_info(symbol)              标的基本信息              ║
║   margin_ratio(symbol)             保证金比例                ║
║   estimate_buy_qty(symbol, price)  估算可买数量             ║
║   watchlist()                      自选股列表                ║
╚══════════════════════════════════════════════════════════════╝""",

            "buy": u"""
╔══════════════════════════════════════════════════════════════╗
║                     买入帮助                                ║
╠══════════════════════════════════════════════════════════════╣
║ buy(symbol, qty, *, price=None,                            ║
║     time_in_force="Day", outside_rth="ANY_TIME")            ║
║                                                              ║
║ 参数:                                                       ║
║   symbol        标的代码，如 "TSLA.US"                       ║
║   qty           数量（整数）                                 ║
║   price         限价（None=市价LO）                          ║
║   time_in_force Day/GTC/GTD/IOC/FOK                         ║
║   outside_rth   ANY_TIME(盘前盘后) / RTH_ONLY(仅盘中)      ║
║                                                              ║
║ 示例:                                                       ║
║   client.buy("TSLA.US", 10, price=415.50)                   ║
║   client.buy("AAPL.US", 5, price=190.0,                     ║
║              time_in_force="GTC")                            ║
╚══════════════════════════════════════════════════════════════╝""",

            "sell": u"""
╔══════════════════════════════════════════════════════════════╗
║                     卖出帮助                                ║
╠══════════════════════════════════════════════════════════════╣
║ sell(symbol, qty, *, price=None,                           ║
║      stop_loss=False, trigger_price=None)                   ║
║                                                              ║
║ 限价卖出:                                                    ║
║   client.sell("TSLA.US", 10, price=420.0)                   ║
║                                                              ║
║ 止损单 (MIT + GTC + AnyTime):                               ║
║   client.sell("SPCX.US", 10, stop_loss=True,                ║
║               trigger_price=152.0)                           ║
║   # 等价于:                                                 ║
║   client.stop_loss("SPCX.US", 10, 152.0)                    ║
╚══════════════════════════════════════════════════════════════╝""",

            "stop_loss": u"""
╔══════════════════════════════════════════════════════════════╗
║                    止损单帮助                               ║
╠══════════════════════════════════════════════════════════════╣
║ stop_loss(symbol, qty, trigger_price, *, remark="")         ║
║                                                              ║
║ 订单类型: MIT (触价单)                                       ║
║ 时效:     GTC (Good Till Canceled)                          ║
║ 盘后:     AnyTime (ANY_TIME)                                ║
║                                                              ║
║ 注意: trigger_price 必须传 Decimal 类型，内部自动转换       ║
║                                                              ║
║ 示例:                                                       ║
║   client.stop_loss("SPCX.US", 10, 152.0)                    ║
║   client.stop_loss("GLD.US", 1, 350.0)                      ║
╚══════════════════════════════════════════════════════════════╝""",

            "orders": u"""
╔══════════════════════════════════════════════════════════════╗
║                   订单管理帮助                              ║
╠══════════════════════════════════════════════════════════════╣
║ orders(days=1, side=None, symbol=None)                      ║
║   days=1 今日订单，days>1 历史订单                          ║
║   side="buy"/"sell" 过滤方向                                 ║
║   symbol="TSLA.US" 过滤标的                                  ║
║                                                              ║
║ order_query(order_id)          查询单个订单                  ║
║ order_cancel(order_id)         撤单                          ║
║ order_replace(old_id, **kw)    改单 (撤旧+提新)             ║
╚══════════════════════════════════════════════════════════════╝""",

            "quote": u"""
╔══════════════════════════════════════════════════════════════╗
║                    行情查询帮助                             ║
╠══════════════════════════════════════════════════════════════╣
║ quote(symbol) -> dict                                       ║
║   返回: symbol/name/current_price/change_pct/volume/...     ║
║                                                              ║
║ quotes(list_of_symbols) -> list of dict                     ║
║   批量获取多只行情                                           ║
║                                                              ║
║ vix() -> float              VIX 恐慌指数                    ║
║ market_sentiment() -> dict  VIX + SPY/QQQ 涨跌幅 + regime   ║
╚══════════════════════════════════════════════════════════════╝""",

            "kline": u"""
╔══════════════════════════════════════════════════════════════╗
║                    K线数据帮助                               ║
╠══════════════════════════════════════════════════════════════╣
║ kline(symbol, days=30, period="day")                        ║
║   按日期范围获取，period: day/week/month/1m/5m/15m/30m/60m ║
║                                                              ║
║ kline_by_count(symbol, count=260, period="day")             ║
║   按根数获取（最近 N 根，技术指标专用）                     ║
║                                                              ║
║ kline_for_indicators(symbol, is_leverage=False)             ║
║   260根日线（杠杆ETF用200根）                               ║
╚══════════════════════════════════════════════════════════════╝""",

            "positions": u"""
╔══════════════════════════════════════════════════════════════╗
║                   持仓/资产帮助                              ║
╠══════════════════════════════════════════════════════════════╣
║ positions() -> list of dict                                 ║
║   含 symbol/quantity/cost_price/currency                    ║
║                                                              ║
║ assets() -> dict                                            ║
║   含 total_cash/net_assets/currency                         ║
║                                                              ║
║ exchange_rates() -> dict                                    ║
║   HKD/USD 和 USD/HKD 汇率                                   ║
║                                                              ║
║ cash_flow(days=30) -> list of dict                          ║
║   资金流水记录                                               ║
╚══════════════════════════════════════════════════════════════╝""",

            "vix": u"""
╔══════════════════════════════════════════════════════════════╗
║                   市场情绪帮助                               ║
╠══════════════════════════════════════════════════════════════╣
║ vix() -> float              VIX 恐慌指数                    ║
║ market_sentiment() -> dict                                  ║
║   返回: {vix, spy_pct, qqq_pct, regime}                     ║
║   regime: 低波 (<15) / 正常 (<20) / 警惕 (<25) / 恐慌      ║
╚══════════════════════════════════════════════════════════════╝""",

            "local": u"""
╔══════════════════════════════════════════════════════════════╗
║                本地电脑使用指南 (Win/Mac)                    ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║ 1. 安装本项目锁定的长桥 Python SDK:                          ║
║    pip install longbridge==0.2.74                            ║
║                                                              ║
║ 2. 在项目 .env 设置开放平台 Legacy API Key 三凭证:          ║
║    LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET /             ║
║    LONGBRIDGE_ACCESS_TOKEN                                  ║
║                                                              ║
║ 3. 使用 longbridge_client:                                   ║
║    from longbridge_client import LongbridgeClient            ║
║    client = LongbridgeClient()  # 自动读取 SDK 三凭证       ║
║    client.positions()                                        ║
║                                                              ║
║ 注意：不读取 CLI OAuth token/Region，也不会启动浏览器认证。  ║
╚══════════════════════════════════════════════════════════════╝""",
        }

        text = helps.get(topic)
        if text is None:
            if topic:
                print(f"❌ 未知帮助主题: {topic}", file=sys.stderr)
                print("可用主题: None, buy, sell, stop_loss, orders, quote, kline, positions, vix",
                      file=sys.stderr)
            text = helps.get(None)

        print(text)


# ===========================================================================
# CLI 入口
# ===========================================================================

def main():
    """CLI 入口点

    用法::

        python longbridge_client.py quote TSLA.US
        python longbridge_client.py positions
        python longbridge_client.py buy TSLA.US 10 --price 415.50
        python longbridge_client.py stop_loss SPCX.US 10 --trigger-price 152
        python longbridge_client.py help [topic]
    """
    if len(sys.argv) < 2:
        LongbridgeClient.help()
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd in ("help", "--help", "-h"):
        topic = sys.argv[2] if len(sys.argv) > 2 else None
        LongbridgeClient.help(topic)
        sys.exit(0)

    try:
        client = LongbridgeClient()
    except Exception as e:
        print(f"❌ 认证失败: {e}", file=sys.stderr)
        sys.exit(1)

    rest = sys.argv[2:]

    if cmd == "quote":
        if not rest:
            print("用法: python longbridge_client.py quote SYMBOL [SYM2 ...]")
            sys.exit(1)
        data = client.quotes(rest) if len(rest) > 1 else [client.quote(rest[0])]
        print(json.dumps(data, indent=2, ensure_ascii=False))

    elif cmd == "quotes":
        if not rest:
            print("用法: python longbridge_client.py quotes SYM1 SYM2 ...")
            sys.exit(1)
        data = client.quotes(rest)
        print(json.dumps(data, indent=2, ensure_ascii=False))

    elif cmd == "positions":
        data = client.positions()
        if not data:
            print("无持仓")
        else:
            for pos in data:
                qty = pos.get('quantity', '')
                cost = pos.get('cost_price', '')
                ccy = pos.get('currency', 'USD')
                print(f"  {pos.get('symbol', ''):>10s}  数量: {qty:>6s}  成本: {cost:>10s} {ccy}")

    elif cmd == "assets":
        data = client.assets()
        if data:
            print(json.dumps(data, indent=2, ensure_ascii=False))

    elif cmd == "vix":
        v = client.vix()
        if v is not None:
            print(f"VIX: {v:.2f}")
        else:
            print("无法获取 VIX")

    elif cmd == "sentiment":
        data = client.market_sentiment()
        print(json.dumps(data, indent=2, ensure_ascii=False))

    elif cmd == "buy":
        if len(rest) < 2:
            print("用法: python longbridge_client.py buy SYMBOL QTY [--price PRICE]")
            sys.exit(1)
        symbol, qty = rest[0], int(rest[1])
        price = None
        i = 2
        while i < len(rest):
            if rest[i] == "--price" and i + 1 < len(rest):
                price = float(rest[i + 1]); i += 2
            else:
                i += 1
        result = client.buy(symbol, qty, price=price)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "sell":
        if len(rest) < 2:
            print("用法: python longbridge_client.py sell SYMBOL QTY [--price PRICE] [--stop-loss] [--trigger-price PRICE]")
            sys.exit(1)
        symbol, qty = rest[0], int(rest[1])
        price = None
        stop_loss = False
        trigger_price = None
        i = 2
        while i < len(rest):
            if rest[i] == "--price" and i + 1 < len(rest):
                price = float(rest[i + 1]); i += 2
            elif rest[i] == "--trigger-price" and i + 1 < len(rest):
                trigger_price = float(rest[i + 1]); i += 2
            elif rest[i] == "--stop-loss":
                stop_loss = True; i += 1
            else:
                i += 1
        result = client.sell(symbol, qty, price=price, stop_loss=stop_loss, trigger_price=trigger_price)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "stop_loss":
        if len(rest) < 3:
            print("用法: python longbridge_client.py stop_loss SYMBOL QTY TRIGGER_PRICE")
            sys.exit(1)
        result = client.stop_loss(rest[0], int(rest[1]), float(rest[2]))
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "cancel":
        if not rest:
            print("用法: python longbridge_client.py cancel ORDER_ID")
            sys.exit(1)
        client.order_cancel(rest[0])

    elif cmd == "orders":
        days = int(rest[0]) if rest else 1
        data = client.orders(days=days)
        if not data:
            print("无订单")
        else:
            for o in data:
                side_cn = "买入" if o.get("side") == "Buy" else "卖出"
                print(f"  {o.get('order_id', ''):>20s}  {o.get('symbol', ''):>10s}  {side_cn}  {o.get('order_type', '')}  {o.get('status', '')}")

    elif cmd == "kline":
        if not rest:
            print("用法: python longbridge_client.py kline SYMBOL [--days N] [--period day]")
            sys.exit(1)
        symbol = rest[0]
        days = 30
        period = "day"
        i = 1
        while i < len(rest):
            if rest[i] == "--days" and i + 1 < len(rest):
                days = int(rest[i + 1]); i += 2
            elif rest[i] == "--period" and i + 1 < len(rest):
                period = rest[i + 1]; i += 2
            else:
                i += 1
        data = client.kline(symbol, days=days, period=period)
        if data:
            for k in data:
                ts = k.get('timestamp', '')[:10]
                print(f"  {ts:>12s}  O:{k['open']:>10.2f}  H:{k['high']:>10.2f}  L:{k['low']:>10.2f}  C:{k['close']:>10.2f}")
        else:
            print("无数据")

    elif cmd == "watchlist":
        symbols = client.watchlist()
        for s in symbols:
            print(f"  {s}")

    else:
        print(f"❌ 未知命令: {cmd}")
        print("可用命令: quote, quotes, positions, assets, vix, sentiment, buy, sell, stop_loss, cancel, orders, kline, watchlist, help")
        sys.exit(1)


if __name__ == "__main__":
    main()
