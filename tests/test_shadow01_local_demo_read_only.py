"""Focused safety tests for lazy local Shadow01 IG Demo construction."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.ig_trader.shadow01.local_demo_read_only import (
    SessionManagerReadOnlyTransport,
    Shadow01LocalDemoReadOnlyError,
    Shadow01LocalDemoReadOnlyFactory,
)
from src.ig_trader.shadow01.read_only_broker import ReadOnlyBrokerError

_DEFAULT_RESPONSE_DOCUMENT = object()
_UNCHANGED = object()


class FakeResponse:
    """Minimal successful or malformed HTTP-style response for local-only tests."""

    def __init__(
        self,
        *,
        status_code: object = 200,
        document: object = _DEFAULT_RESPONSE_DOCUMENT,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._document = document
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._document


class FakeSessionManager:
    """In-memory session stub which models the SessionManager observer hook."""

    def __init__(
        self,
        request_observer: Any,
        *,
        authenticated_account_id: str | None = "DEMO-EXPECTED",
        login_succeeds: bool = True,
        response: object = _DEFAULT_RESPONSE_DOCUMENT,
        account_id_after_authorized_request: object = _UNCHANGED,
        authenticated_after_authorized_request: object = _UNCHANGED,
        logout_succeeds: bool = True,
        logout_response: FakeResponse | None = None,
    ) -> None:
        self._request_observer = request_observer
        self._authenticated_account_id = authenticated_account_id
        self._login_succeeds = login_succeeds
        self._response = response
        self._account_id_after_authorized_request = account_id_after_authorized_request
        self._authenticated_after_authorized_request = authenticated_after_authorized_request
        self._logout_succeeds = logout_succeeds
        self._logout_response = logout_response
        self._response_error_observer: Any = None
        self._request_version_observer: Any = None
        self._authenticated = False
        self.account_id: str | None = None
        self.lightstreamer_endpoint = "https://stream.unit.test"
        self.cst = "unit-cst"
        self.x_security_token = "unit-xst"
        self.login_count = 0
        self.logout_count = 0
        self.authorized_requests: list[tuple[str, str]] = []
        self.authorized_request_kwargs: list[dict[str, Any]] = []
        self.observed_requests: list[tuple[str, str]] = []

    def login(self) -> bool:
        self._request_observer("POST", "session")
        self.observed_requests.append(("POST", "session"))
        self.login_count += 1
        if not self._login_succeeds:
            return False
        self.account_id = self._authenticated_account_id
        self._authenticated = True
        return True

    def logout(self) -> bool:
        assert self.is_authenticated()
        self._request_observer("DELETE", "session")
        self.observed_requests.append(("DELETE", "session"))
        self.logout_count += 1
        if not self._logout_succeeds:
            if self._logout_response is not None and self._response_error_observer is not None:
                self._response_error_observer(self._logout_response)
            return False
        self._authenticated = False
        self.account_id = None
        self.lightstreamer_endpoint = None
        self.cst = None
        self.x_security_token = None
        return True

    def is_authenticated(self) -> bool:
        return self._authenticated

    def set_response_error_observer(self, observer: Any) -> None:
        self._response_error_observer = observer

    def set_request_version_observer(self, observer: Any) -> None:
        self._request_version_observer = observer

    def authorized_request(self, method: str, endpoint: str, **kwargs: Any) -> object:
        assert self.is_authenticated()
        self._request_observer(method, endpoint.lstrip("/"))
        if self._request_version_observer is not None:
            headers = kwargs.get("headers")
            version = headers.get("VERSION") if isinstance(headers, dict) else None
            self._request_version_observer(method, endpoint.lstrip("/"), version)
        self.authorized_requests.append((method, endpoint))
        self.authorized_request_kwargs.append(dict(kwargs))
        if self._account_id_after_authorized_request is not _UNCHANGED:
            self.account_id = self._account_id_after_authorized_request
        if self._authenticated_after_authorized_request is not _UNCHANGED:
            self._authenticated = self._authenticated_after_authorized_request
        if self._response is not _DEFAULT_RESPONSE_DOCUMENT:
            if (
                isinstance(self._response, FakeResponse)
                and isinstance(self._response.status_code, int)
                and not 200 <= self._response.status_code < 300
                and self._response_error_observer is not None
            ):
                self._response_error_observer(self._response)
            return self._response
        return FakeResponse(document={"method": method, "endpoint": endpoint})


def _demo_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "ig_demo": True,
        "ig_base_url": "https://demo-api.ig.com/gateway/deal",
        "demo_operator_local": True,
        "paper_trading": True,
        "ig_expected_demo_account_id": "DEMO-EXPECTED",
        "ig_api_key": "api-key-not-for-output",
        "ig_identifier": "identifier-not-for-output",
        "ig_password": "password-not-for-output",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_status_is_nonactivating_and_credential_safe() -> None:
    session_factory_calls: list[Any] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session_factory_calls.append(observer)
        return FakeSessionManager(observer)

    factory = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    )

    status = factory.status()

    assert status.ready is True
    assert status.reason_code == "SHADOW01_DEMO_READ_ONLY_READY"
    assert status.execution_authority == "OFF"
    assert status.demo_mode is True
    assert status.local_operator is True
    assert status.paper_trading is True
    assert status.expected_demo_account_configured is True
    assert status.credentials_present is True
    assert session_factory_calls == []
    assert "api-key-not-for-output" not in str(status.document())
    assert "identifier-not-for-output" not in str(status.document())
    assert "password-not-for-output" not in str(status.document())
    assert "DEMO-EXPECTED" not in str(status.document())


@pytest.mark.parametrize("field", ("ig_api_key", "ig_identifier", "ig_password"))
def test_status_requires_each_nonempty_demo_credential_without_echoing_it(field: str) -> None:
    settings = _demo_settings(**{field: "   "})
    factory = Shadow01LocalDemoReadOnlyFactory(settings_provider=lambda: settings)

    status = factory.status()

    assert status.ready is False
    assert status.reason_code == "SHADOW01_DEMO_CREDENTIALS_REQUIRED"
    assert field not in str(status.document())


@pytest.mark.parametrize("expected_demo_account_id", ("", "   ", " DEMO-EXPECTED "))
def test_status_requires_an_exact_nonempty_expected_demo_account_id(
    expected_demo_account_id: str,
) -> None:
    settings = _demo_settings(ig_expected_demo_account_id=expected_demo_account_id)
    session_factory_calls: list[Any] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session_factory_calls.append(observer)
        return FakeSessionManager(observer)

    factory = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: settings,
        session_factory=session_factory,
    )

    status = factory.status()

    assert status.ready is False
    assert status.reason_code == "SHADOW01_EXPECTED_DEMO_ACCOUNT_ID_REQUIRED"
    assert "ig_expected_demo_account_id" not in str(status.document())
    assert "DEMO-EXPECTED" not in str(status.document())
    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_EXPECTED_DEMO_ACCOUNT_ID_REQUIRED",
    ):
        factory.build()
    assert session_factory_calls == []


@pytest.mark.parametrize(
    ("settings", "reason_code"),
    (
        (_demo_settings(ig_demo=False), "SHADOW01_DEMO_MODE_REQUIRED"),
        (
            _demo_settings(ig_base_url="https://api.ig.com/gateway/deal"),
            "SHADOW01_DEMO_ENDPOINT_REQUIRED",
        ),
        (_demo_settings(demo_operator_local=False), "SHADOW01_LOCAL_OPERATOR_REQUIRED"),
        (_demo_settings(paper_trading=False), "SHADOW01_PAPER_TRADING_REQUIRED"),
    ),
)
def test_status_fails_closed_for_non_demo_or_nonlocal_settings(
    settings: SimpleNamespace, reason_code: str
) -> None:
    factory = Shadow01LocalDemoReadOnlyFactory(settings_provider=lambda: settings)

    assert factory.status().reason_code == reason_code


def test_build_is_explicit_and_returns_an_off_read_only_broker() -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer)
        created_sessions.append(session)
        return session

    factory = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    )

    broker = factory.build()

    assert len(created_sessions) == 1
    assert created_sessions[0].login_count == 0
    assert factory.execution_authority == "OFF"
    assert broker.execution_authority == "OFF"
    assert broker.read_market("CS.D.EURGBP.MINI.IP") == {
        "method": "GET",
        "endpoint": "/markets/CS.D.EURGBP.MINI.IP",
    }
    assert created_sessions[0].login_count == 1
    assert created_sessions[0].authorized_requests == [("GET", "/markets/CS.D.EURGBP.MINI.IP")]
    assert broker.request_counters.authentication_request_count == 1
    assert broker.request_counters.market_read_count == 1
    assert broker.request_counters.total_rest_request_count == 2
    assert broker.request_counters.execution_safety_document() == {
        "create": 0,
        "close": 0,
        "working_orders": 0,
        "demo_starts": 0,
    }


def test_explicit_authentication_is_allowlisted_and_only_happens_on_request() -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer)
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    assert created_sessions[0].login_count == 0
    assert broker.authenticate() is True
    assert created_sessions[0].login_count == 1
    assert broker.request_counters.authentication_request_count == 1
    assert broker.request_counters.total_rest_request_count == 1


def test_authenticated_session_material_is_immutable_redacted_and_does_not_issue_rest() -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer)
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    assert broker.authenticate() is True
    material = broker.stream_session_material()

    assert material.presence_document() == {
        "account_identifier_present": True,
        "lightstreamer_endpoint_present": True,
        "cst_present": True,
        "x_security_token_present": True,
    }
    assert "DEMO-EXPECTED" not in repr(material)
    assert "stream.unit.test" not in repr(material)
    assert "unit-cst" not in repr(material)
    assert "unit-xst" not in repr(material)
    assert created_sessions[0].authorized_requests == []
    assert broker.request_counters.total_rest_request_count == 1


def test_stream_material_fails_closed_before_authentication_without_creating_a_session() -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer)
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    with pytest.raises(ReadOnlyBrokerError, match="SHADOW01_STREAM_SESSION_MATERIAL_UNAVAILABLE"):
        broker.stream_session_material()

    assert created_sessions[0].login_count == 0
    assert created_sessions[0].authorized_requests == []


def test_second_allowlist_blocks_direct_and_internal_non_read_requests() -> None:
    session = FakeSessionManager(lambda method, endpoint: None)
    transport = SessionManagerReadOnlyTransport(
        session,
        expected_demo_account_id="DEMO-EXPECTED",
    )
    session._request_observer = transport.observe_transport_request

    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_READ_ONLY_REQUEST_DENIED",
    ):
        transport.authorized_request("DELETE", "/session")
    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_READ_ONLY_REQUEST_DENIED",
    ):
        transport.observe_transport_request("POST", "positions/otc")
    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_READ_ONLY_REQUEST_ARGUMENTS_DENIED",
    ):
        transport.authorized_request("GET", "/accounts", headers={"bad": "input"})
    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_READ_ONLY_REQUEST_DENIED",
    ):
        transport.authorized_request("GET", "/markets")
    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_READ_ONLY_REQUEST_DENIED",
    ):
        transport.observe_transport_request("DELETE", "session")

    assert session.login_count == 0
    assert session.authorized_requests == []


def test_bounded_transport_blocks_retries_reauthentication_and_extra_requests_pre_dispatch() -> (
    None
):
    session = FakeSessionManager(lambda method, endpoint: None)
    transport = SessionManagerReadOnlyTransport(
        session,
        expected_demo_account_id="DEMO-EXPECTED",
        maximum_outbound_http_requests=2,
        maximum_authentication_requests=1,
        maximum_requests_per_route=1,
    )
    session._request_observer = transport.observe_transport_request

    assert transport.authorized_request("POST", "/session") is True
    assert transport.authorized_request("GET", "/markets/CS.D.EURGBP.MINI.IP") == {
        "method": "GET",
        "endpoint": "/markets/CS.D.EURGBP.MINI.IP",
    }
    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_READ_ONLY_ROUTE_REQUEST_BUDGET_EXCEEDED",
    ):
        transport.authorized_request("GET", "/markets/CS.D.EURGBP.MINI.IP")
    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_READ_ONLY_OUTBOUND_REQUEST_BUDGET_EXCEEDED",
    ):
        transport.authorized_request("GET", "/markets/CS.D.EURUSD.CFD.IP")

    session._authenticated = False
    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_READ_ONLY_AUTHENTICATION_REQUEST_BUDGET_EXCEEDED",
    ):
        transport.authorized_request("GET", "/markets/CS.D.EURUSD.CFD.IP")

    assert session.login_count == 1
    assert session.authorized_requests == [("GET", "/markets/CS.D.EURGBP.MINI.IP")]
    assert transport.outbound_http_request_count == 2
    assert transport.outbound_authentication_request_count == 1


@pytest.mark.parametrize(
    ("read", "version"),
    (
        (lambda broker: broker.read_session(), "1"),
        (lambda broker: broker.read_account(), "1"),
        (lambda broker: broker.read_market("CS.D.EURGBP.MINI.IP"), "4"),
        (lambda broker: broker.read_market_schedule_v3("CS.D.EURGBP.MINI.IP"), "3"),
        (lambda broker: broker.read_historical_prices("CS.D.EURGBP.MINI.IP", "DAY", 5), "2"),
        (lambda broker: broker.read_historical_prices("CS.D.EURGBP.MINI.IP", "DAY", 300), "2"),
    ),
)
def test_gets_use_only_fixed_internal_ig_api_versions(read: Any, version: str) -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer)
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    read(broker)

    assert created_sessions[0].authorized_request_kwargs == [
        {
            "headers": {
                "VERSION": version,
                "Accept": "application/json; charset=UTF-8",
            }
        }
    ]


def test_v3_shape_contract_is_value_safe_and_records_final_dispatch_version() -> None:
    created_sessions: list[FakeSessionManager] = []
    raw_document = {
        "instrument": {
            "epic": "must-not-escape",
            "SHADOW01_SECRET_INSTRUMENT_KEY_MUST_NOT_ESCAPE": "must-not-escape",
            "openingHours": {
                "marketTimes": [
                    {"openTime": "01:23", "closeTime": "22:34", "secret": "must-not-escape"}
                ]
            },
        },
        "snapshot": {"bid": "must-not-escape", "offer": "must-not-escape"},
        "SHADOW01_SECRET_TOP_LEVEL_KEY_MUST_NOT_ESCAPE": "must-not-escape",
    }

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer, response=FakeResponse(document=raw_document))
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    schedule = broker.read_market_schedule_v3("CS.D.EURGBP.MINI.IP")
    contract = broker.consume_v3_schedule_response_contract()

    assert schedule == {
        "instrument": {
            "openingHours": {"marketTimes": [{"openTime": "01:23", "closeTime": "22:34"}]}
        }
    }
    assert contract == {
        "contract_version": "shadow01-v3-schedule-contract/1",
        "response_status": 200,
        "http_client_dispatch_VERSION": "3",
        "document_is_object": True,
        "top_level_key_names": ["instrument", "snapshot"],
        "top_level_unknown_key_count": 1,
        "instrument": {
            "present": True,
            "type": "object",
            "key_names": ["epic", "openingHours"],
            "unknown_key_count": 1,
        },
        "openingHours": {"present": True, "type": "object"},
        "marketTimes": {"present": True, "type": "array", "count": 1},
        "openTime": {"present": True, "type": "string"},
        "closeTime": {"present": True, "type": "string"},
    }
    assert broker.consume_v3_schedule_response_contract() is None
    assert broker.outbound_http_request_count == 2
    assert "must-not-escape" not in str(contract)
    assert "01:23" not in str(contract)
    assert "22:34" not in str(contract)
    assert "SHADOW01_SECRET_INSTRUMENT_KEY_MUST_NOT_ESCAPE" not in str(contract)
    assert "SHADOW01_SECRET_TOP_LEVEL_KEY_MUST_NOT_ESCAPE" not in str(contract)
    assert created_sessions[0].authorized_request_kwargs[0]["headers"]["VERSION"] == "3"


def test_dedicated_logout_requires_proven_identity_and_never_uses_generic_delete() -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer)
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    assert broker.authenticate() is True
    assert broker.logout() is True

    session = created_sessions[0]
    assert session.observed_requests == [("POST", "session"), ("DELETE", "session")]
    assert session.authorized_requests == []
    assert session.logout_count == 1
    assert session.is_authenticated() is False
    assert broker.request_counters.session_logout_count == 1
    assert broker.request_counters.total_rest_request_count == 2


def test_dedicated_logout_never_authenticates_or_deletes_when_identity_is_unproven() -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer)
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    with pytest.raises(ReadOnlyBrokerError, match="SESSION_LOGOUT_FAILED"):
        broker.logout()

    assert created_sessions[0].login_count == 0
    assert created_sessions[0].logout_count == 0
    assert created_sessions[0].observed_requests == []


def test_dedicated_logout_blocks_a_known_account_mismatch_before_delete() -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer)
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()
    assert broker.authenticate() is True
    created_sessions[0].account_id = "DEMO-OTHER"

    with pytest.raises(ReadOnlyBrokerError, match="SHADOW01_DEMO_ACCOUNT_MISMATCH"):
        broker.logout()

    assert created_sessions[0].logout_count == 0
    assert created_sessions[0].observed_requests == [("POST", "session")]


@pytest.mark.parametrize(
    "unexpected_row_dealing_enabled",
    (None, False, True),
)
def test_account_state_validator_accepts_real_account_row_shape_without_authorizing_execution(
    unexpected_row_dealing_enabled: bool | None,
) -> None:
    configured_account: dict[str, object] = {
        "accountId": "DEMO-EXPECTED",
        "preferred": True,
        "status": "ENABLED",
    }
    if unexpected_row_dealing_enabled is not None:
        configured_account["dealingEnabled"] = unexpected_row_dealing_enabled
    response = FakeResponse(document={"accounts": [configured_account]})
    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=lambda observer: FakeSessionManager(observer, response=response),
    ).build()

    document = broker.read_account()

    assert broker.account_state_is_valid(document) is True
    assert broker.account_state_is_valid({"accounts": []}) is False
    assert broker.execution_authority == "OFF"


@pytest.mark.parametrize(
    "accounts",
    (
        [{"accountId": "DEMO-OTHER", "preferred": True, "status": "ENABLED"}],
        [{"accountId": "DEMO-EXPECTED", "preferred": False, "status": "ENABLED"}],
        [{"accountId": "DEMO-EXPECTED", "preferred": True, "status": "SUSPENDED"}],
        [{"accountId": "DEMO-EXPECTED", "preferred": True, "status": "enabled"}],
        [{"accountId": "DEMO-EXPECTED", "preferred": True}],
    ),
)
def test_account_state_validator_fails_closed_for_wrong_preferred_or_non_enabled_account_row(
    accounts: list[dict[str, object]],
) -> None:
    response = FakeResponse(document={"accounts": accounts})
    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=lambda observer: FakeSessionManager(observer, response=response),
    ).build()

    assert broker.account_state_is_valid(broker.read_account()) is False


@pytest.mark.parametrize(
    "read",
    (
        lambda broker: broker.read_account(),
        lambda broker: broker.read_market("CS.D.EURGBP.MINI.IP"),
        lambda broker: broker.read_historical_prices("CS.D.EURGBP.MINI.IP", "DAY", 300),
    ),
)
@pytest.mark.parametrize(
    ("authenticated_account_id", "reason_code"),
    (
        (None, "SHADOW01_DEMO_ACCOUNT_IDENTITY_UNVERIFIED"),
        ("DEMO-OTHER", "SHADOW01_DEMO_ACCOUNT_MISMATCH"),
    ),
)
def test_gets_are_blocked_before_session_manager_when_account_identity_is_unverified(
    read: Any,
    authenticated_account_id: str | None,
    reason_code: str,
) -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(
            observer,
            authenticated_account_id=authenticated_account_id,
        )
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match=reason_code,
    ):
        read(broker)

    assert created_sessions[0].login_count == 1
    assert created_sessions[0].authorized_requests == []


def test_account_identity_is_rechecked_before_every_get() -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer)
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    assert broker.read_market("CS.D.EURGBP.MINI.IP")["endpoint"] == "/markets/CS.D.EURGBP.MINI.IP"
    created_sessions[0].account_id = "DEMO-OTHER"

    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_DEMO_ACCOUNT_MISMATCH",
    ):
        broker.read_historical_prices("CS.D.EURGBP.MINI.IP", "DAY", 300)

    assert created_sessions[0].authorized_requests == [("GET", "/markets/CS.D.EURGBP.MINI.IP")]


def test_identity_is_rechecked_after_the_session_manager_returns_a_get_response() -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(
            observer,
            account_id_after_authorized_request="DEMO-OTHER",
        )
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_DEMO_ACCOUNT_MISMATCH",
    ):
        broker.read_market("CS.D.EURGBP.MINI.IP")

    assert created_sessions[0].authorized_requests == [("GET", "/markets/CS.D.EURGBP.MINI.IP")]
    assert broker.request_counters.authentication_request_count == 1
    assert broker.request_counters.market_read_count == 1


@pytest.mark.parametrize(
    ("response", "reason_code"),
    (
        (
            FakeResponse(status_code=503, document={"unsafe": "not returned"}),
            "SHADOW01_READ_ONLY_RESPONSE_STATUS_INVALID",
        ),
        (
            FakeResponse(status_code=200, json_error=ValueError("invalid json")),
            "SHADOW01_READ_ONLY_RESPONSE_JSON_INVALID",
        ),
        (
            FakeResponse(status_code=200, document=["not", "a", "mapping"]),
            "SHADOW01_READ_ONLY_RESPONSE_DOCUMENT_INVALID",
        ),
        (object(), "SHADOW01_READ_ONLY_RESPONSE_STATUS_INVALID"),
    ),
)
def test_gets_fail_closed_for_non_success_or_non_mapping_session_responses(
    response: object,
    reason_code: str,
) -> None:
    created_sessions: list[FakeSessionManager] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer, response=response)
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    with pytest.raises(Shadow01LocalDemoReadOnlyError, match=reason_code):
        broker.read_market("CS.D.EURGBP.MINI.IP")

    assert created_sessions[0].authorized_requests == [("GET", "/markets/CS.D.EURGBP.MINI.IP")]
    assert broker.request_counters.authentication_request_count == 1
    assert broker.request_counters.market_read_count == 1


def test_403_read_exposes_only_sanitized_transport_evidence_and_preserves_identity() -> None:
    created_sessions: list[FakeSessionManager] = []
    response = FakeResponse(
        status_code=403,
        document={
            "errorCode": "error.public-api.access-denied",
            "accountId": "account-must-not-leave-boundary",
            "detail": "body-must-not-leave-boundary",
        },
    )

    def session_factory(observer: Any) -> FakeSessionManager:
        session = FakeSessionManager(observer, response=response)
        created_sessions.append(session)
        return session

    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=session_factory,
    ).build()

    with pytest.raises(Shadow01LocalDemoReadOnlyError, match="SHADOW01_READ_ONLY_HTTP_403"):
        broker.read_historical_prices("CS.D.EURGBP.MINI.IP", "DAY", 300)

    assert broker.latest_response_diagnostic() == {
        "status_code": 403,
        "upstream_error_code": "error.public-api.access-denied",
    }
    assert created_sessions[0].account_id == "DEMO-EXPECTED"
    assert created_sessions[0].is_authenticated() is True
    assert "account-must-not-leave-boundary" not in str(broker.latest_response_diagnostic())
    assert "body-must-not-leave-boundary" not in str(broker.latest_response_diagnostic())


def test_403_read_rejects_an_unsafe_upstream_error_code() -> None:
    response = FakeResponse(
        status_code=403,
        document={"errorCode": "unsafe error text with a possible token"},
    )
    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=lambda observer: FakeSessionManager(observer, response=response),
    ).build()

    with pytest.raises(Shadow01LocalDemoReadOnlyError, match="SHADOW01_READ_ONLY_HTTP_403"):
        broker.read_market("CS.D.EURGBP.MINI.IP")

    assert broker.latest_response_diagnostic() == {
        "status_code": 403,
        "upstream_error_code": None,
    }


def test_403_logout_exposes_only_sanitized_transport_evidence() -> None:
    logout_response = FakeResponse(
        status_code=403,
        document={
            "errorCode": "error.security.client-token-invalid",
            "accountId": "account-must-not-leave-boundary",
        },
    )
    broker = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(),
        session_factory=lambda observer: FakeSessionManager(
            observer,
            logout_succeeds=False,
            logout_response=logout_response,
        ),
    ).build()

    assert broker.authenticate() is True
    with pytest.raises(ReadOnlyBrokerError, match="SHADOW01_DEMO_SESSION_LOGOUT_FAILED"):
        broker.logout()

    assert broker.latest_response_diagnostic() == {
        "status_code": 403,
        "upstream_error_code": "error.security.client-token-invalid",
    }
    assert "account-must-not-leave-boundary" not in str(broker.latest_response_diagnostic())


def test_factory_refuses_to_construct_when_status_is_not_ready() -> None:
    session_factory_calls: list[Any] = []

    def session_factory(observer: Any) -> FakeSessionManager:
        session_factory_calls.append(observer)
        return FakeSessionManager(observer)

    factory = Shadow01LocalDemoReadOnlyFactory(
        settings_provider=lambda: _demo_settings(ig_demo=False),
        session_factory=session_factory,
    )

    with pytest.raises(
        Shadow01LocalDemoReadOnlyError,
        match="SHADOW01_DEMO_MODE_REQUIRED",
    ):
        factory.build()

    assert session_factory_calls == []


def test_local_factory_and_transport_offer_no_order_or_position_actions() -> None:
    forbidden_fragments = ("order", "position", "close", "action")
    for subject in (Shadow01LocalDemoReadOnlyFactory, SessionManagerReadOnlyTransport):
        public_names = {name.lower() for name in dir(subject) if not name.startswith("_")}
        assert not any(
            fragment in public_name
            for fragment in forbidden_fragments
            for public_name in public_names
        )
