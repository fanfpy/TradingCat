"""DataHub 同步边界：长桥 4.4.3 交易日历 -> StateRepository。"""

from datetime import date

from shared import db as dbm


class LongbridgeDataHub:
    def __init__(self, conn, client=None, daily_quota: int = 1000):
        if client is None:
            from shared.longbridge_client import LongbridgeClient
            client = LongbridgeClient(scope="quote")
        self.conn = conn
        self.client = client
        self.daily_quota = daily_quota

    def _reserve(self, operation: str, units: int = 1) -> None:
        result = dbm.reserve_api_quota(
            self.conn, f"longbridge.{operation}", units, self.daily_quota,
            window_seconds=86400,
        )
        if not result["allowed"]:
            raise RuntimeError(
                f"Longbridge API quota exceeded: {operation} "
                f"{result['used']}/{result['limit']}")

    def sync_calendar(self, market: str, start: date, end: date) -> int:
        if end < start:
            raise ValueError("calendar end 必须 >= start")
        self._reserve("calendar")
        rows = self.client.trading_calendar(market, start, end)
        count = dbm.upsert_calendar(self.conn, market.upper(), rows, "longbridge")
        dbm.audit(self.conn, "DATAHUB_CALENDAR", entity_type="market",
                  entity_id=market.upper(),
                  payload={"start": str(start), "end": str(end), "rows": count})
        return count
