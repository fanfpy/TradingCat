"""Security metadata resolution without inferred trading attributes."""

from typing import Dict, Optional

from shared import db as dbm


UNKNOWN_METADATA = "UNKNOWN_METADATA"


class UnknownSecurityMetadataError(ValueError):
    pass


class LazyLongbridgeSecurityProvider:
    """仅在 security_master miss 时初始化 quote-scope Longbridge。"""

    def __init__(self):
        self._client = None

    def static_info(self, symbol: str):
        if self._client is None:
            from shared.longbridge_client import LongbridgeClient
            self._client = LongbridgeClient(scope="quote")
        return self._client.static_info(symbol)


def require_security_metadata(conn, symbol: str) -> Dict:
    row = dbm.get_security(conn, symbol)
    if row is None:
        raise UnknownSecurityMetadataError(f"UNKNOWN_METADATA: {symbol}")
    metadata = dict(row)
    required = ("symbol", "name", "exchange", "currency", "asset_type")
    unknown = [field for field in required
               if not metadata.get(field)
               or str(metadata[field]).upper() == "UNKNOWN"]
    if unknown:
        raise UnknownSecurityMetadataError(
            f"UNKNOWN_METADATA: {symbol} missing {','.join(unknown)}")
    return metadata


class SecurityResolver:
    def __init__(self, core_conn, provider=None):
        self.core = core_conn
        self.provider = provider

    def resolve(self, query: str) -> Dict:
        matches = dbm.search_security(self.core, query)
        if matches:
            return {"status": "RESOLVED", "matches": matches, "source": "security_master"}

        symbol = query.strip().upper()
        if "." not in symbol or self.provider is None:
            return {"status": UNKNOWN_METADATA, "symbol": symbol, "matches": []}

        metadata = self.provider.static_info(symbol)
        if metadata is None:
            return {"status": UNKNOWN_METADATA, "symbol": symbol, "matches": []}
        required = ("symbol", "name", "exchange", "currency", "asset_type")
        missing = [field for field in required if not metadata.get(field)]
        if missing or str(metadata.get("asset_type", "")).upper() == "UNKNOWN":
            return {
                "status": UNKNOWN_METADATA,
                "symbol": symbol,
                "matches": [],
                "missing": missing or ["asset_type"],
            }

        dbm.upsert_security(
            self.core,
            metadata["symbol"],
            metadata["name"],
            metadata["exchange"],
            metadata["currency"],
            aliases=[query],
            asset_type=metadata["asset_type"],
            lot_size=metadata.get("lot_size"),
        )
        matches = dbm.search_security(self.core, metadata["symbol"])
        return {"status": "RESOLVED", "matches": matches, "source": "longbridge"}