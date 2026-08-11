"""Security metadata hydration and resolution without inferred attributes."""

from typing import Dict, Iterable, Optional

from shared import db as dbm


UNKNOWN_METADATA = "UNKNOWN_METADATA"
MAX_PROVIDER_ATTEMPTS = 2


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


class SecurityService:
    """Cache-first metadata hydration; provider data is persisted only after validation."""

    def __init__(self, core_conn, provider=None):
        self.core = core_conn
        self.provider = provider

    @staticmethod
    def _validate(symbol: str, metadata) -> Dict:
        if metadata is None:
            raise UnknownSecurityMetadataError(f"UNKNOWN_METADATA: {symbol}")
        normalized = dict(metadata)
        normalized["symbol"] = str(normalized.get("symbol", "")).upper()
        expected = symbol.strip().upper()
        if normalized["symbol"] != expected:
            raise UnknownSecurityMetadataError(
                f"UNKNOWN_METADATA: {expected} provider symbol mismatch")
        required = ("symbol", "name", "exchange", "currency", "asset_type")
        unknown = [field for field in required
                   if not normalized.get(field)
                   or str(normalized[field]).upper() == "UNKNOWN"]
        if unknown:
            raise UnknownSecurityMetadataError(
                f"UNKNOWN_METADATA: {expected} missing {','.join(unknown)}")
        lot_size = normalized.get("lot_size")
        if lot_size not in (None, "", 0) and int(lot_size) <= 0:
            raise UnknownSecurityMetadataError(
                f"UNKNOWN_METADATA: {expected} invalid lot_size")
        return normalized

    def cached(self, symbol: str) -> Optional[Dict]:
        row = dbm.get_security(self.core, symbol)
        if row is None:
            return None
        try:
            return self._validate(symbol, row)
        except UnknownSecurityMetadataError:
            return None

    def ensure_metadata(self, symbol: str) -> Dict:
        symbol = symbol.strip().upper()
        cached = self.cached(symbol)
        if cached is not None:
            return {**cached, "metadata_source": "security_master"}
        if self.provider is None:
            raise UnknownSecurityMetadataError(f"UNKNOWN_METADATA: {symbol}")

        metadata = None
        last_exc = None
        for attempt in range(1, MAX_PROVIDER_ATTEMPTS + 1):
            try:
                metadata = self._validate(symbol, self.provider.static_info(symbol))
                break
            except Exception as exc:
                last_exc = exc
                # Validation/identity failures are deterministic and must never be
                # retried. Provider exceptions may opt into one bounded retry.
                if not bool(getattr(exc, "retryable", False)) or attempt >= MAX_PROVIDER_ATTEMPTS:
                    raise
        if metadata is None and last_exc is not None:
            raise last_exc
        metadata_version = str(
            metadata.get("metadata_version") or metadata.get("version") or
            metadata.get("updated_at") or "unknown")
        dbm.upsert_security(
            self.core, metadata["symbol"], metadata["name"],
            metadata["exchange"], metadata["currency"], aliases=[symbol],
            sector=metadata.get("sector", "UNKNOWN"),
            asset_type=metadata["asset_type"],
            beta=float(metadata.get("beta", 1.0)),
            leverage=float(metadata.get("leverage", 1.0)),
            lot_size=metadata.get("lot_size"),
            metadata_source="provider", metadata_version=metadata_version,
        )
        stored = self.cached(symbol)
        if stored is None:
            raise UnknownSecurityMetadataError(
                f"UNKNOWN_METADATA: {symbol} persisted metadata incomplete")
        return {**stored, "metadata_source": "provider"}

    def ensure_batch(self, symbols: Iterable[str]) -> list[Dict]:
        results = []
        seen = set()
        for raw_symbol in symbols:
            symbol = str(raw_symbol).strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            try:
                metadata = self.ensure_metadata(symbol)
                results.append({"symbol": symbol, "ok": True,
                                "metadata": metadata})
            except Exception as exc:
                dbm.record_security_failure(
                    self.core, symbol, type(exc).__name__, str(exc),
                    retryable=bool(getattr(exc, "retryable", False)))
                results.append({
                    "symbol": symbol,
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "retryable": bool(getattr(exc, "retryable", False)),
                })
        return results


def require_security_metadata(conn, symbol: str) -> Dict:
    metadata = SecurityService(conn).cached(symbol)
    if metadata is None:
        raise UnknownSecurityMetadataError(f"UNKNOWN_METADATA: {symbol}")
    return metadata


class SecurityResolver:
    def __init__(self, core_conn, provider=None):
        self.core = core_conn
        self.provider = provider
        self.service = SecurityService(core_conn, provider)

    def resolve(self, query: str) -> Dict:
        matches = [match for match in dbm.search_security(self.core, query)
                   if self.service.cached(match["symbol"]) is not None]
        if matches:
            return {"status": "RESOLVED", "matches": matches,
                    "source": "security_master"}

        symbol = query.strip().upper()
        if "." not in symbol:
            return {"status": UNKNOWN_METADATA, "symbol": symbol, "matches": []}
        try:
            metadata = self.service.ensure_metadata(symbol)
        except UnknownSecurityMetadataError as exc:
            return {
                "status": UNKNOWN_METADATA,
                "symbol": symbol,
                "matches": [],
                "error_message": str(exc),
            }
        matches = dbm.search_security(self.core, metadata["symbol"])
        return {"status": "RESOLVED", "matches": matches, "source": "longbridge"}
