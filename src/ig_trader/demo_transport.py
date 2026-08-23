"""Endpoint-locked IG Demo REST adapter for the DQ-01 execution protocol.

The transport owns no trading policy.  It validates the one permitted Demo
gateway before every request, rejects redirects, and exposes broker facts in
small typed projections.  Order mutation methods exist solely so
``DemoExecutionCore`` can use the existing narrow ``IGDemoTransport``
protocol; callers must not invoke them directly.
"""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlsplit

from src.ig_trader.demo_execution import (
    DemoConfirmation,
    DemoConfirmationStatus,
    DemoDirection,
    DemoMarketMetadata,
    DemoPosition,
    DemoSubmission,
)

IG_DEMO_BASE_URL = "https://demo-api.ig.com/gateway/deal"
APPROVED_IG_DEMO_BASE_URLS = frozenset({IG_DEMO_BASE_URL})


class DemoTransportError(RuntimeError):
    """A Demo REST response or endpoint cannot be trusted."""


class AuthorizedIGSession(Protocol):
    """The safe, token-owning slice supplied by ``SessionManager``."""

    account_id: str | None

    def authorized_request(self, method: str, endpoint: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class IGDemoAccount:
    """Sanitized account facts used to prove local Demo identity."""

    account_id: str | None
    currency: str | None
    balance: Decimal | None
    available_funds: Decimal | None
    profit_loss: Decimal | None


@dataclass(frozen=True)
class IGDemoPositionDetails:
    """Broker position truth for operator display and reconciliation."""

    deal_id: str
    deal_reference: str | None
    epic: str
    instrument_name: str | None
    direction: DemoDirection
    size: Decimal
    entry_level: Decimal | None
    stop_level: Decimal | None
    limit_level: Decimal | None
    bid: Decimal | None
    offer: Decimal | None
    currency: str | None
    created_at: datetime | None

    @property
    def core_position(self) -> DemoPosition:
        return DemoPosition(
            deal_id=self.deal_id,
            epic=self.epic,
            direction=self.direction,
            size=self.size,
        )


@dataclass(frozen=True)
class IGDemoMarketDetails:
    """Only verified market metadata returned by IG, never inferred defaults."""

    epic: str
    display_name: str | None
    asset_class: str | None
    expiry: str | None
    market_status: str | None
    currency: str | None
    minimum_deal_size: Decimal | None
    minimum_stop_distance: Decimal | None
    decimal_places: int | None
    pip_or_tick_size: Decimal | None
    value_of_one_pip: Decimal | None
    streaming_available: bool | None
    bid: Decimal | None
    offer: Decimal | None
    observed_at: datetime
    controlled_risk_supported: bool | None = None

    def to_execution_metadata(self) -> DemoMarketMetadata:
        """Project the DQ-01 validation fields without filling missing values."""

        return DemoMarketMetadata(
            epic=self.epic,
            instrument_currency=self.currency,
            expiry=self.expiry,
            pip_scale=self.pip_or_tick_size,
            decimal_places=self.decimal_places,
            minimum_deal_size=self.minimum_deal_size,
            minimum_stop_distance=self.minimum_stop_distance,
            guaranteed_stop_supported=self.controlled_risk_supported,
            market_status=self.market_status,
            observed_at=self.observed_at,
        )


def validate_ig_demo_endpoint(base_url: str | None) -> str:
    """Accept exactly the documented HTTPS Demo gateway and nothing else."""

    if not isinstance(base_url, str) or not base_url:
        raise DemoTransportError("IG Demo endpoint is missing")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "demo-api.ig.com"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or base_url not in APPROVED_IG_DEMO_BASE_URLS
    ):
        raise DemoTransportError("IG endpoint is not the approved Demo gateway")
    return base_url


