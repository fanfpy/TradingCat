"""交易所完成日线新鲜度规则（US/HK/CN），供研究与生产共用。"""

from datetime import datetime

from shared import db as dbm


def market_for_symbol(symbol: str) -> str:
    suffix = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else "US"
    return {"US": "US", "HK": "HK", "SH": "CN", "SZ": "CN", "CN": "CN"}.get(
        suffix, "US")


def completed_bar_freshness(conn, symbol: str, last_bar_date: str,
                            as_of_date: str, *, source: str = "") -> tuple[bool, str]:
    """按交易日而非自然日判断最新完成 bar。

    亚洲时区运行 US 日线时允许一个已开市 session 的数据发布时差。真实供应商
    数据若缺少日历覆盖则 fail closed；测试/模拟源保留三自然日兼容回退。
    """
    market = market_for_symbol(symbol)
    rows = dbm.calendar_between(conn, market, last_bar_date, as_of_date)
    max_calendar = conn.execute(
        "SELECT max(trade_date) FROM trading_calendar WHERE market=?", (market,),
    ).fetchone()[0]
    if rows and max_calendar and max_calendar >= as_of_date:
        open_dates = [row["trade_date"] for row in rows if row["is_open"]]
        if not open_dates:
            return True, "calendar_no_open_session"
        lag = 1 if market == "US" else 0
        required = open_dates[max(0, len(open_dates) - 1 - lag)]
        return last_bar_date >= required, f"calendar_required={required}"

    if source.lower() in ("longbridge", "production", "live"):
        return False, f"{market} 交易日历未覆盖 {as_of_date}"
    try:
        days_old = (
            datetime.strptime(as_of_date, "%Y-%m-%d")
            - datetime.strptime(last_bar_date, "%Y-%m-%d")
        ).days
    except ValueError:
        return False, "bar 日期格式无效"
    return days_old <= 3, f"non_production_calendar_fallback_days={days_old}"