class IGDemoRESTTransport:
    """Real IG Demo adapter with strict response validation and no token logging."""

    def __init__(self, *, session: AuthorizedIGSession, base_url: str) -> None:
        self._session = session
        self.base_url = validate_ig_demo_endpoint(base_url)

    def get_account(self) -> IGDemoAccount:
        document = self._request_json("GET", "/session", version="1")
        account_info = _mapping(document.get("accountInfo"))
        return IGDemoAccount(
            account_id=_text(document.get("accountId"))
            or _text(document.get("currentAccountId"))
            or _text(getattr(self._session, "account_id", None)),
            currency=_text(document.get("currency")) or _text(account_info.get("currency")),
            balance=_decimal(account_info.get("balance")),
            available_funds=_decimal(account_info.get("available")),
            profit_loss=_decimal(account_info.get("profitLoss")),
        )

    def list_positions(self) -> tuple[DemoPosition, ...]:
        return tuple(item.core_position for item in self.list_position_details())

    def list_position_details(self) -> tuple[IGDemoPositionDetails, ...]:
        document = self._request_json("GET", "/positions", version="2")
        values = document.get("positions")
        if not isinstance(values, list):
            raise DemoTransportError("IG positions response is incomplete")
        return tuple(_position_details(item) for item in values)

    def get_position(self, deal_id: str) -> DemoPosition | None:
        return self.get_position_details(deal_id).core_position

    def get_position_details(self, deal_id: str) -> IGDemoPositionDetails:
        _required_identifier(deal_id, "deal ID")
        document = self._request_json("GET", f"/positions/{deal_id}", version="2")
        return _position_details(document)

    def get_confirmation(self, deal_reference: str) -> DemoConfirmation | None:
        _required_identifier(deal_reference, "deal reference")
        document = self._request_json("GET", f"/confirms/{deal_reference}", version="1")
        status = _text(document.get("dealStatus"))
        direction = _direction(document.get("direction"))
        if status not in {
            DemoConfirmationStatus.ACCEPTED.value,
            DemoConfirmationStatus.REJECTED.value,
        }:
            raise DemoTransportError("IG confirmation status is unknown")
        return DemoConfirmation(
            deal_reference=_text(document.get("dealReference")) or deal_reference,
            deal_id=_text(document.get("dealId")),
            deal_status=DemoConfirmationStatus(status),
            status=_text(document.get("status")) or "UNKNOWN",
            epic=_text(document.get("epic")),
            direction=direction,
            size=_decimal(document.get("size")),
            level=_decimal(document.get("level")),
            stop_level=_decimal(document.get("stopLevel")),
            limit_level=_decimal(document.get("limitLevel")),
        )

    def create_position(self, payload: Mapping[str, object]) -> DemoSubmission:
        """Submit only the immutable DQ-01 payload supplied by ``DemoExecutionCore``."""

        document = self._request_json("POST", "/positions/otc", version="2", json=dict(payload))
        return _submission(document)

    def close_position(self, payload: Mapping[str, object]) -> DemoSubmission:
        """Close only a reconciled deal payload supplied by ``DemoExecutionCore``."""

        document = self._request_json("DELETE", "/positions/otc", version="1", json=dict(payload))
        return _submission(document)

    def get_market(self, epic: str) -> IGDemoMarketDetails:
        _required_identifier(epic, "EPIC")
        document = self._request_json("GET", f"/markets/{epic}", version="4")
        return _market_details(document, epic=epic)

    def search_markets(self, search_term: str) -> tuple[dict[str, object], ...]:
        """Return only minimal discovery candidates; selection remains conservative."""

        if not isinstance(search_term, str) or not search_term.strip():
            raise DemoTransportError("market search term is required")
        document = self._request_json(
            "GET", "/markets", version="1", params={"searchTerm": search_term.strip()}
        )
        markets = document.get("markets")
        if not isinstance(markets, list):
            raise DemoTransportError("IG market search response is incomplete")
        return tuple(_market_candidate(item) for item in markets if isinstance(item, Mapping))

    def get_market_navigation(self, node_id: str | None = None) -> Mapping[str, object]:
        endpoint = "/market-navigation" if node_id is None else f"/market-navigation/{node_id}"
        return self._request_json("GET", endpoint, version="3")

    def get_historical_prices(
        self, epic: str, resolution: str, points: int
    ) -> Mapping[str, object]:
        """Read bounded history only; quota accounting remains the caller's responsibility."""

        _required_identifier(epic, "EPIC")
        if not isinstance(points, int) or isinstance(points, bool) or not 1 <= points <= 10_000:
            raise DemoTransportError("historical point count is invalid")
        if not isinstance(resolution, str) or not resolution:
            raise DemoTransportError("historical resolution is required")
        return self._request_json("GET", f"/prices/{epic}/{resolution}/{points}", version="2")

    def get_working_orders(self) -> Mapping[str, object]:
        return self._request_json("GET", "/working-orders", version="2")

    def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        version: str,
        json: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        validate_ig_demo_endpoint(self.base_url)
        response = self._session.authorized_request(
            method,
            endpoint,
            headers={
                "VERSION": version,
                "Accept": "application/json; charset=UTF-8",
                "Content-Type": "application/json",
            },
            json=json,
            params=params,
        )
        status_code = getattr(response, "status_code", None)
        history = getattr(response, "history", ())
        if (
            not isinstance(status_code, int)
            or 300 <= status_code < 400
            or bool(getattr(response, "is_redirect", False))
            or history
        ):
            raise DemoTransportError("IG Demo response redirected or is invalid")
        if not 200 <= status_code < 300:
            raise DemoTransportError(f"IG Demo request failed with HTTP status {status_code}")
        try:
            document = response.json()
        except Exception as error:  # pragma: no cover - external client variations
            raise DemoTransportError("IG Demo response is not JSON") from error
        if not isinstance(document, Mapping):
            raise DemoTransportError("IG Demo response has an invalid JSON shape")
        return document


def _submission(document: Mapping[str, object]) -> DemoSubmission:
    reference = _text(document.get("dealReference"))
    if reference is None:
        raise DemoTransportError("IG dealing acknowledgement has no deal reference")
    return DemoSubmission(deal_reference=reference, deal_id=_text(document.get("dealId")))


def _position_details(value: object) -> IGDemoPositionDetails:
    document = _mapping(value)
    position = _mapping(document.get("position"))
    market = _mapping(document.get("market"))
    direction = _direction(position.get("direction"))
    deal_id = _text(position.get("dealId"))
    epic = _text(market.get("epic")) or _text(position.get("epic"))
    size = _decimal(position.get("size"))
    if deal_id is None or epic is None or direction is None or size is None or size <= 0:
        raise DemoTransportError("IG position response is incomplete")
    return IGDemoPositionDetails(
        deal_id=deal_id,
        deal_reference=_text(position.get("dealReference")),
        epic=epic,
        instrument_name=_text(market.get("instrumentName")),
        direction=direction,
        size=size,
        entry_level=_decimal(position.get("level")),
        stop_level=_decimal(position.get("stopLevel")),
        limit_level=_decimal(position.get("limitLevel")),
        bid=_decimal(market.get("bid")),
        offer=_decimal(market.get("offer")),
        currency=_text(position.get("currency")) or _text(market.get("currency")),
        created_at=_datetime(position.get("createdDateUTC")),
    )


def _market_details(document: Mapping[str, object], *, epic: str) -> IGDemoMarketDetails:
    instrument = _mapping(document.get("instrument"))
    snapshot = _mapping(document.get("snapshot"))
    rules = _mapping(document.get("dealingRules"))
    currencies = instrument.get("currencies")
    currency = None
    if isinstance(currencies, list) and len(currencies) == 1:
        currency = _text(_mapping(currencies[0]).get("code"))
    min_size = _rule_value(rules.get("minDealSize"))
    min_stop = _rule_value(rules.get("minNormalStopOrLimitDistance"))
    decimal_places = _integer(snapshot.get("decimalPlacesFactor"))
    pip = _decimal(instrument.get("onePipMeans"))
    return IGDemoMarketDetails(
        epic=epic,
        display_name=_text(instrument.get("name")),
        asset_class=_text(instrument.get("type")),
        expiry=_text(instrument.get("expiry")),
        market_status=_text(snapshot.get("marketStatus")),
        currency=currency,
        minimum_deal_size=min_size,
        minimum_stop_distance=min_stop,
        decimal_places=decimal_places,
        pip_or_tick_size=pip,
        value_of_one_pip=_decimal(instrument.get("valueOfOnePip")),
        streaming_available=_bool(instrument.get("streamingPricesAvailable")),
        bid=_decimal(snapshot.get("bid")),
        offer=_decimal(snapshot.get("offer")),
        observed_at=datetime.now(UTC),
        controlled_risk_supported=_bool(instrument.get("controlledRiskAllowed")),
    )


def _market_candidate(value: Mapping[str, object]) -> dict[str, object]:
    instrument = _mapping(value.get("instrument"))
    snapshot = _mapping(value.get("snapshot"))
    return {
        "epic": _text(instrument.get("epic")) or _text(value.get("epic")),
        "name": _text(instrument.get("name")) or _text(value.get("instrumentName")),
        "type": _text(instrument.get("type")),
        "expiry": _text(instrument.get("expiry")),
        "market_status": _text(snapshot.get("marketStatus")),
    }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _direction(value: object) -> DemoDirection | None:
    try:
        return DemoDirection(str(value))
    except ValueError:
        return None


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _rule_value(value: object) -> Decimal | None:
    rule = _mapping(value)
    if _text(rule.get("unit")) != "POINTS":
        return None
    return _decimal(rule.get("value"))


def _required_identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "/" in value or "?" in value:
        raise DemoTransportError(f"{name} is invalid")
