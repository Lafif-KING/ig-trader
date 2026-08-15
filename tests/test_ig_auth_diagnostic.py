"""Offline tests for the G1-01 IG Demo read-only diagnostic."""

from __future__ import annotations

import json
import tempfile
from argparse import Namespace
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import truststore

import tools.ig_auth_diagnostic as diagnostic_module
from tools.ig_auth_diagnostic import (
    ACCOUNT_ENV_NAMES,
    DEMO_REST_BASE_URL,
    SECRET_ENV_NAMES,
    Classification,
    DiagnosticConfig,
    DiagnosticError,
    DiagnosticRunner,
    EndpointBlockedError,
    SafeRestClient,
    StreamingProbe,
    _configuration_failure_document,
    build_stream_password,
    create_system_trust_lightstreamer_client,
    endpoint_is_allowed,
    lightstreamer_subscription_error_category,
    load_config,
    write_reports,
)
from tools.ig_auth_trading_ig_reference import (
    DiagnosticError as ReferenceDiagnosticError,
)
from tools.ig_auth_trading_ig_reference import ReferenceRequestGuard

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
ACCOUNT = "DEMO-ACCOUNT-SECRET"


@pytest.fixture
def tmp_path() -> Path:
    """Avoid Pytest's hanging Windows per-test convenience symlink."""

    base = Path(".runtime/pytest-g1-local").resolve()
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="case-", dir=base))


class SequenceClient:
    """Deterministic HTTPX-shaped client that never reaches the network."""

    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


class FakeConnectionDetails:
    def __init__(self) -> None:
        self.user: str | None = None
        self.password: str | None = None

    def setUser(self, value: str) -> None:  # noqa: N802
        self.user = value

    def setPassword(self, value: str) -> None:  # noqa: N802
        self.password = value


class FakeItemUpdate:
    def getValue(self, field: str) -> str | None:  # noqa: N802
        return {"BID": "1.0000", "OFFER": "1.0002"}.get(field)


class FakeSubscription:
    def __init__(self, mode: str, items: list[str], fields: list[str]) -> None:
        self.mode = mode
        self.items = items
        self.fields = fields
        self.listener: Any = None

    def addListener(self, listener: Any) -> None:  # noqa: N802
        self.listener = listener


class FakeLightstreamerClient:
    def __init__(self, endpoint: str, adapter_set: str | None) -> None:
        self.endpoint = endpoint
        self.adapter_set = adapter_set
        self.connectionDetails = FakeConnectionDetails()
        self.listener: Any = None
        self.subscription: FakeSubscription | None = None

    def addListener(self, listener: Any) -> None:  # noqa: N802
        self.listener = listener

    def connect(self) -> None:
        self.listener.onStatusChange("CONNECTED:STREAM-SENSING")

    def subscribe(self, subscription: FakeSubscription) -> None:
        self.subscription = subscription
        subscription.listener.onSubscription()
        subscription.listener.onItemUpdate(FakeItemUpdate())

    def disconnect(self) -> None:
        self.listener.onStatusChange("DISCONNECTED")


class NoStatusLightstreamerClient(FakeLightstreamerClient):
    def connect(self) -> None:
        return None


class RejectingLightstreamerClient(FakeLightstreamerClient):
    instances: list[RejectingLightstreamerClient] = []

    def __init__(self, endpoint: str, adapter_set: str | None) -> None:
        super().__init__(endpoint, adapter_set)
        self.disconnected = False
        self.__class__.instances.append(self)

    def subscribe(self, subscription: FakeSubscription) -> None:
        self.subscription = subscription
        subscription.listener.onSubscriptionError(
            -1,
            "deliberately discarded server text with DEMO-ACCOUNT-SECRET",
        )

    def disconnect(self) -> None:
        self.disconnected = True
        super().disconnect()


class RejectingProbeFactory:
    def __call__(self, **kwargs: Any) -> StreamingProbe:
        return StreamingProbe(
            **kwargs,
            client_factory=RejectingLightstreamerClient,
            subscription_factory=FakeSubscription,
            clock=lambda: NOW,
        )


class FakeRunnerProbe:
    """No-network probe for end-to-end runner tests."""

    instances: list[FakeRunnerProbe] = []

    def __init__(self, **kwargs: Any) -> None:
        self.endpoint_hostname = "demo-stream.example.test"
        self.status_history = kwargs["status_history"]
        self.quote_listener = SimpleNamespace(
            first_quote_at=NOW,
            bid_present=True,
            offer_present=True,
        )
        self.client = SimpleNamespace(disconnect=lambda: None)
        self.disconnected = False
        self.__class__.instances.append(self)

    def connect_and_wait(self, *, connect_timeout: float, quote_timeout: float) -> None:
        assert connect_timeout > 0
        assert quote_timeout > 0
        self.status_history.append(
            {"timestamp_utc": "2026-08-15T12:00:00Z", "status": "CONNECTED:WS-STREAMING"}
        )

    def require_fresh_quote(self, maximum_age_seconds: float) -> float:
        assert maximum_age_seconds > 0
        return 0.1

    def disconnect_and_wait(self, timeout: float) -> None:
        assert timeout > 0
        self.disconnected = True
        self.status_history.append(
            {"timestamp_utc": "2026-08-15T12:00:01Z", "status": "DISCONNECTED"}
        )


def config(tmp_path: Path, **overrides: Any) -> DiagnosticConfig:
    values: dict[str, Any] = {
        "environment": "demo",
        "session_version": 2,
        "epic": "CS.D.EURGBP.MINI.IP",
        "output": tmp_path / "diagnostic.json",
        "base_url": DEMO_REST_BASE_URL,
        "paper_trading": True,
        "api_key": "API-KEY-SECRET",
        "identifier": "IDENTIFIER-SECRET",
        "password": "PASSWORD-SECRET",
        "account_id": ACCOUNT,
        "connect_timeout_seconds": 0.01,
        "quote_timeout_seconds": 0.01,
        "disconnect_timeout_seconds": 0.01,
        "maximum_quote_age_seconds": 5.0,
        "max_request_attempts": 2,
        "max_reauth_attempts": 2,
    }
    values.update(overrides)
    return DiagnosticConfig(**values)


def cli_args(tmp_path: Path, **overrides: Any) -> Namespace:
    values: dict[str, Any] = {
        "environment": "demo",
        "session_version": 2,
        "epic": "CS.D.EURGBP.MINI.IP",
        "output": tmp_path / "diagnostic.json",
        "connect_timeout": 1.0,
        "quote_timeout": 1.0,
        "disconnect_timeout": 1.0,
        "maximum_quote_age": 1.0,
    }
    values.update(overrides)
    return Namespace(**values)


def configure_demo_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in {*SECRET_ENV_NAMES, *ACCOUNT_ENV_NAMES, "IG_DEMO", "IG_BASE_URL", "PAPER_TRADING"}:
        monkeypatch.delenv(name, raising=False)
    for name in tuple(__import__("os").environ):
        if name.upper().startswith("IG_LIVE"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IG_DEMO", "true")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("IG_BASE_URL", DEMO_REST_BASE_URL)
    monkeypatch.setenv("IG_API_KEY", "API-KEY-SECRET")
    monkeypatch.setenv("IG_IDENTIFIER", "IDENTIFIER-SECRET")
    monkeypatch.setenv("IG_PASSWORD", "PASSWORD-SECRET")
    monkeypatch.setenv("IG_ACCOUNT_ID", ACCOUNT)


def response(
    status: int,
    payload: object | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    if status == 204:
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, json=payload, headers=headers)


def login_response(
    *,
    account: str = ACCOUNT,
    cst: str = "CST-SECRET",
    xst: str = "XST-SECRET",
) -> httpx.Response:
    headers = {}
    if cst:
        headers["CST"] = cst
    if xst:
        headers["X-SECURITY-TOKEN"] = xst
    return response(
        200,
        {
            "currentAccountId": account,
            "lightstreamerEndpoint": "https://demo-stream.example.test",
        },
        headers=headers,
    )


def account_response(
    *,
    preferred: bool | None = True,
    dealing_enabled: bool = True,
    status: str = "ENABLED",
) -> httpx.Response:
    account: dict[str, Any] = {
        "accountId": ACCOUNT,
        "accountType": "CFD",
        "dealingEnabled": dealing_enabled,
        "status": status,
    }
    if preferred is not None:
        account["preferred"] = preferred
    return response(200, {"accounts": [account]})


def successful_round() -> list[httpx.Response]:
    return [
        login_response(),
        response(200, {"accountId": ACCOUNT, "currency": "EUR"}),
        account_response(),
        response(200, {"instrument": {"epic": "CS.D.EURGBP.MINI.IP"}}),
        response(204),
    ]


def runner_with_responses(
    tmp_path: Path,
    responses: list[httpx.Response | Exception],
    *,
    diagnostic_config: DiagnosticConfig | None = None,
    stream_factory: Any = FakeRunnerProbe,
) -> tuple[DiagnosticRunner, SequenceClient]:
    selected_config = diagnostic_config or config(tmp_path)
    client = SequenceClient(responses)
    rest = SafeRestClient(
        selected_config,
        [],
        client=client,
        clock=lambda: NOW,
        monotonic=lambda: 1.0,
    )
    runner = DiagnosticRunner(
        selected_config,
        rest_client=rest,
        stream_factory=stream_factory,
        fingerprint_key=b"test-only-fingerprint-key",
    )
    return runner, client


def test_demo_hostname_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_demo_environment(monkeypatch)

    loaded = load_config(cli_args(tmp_path), dotenv_path=tmp_path / "absent.env")

    assert loaded.environment == "demo"
    assert loaded.base_url == DEMO_REST_BASE_URL
    assert loaded.paper_trading is True


def test_live_hostname_is_rejected_before_credentials_are_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_demo_environment(monkeypatch)
    monkeypatch.setenv("IG_BASE_URL", "https://api.ig.com/gateway/deal")

    with pytest.raises(DiagnosticError, match="DEMO_HOSTNAME_REQUIRED"):
        load_config(cli_args(tmp_path), dotenv_path=tmp_path / "absent.env")


def test_live_named_credentials_are_rejected_without_loading_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_demo_environment(monkeypatch)
    dotenv = tmp_path / ".env"
    dotenv.write_text("IG_LIVE_PASSWORD=NEVER-LOAD-THIS\n", encoding="utf-8")

    with pytest.raises(DiagnosticError, match="LIVE_CREDENTIAL_CONFIGURATION_PRESENT"):
        load_config(cli_args(tmp_path), dotenv_path=dotenv)


def test_missing_api_key_is_a_configuration_defect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_demo_environment(monkeypatch)
    monkeypatch.delenv("IG_API_KEY")

    with pytest.raises(DiagnosticError, match="MISSING_REQUIRED_CONFIG:IG_API_KEY"):
        load_config(cli_args(tmp_path), dotenv_path=tmp_path / "absent.env")


def test_missing_config_report_preserves_validated_demo_hostname(tmp_path: Path) -> None:
    error = DiagnosticError(
        Classification.CONFIGURATION_DEFECT,
        "MISSING_REQUIRED_CONFIG:IG_ACCOUNT_ID",
    )

    document = _configuration_failure_document(cli_args(tmp_path), error)

    assert document["sanitized_base_hostname"] == "demo-api.ig.com"
    assert document["preflight"]["demo_hostname_accepted"] is True
    assert document["preflight"]["live_hostname_absent"] is True
    assert document["preflight"]["required_configuration_present"] is False


def test_nonconfigured_epic_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_demo_environment(monkeypatch)

    with pytest.raises(DiagnosticError, match="CONFIGURED_EPIC_REQUIRED"):
        load_config(
            cli_args(tmp_path, epic="CS.D.EURUSD.MINI.IP"),
            dotenv_path=tmp_path / "absent.env",
        )


@pytest.mark.parametrize(
    ("status", "error_code", "classification"),
    [
        (403, "error.security.api-key-invalid", Classification.API_KEY_RESTRICTION),
        (
            401,
            "error.security.invalid-details",
            Classification.CONFIGURATION_DEFECT,
        ),
        (
            403,
            "error.security.client-account.kyc-required",
            Classification.ACCOUNT_RESTRICTION,
        ),
        (
            403,
            "error.public-api.failure.pending-agreements",
            Classification.ACCOUNT_RESTRICTION,
        ),
    ],
)
def test_auth_failure_preserves_exact_status_and_error_code(
    tmp_path: Path,
    status: int,
    error_code: str,
    classification: Classification,
) -> None:
    runner, client = runner_with_responses(
        tmp_path,
        [response(status, {"errorCode": error_code})],
    )

    evidence = runner.run()

    assert evidence["final_classification"] == classification.value
    assert evidence["http_status_history"][0]["http_status"] == status
    assert evidence["http_status_history"][0]["ig_errorCode"] == error_code
    assert evidence["ig_errorCode_history"][0]["ig_errorCode"] == error_code
    assert evidence["order_endpoint_call_count"] == 0
    assert client.closed is True


@pytest.mark.parametrize(
    ("cst", "xst", "reason"),
    [
        ("", "XST-SECRET", "CST_MISSING"),
        ("CST-SECRET", "", "X_SECURITY_TOKEN_MISSING"),
    ],
)
def test_missing_v2_token_blocks_streaming(
    tmp_path: Path,
    cst: str,
    xst: str,
    reason: str,
) -> None:
    runner, _client = runner_with_responses(
        tmp_path,
        [login_response(cst=cst, xst=xst)],
    )

    evidence = runner.run()

    assert evidence["final_classification"] == Classification.STREAMING_HANDSHAKE_FAILURE.value
    assert evidence["remaining_blocker"] == reason
    assert evidence["lightstreamer"]["subscription_status"] == "NOT_ATTEMPTED"


def test_account_mismatch_blocks_all_following_actions(tmp_path: Path) -> None:
    runner, client = runner_with_responses(
        tmp_path,
        [login_response(account="OTHER-DEMO-ACCOUNT")],
    )

    evidence = runner.run()

    assert evidence["final_classification"] == Classification.CONFIGURATION_DEFECT.value
    assert evidence["remaining_blocker"] == "ACCOUNT_MISMATCH"
    assert evidence["account"]["account_match"] is False
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("account_result", "classification", "reason"),
    [
        (
            account_response(preferred=None),
            Classification.CONFIGURATION_DEFECT,
            "PREFERRED_ACCOUNT_NOT_SET",
        ),
        (
            account_response(preferred=True, dealing_enabled=False),
            Classification.ACCOUNT_RESTRICTION,
            "PREFERRED_ACCOUNT_DISABLED",
        ),
        (
            account_response(preferred=True, status="SUSPENDED"),
            Classification.ACCOUNT_RESTRICTION,
            "PREFERRED_ACCOUNT_DISABLED",
        ),
    ],
)
def test_preferred_account_failures_block_streaming(
    tmp_path: Path,
    account_result: httpx.Response,
    classification: Classification,
    reason: str,
) -> None:
    runner, _client = runner_with_responses(
        tmp_path,
        [
            login_response(),
            response(200, {"accountId": ACCOUNT}),
            account_result,
            response(204),
        ],
    )

    evidence = runner.run()

    assert evidence["final_classification"] == classification.value
    assert evidence["remaining_blocker"] == reason
    assert evidence["lightstreamer"]["subscription_status"] == "NOT_ATTEMPTED"
    assert evidence["session_cleanup"] == "LOGGED_OUT"


def test_oauth_token_is_rejected_for_lightstreamer() -> None:
    with pytest.raises(DiagnosticError, match="LIGHTSTREAMER_REQUIRES_CST_XST"):
        build_stream_password("Bearer oauth-access-token", "XST-SECRET")


def test_streaming_connection_timeout_is_explicit() -> None:
    probe = StreamingProbe(
        endpoint="https://demo-stream.example.test",
        account_id=ACCOUNT,
        cst="CST-SECRET",
        xst="XST-SECRET",
        epic="CS.D.EURGBP.MINI.IP",
        status_history=[],
        client_factory=NoStatusLightstreamerClient,
        subscription_factory=FakeSubscription,
        clock=lambda: NOW,
    )

    with pytest.raises(DiagnosticError, match="LIGHTSTREAMER_CONNECTION_TIMEOUT"):
        probe.connect_and_wait(connect_timeout=0.001, quote_timeout=0.001)


def test_subscription_rejection_records_only_numeric_code_and_category(
    tmp_path: Path,
) -> None:
    RejectingLightstreamerClient.instances = []
    runner, _client = runner_with_responses(
        tmp_path,
        successful_round()[:-1] + [response(204)],
        stream_factory=RejectingProbeFactory(),
    )

    evidence = runner.run()

    stream = evidence["lightstreamer"]
    assert evidence["final_classification"] == (Classification.STREAMING_HANDSHAKE_FAILURE.value)
    assert evidence["remaining_blocker"] == "LIGHTSTREAMER_SUBSCRIPTION_REJECTED"
    assert stream["subscription_status"] == "REJECTED"
    assert stream["subscription_error_code"] == -1
    assert stream["subscription_error_category"] == "METADATA_ADAPTER_REJECTED"
    assert stream["failure_cleanup"] == "DISCONNECTED"
    assert evidence["session_cleanup"] == "LOGGED_OUT"
    assert RejectingLightstreamerClient.instances[0].disconnected is True
    assert "deliberately discarded" not in json.dumps(evidence)


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (-1, "METADATA_ADAPTER_REJECTED"),
        (17, "DATA_ADAPTER_INVALID"),
        (23, "SCHEMA_INVALID"),
        (68, "SERVER_INTERNAL_ERROR"),
        (999, "SERVER_REJECTED"),
    ],
)
def test_lightstreamer_subscription_error_categories(
    code: int,
    category: str,
) -> None:
    assert lightstreamer_subscription_error_category(code) == category


def test_lightstreamer_factory_installs_verified_system_trust_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        contexts: list[Any] = []
        instances: list[tuple[str, str | None]] = []

        @staticmethod
        def setTrustManagerFactory(context: Any) -> None:  # noqa: N802
            FakeClient.contexts.append(context)

        def __init__(self, endpoint: str, adapter_set: str | None) -> None:
            self.__class__.instances.append((endpoint, adapter_set))

    monkeypatch.setattr(diagnostic_module, "LightstreamerClient", FakeClient)
    monkeypatch.setattr(diagnostic_module, "_lightstreamer_trust_configured", False)

    first = create_system_trust_lightstreamer_client("https://stream.example.test", None)
    second = create_system_trust_lightstreamer_client("https://stream.example.test", None)

    assert isinstance(first, FakeClient)
    assert isinstance(second, FakeClient)
    assert len(FakeClient.contexts) == 1
    context = FakeClient.contexts[0]
    assert isinstance(context, truststore.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode.name == "CERT_REQUIRED"
    assert FakeClient.instances == [
        ("https://stream.example.test", None),
        ("https://stream.example.test", None),
    ]


def test_stale_price_stream_is_rejected() -> None:
    probe = StreamingProbe(
        endpoint="https://demo-stream.example.test",
        account_id=ACCOUNT,
        cst="CST-SECRET",
        xst="XST-SECRET",
        epic="CS.D.EURGBP.MINI.IP",
        status_history=[],
        client_factory=FakeLightstreamerClient,
        subscription_factory=FakeSubscription,
        clock=lambda: NOW,
    )
    probe.quote_listener.first_quote_at = NOW - timedelta(seconds=10)

    with pytest.raises(DiagnosticError, match="STALE_PRICE_STREAM"):
        probe.require_fresh_quote(5.0)


def test_forced_disconnect_is_observed_and_subscription_receives_quote() -> None:
    history: list[dict[str, str]] = []
    probe = StreamingProbe(
        endpoint="https://demo-stream.example.test",
        account_id=ACCOUNT,
        cst="CST-SECRET",
        xst="XST-SECRET",
        epic="CS.D.EURGBP.MINI.IP",
        status_history=history,
        client_factory=FakeLightstreamerClient,
        subscription_factory=FakeSubscription,
        clock=lambda: NOW,
    )

    probe.connect_and_wait(connect_timeout=0.01, quote_timeout=0.01)
    probe.disconnect_and_wait(0.01)

    assert probe.subscription.items == ["MARKET:CS.D.EURGBP.MINI.IP"]
    assert probe.subscription.fields == ["UPDATE_TIME", "BID", "OFFER"]
    assert probe.quote_listener.bid_present is True
    assert probe.quote_listener.offer_present is True
    assert [item["status"] for item in history] == [
        "CONNECTED:STREAM-SENSING",
        "DISCONNECTED",
    ]


def test_rest_401_reauthentication_is_bounded(tmp_path: Path) -> None:
    selected_config = replace(config(tmp_path), max_reauth_attempts=1)
    runner, client = runner_with_responses(
        tmp_path,
        [
            login_response(),
            response(401, {"errorCode": "error.security.client-token-invalid"}),
            login_response(),
            response(401, {"errorCode": "error.security.client-token-invalid"}),
            response(204),
        ],
        diagnostic_config=selected_config,
    )

    evidence = runner.run()

    assert evidence["bounded_reauthentication"] == {"limit": 1, "count": 1}
    assert evidence["final_classification"] == Classification.CONFIGURATION_DEFECT.value
    assert (
        sum(
            1
            for method, url, _kwargs in client.calls
            if method == "POST" and url.endswith("/session")
        )
        == 2
    )
    assert len(client.calls) == 5
    assert evidence["session_cleanup"] == "LOGGED_OUT"


def test_invalid_token_reauthenticates_and_restores_full_probe(tmp_path: Path) -> None:
    runner, client = runner_with_responses(
        tmp_path,
        [
            login_response(),
            response(401, {"errorCode": "error.security.account-token-invalid"}),
            *successful_round()[:-1],
            *successful_round(),
        ],
    )

    evidence = runner.run()

    assert evidence["final_classification"] == Classification.PASS.value
    assert evidence["bounded_reauthentication"] == {"limit": 2, "count": 2}
    assert (
        sum(
            1
            for method, url, _kwargs in client.calls
            if method == "POST" and url.endswith("/session")
        )
        == 3
    )


def test_live_rerouting_response_blocks_streaming(tmp_path: Path) -> None:
    rerouted = response(
        200,
        {
            "currentAccountId": ACCOUNT,
            "lightstreamerEndpoint": "https://demo-stream.example.test",
            "reroutingEnvironment": "LIVE",
        },
        headers={"CST": "CST-SECRET", "X-SECURITY-TOKEN": "XST-SECRET"},
    )
    runner, client = runner_with_responses(tmp_path, [rerouted])

    evidence = runner.run()

    assert evidence["final_classification"] == Classification.ACCOUNT_RESTRICTION.value
    assert evidence["remaining_blocker"] == "DEMO_ACCOUNT_NOT_CONFIRMED"
    assert evidence["account"]["demo_account_confirmed"] is False
    assert len(client.calls) == 1


def test_malformed_response_is_recorded_and_not_silently_caught(tmp_path: Path) -> None:
    selected_config = config(tmp_path)
    client = SequenceClient([httpx.Response(200, text="not-json")])
    history: list[dict[str, Any]] = []
    rest = SafeRestClient(
        selected_config,
        history,
        client=client,
        clock=lambda: NOW,
        monotonic=lambda: 1.0,
    )

    with pytest.raises(DiagnosticError, match="MALFORMED_JSON_RESPONSE"):
        rest.request_json("GET", "/accounts", version="1")

    assert history[0]["http_status"] == 200
    assert history[0]["ig_errorCode"] is None


def test_endpoint_allow_list_accepts_only_declared_read_operations() -> None:
    assert endpoint_is_allowed("POST", "/session") is True
    assert endpoint_is_allowed("GET", "/session") is True
    assert endpoint_is_allowed("GET", "/accounts") is True
    assert endpoint_is_allowed("GET", "/markets/CS.D.EURGBP.MINI.IP") is True
    assert endpoint_is_allowed("GET", "/markets?searchTerm=EURGBP") is True
    assert endpoint_is_allowed("DELETE", "/session") is True
    assert endpoint_is_allowed("GET", "/positions") is False
    assert endpoint_is_allowed("GET", "/history/transactions") is False


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/positions/otc"),
        ("PUT", "/positions/otc/DEAL-ID"),
        ("DELETE", "/positions/otc/DEAL-ID"),
        ("POST", "/workingorders/otc"),
        ("PUT", "/workingorders/otc/DEAL-ID"),
        ("DELETE", "/workingorders/otc/DEAL-ID"),
    ],
)
def test_every_order_endpoint_is_blocked_before_network(
    tmp_path: Path,
    method: str,
    path: str,
) -> None:
    selected_config = config(tmp_path)
    client = SequenceClient([])
    rest = SafeRestClient(selected_config, [], client=client)

    with pytest.raises(EndpointBlockedError):
        rest.request_json(method, path, version="2")

    assert client.calls == []
    assert rest.order_endpoint_call_count == 0
    assert rest.blocked_endpoint_attempt_count == 1


def test_wrong_api_version_is_blocked_before_network(tmp_path: Path) -> None:
    selected_config = config(tmp_path)
    client = SequenceClient([])
    rest = SafeRestClient(selected_config, [], client=client)

    with pytest.raises(EndpointBlockedError):
        rest.request_json("POST", "/session", version="3")

    assert client.calls == []
    assert rest.order_endpoint_call_count == 0
    assert rest.blocked_endpoint_attempt_count == 1


def test_reference_request_guard_blocks_live_and_order_endpoints() -> None:
    guard = ReferenceRequestGuard()
    guard.validate("GET", "https://demo-api.ig.com/gateway/deal/accounts")

    with pytest.raises(
        ReferenceDiagnosticError,
        match="TRADING_IG_NON_DEMO_HOST_BLOCKED",
    ):
        guard.validate("GET", "https://api.ig.com/gateway/deal/accounts")
    with pytest.raises(ReferenceDiagnosticError, match="TRADING_IG_ENDPOINT_BLOCKED"):
        guard.validate("POST", "https://demo-api.ig.com/gateway/deal/positions/otc")

    assert guard.network_request_count == 1
    assert guard.order_endpoint_call_count == 0
    assert guard.blocked_endpoint_attempt_count == 2


def test_forced_reconnect_runner_passes_with_one_active_connection(tmp_path: Path) -> None:
    FakeRunnerProbe.instances = []
    runner, client = runner_with_responses(
        tmp_path,
        successful_round()[:-1] + successful_round(),
    )

    evidence = runner.run()

    assert evidence["final_classification"] == Classification.PASS.value
    assert evidence["account"]["account_match"] is True
    assert evidence["account"]["configured_account_exists"] is True
    assert evidence["tokens"] == {
        "cst_present": True,
        "x_security_token_present": True,
    }
    assert evidence["lightstreamer"]["subscription_status"] == ("SUBSCRIBED_QUOTE_RECEIVED")
    assert evidence["lightstreamer"]["forced_reconnect_result"] == (
        "REAUTHENTICATED_AND_SUBSCRIPTION_RESTORED"
    )
    assert evidence["lightstreamer"]["active_connection_high_watermark"] == 1
    assert evidence["bounded_reauthentication"]["count"] == 1
    assert evidence["order_endpoint_call_count"] == 0
    assert len(client.calls) == 9
    assert [
        (method, url.rsplit("/", maxsplit=1)[-1])
        for method, url, _kwargs in client.calls
        if method in {"POST", "DELETE"}
    ] == [("POST", "session"), ("POST", "session"), ("DELETE", "session")]
    assert len(FakeRunnerProbe.instances) == 2
    assert all(probe.disconnected for probe in FakeRunnerProbe.instances)


def test_report_secret_redaction_and_required_output(tmp_path: Path) -> None:
    runner, _client = runner_with_responses(
        tmp_path,
        [response(403, {"errorCode": "error.security.invalid-details"})],
    )
    evidence = runner.run()

    json_path, markdown_path = write_reports(
        evidence,
        tmp_path / "g1-auth-diagnostic.json",
        secrets=runner.config.secret_values(),
    )

    combined = json_path.read_text(encoding="utf-8") + markdown_path.read_text(encoding="utf-8")
    for secret in runner.config.secret_values():
        assert secret not in combined
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["secret_scan_result"] == "PASS"
    assert document["order_endpoint_call_count"] == 0
    assert document["final_classification"] in {item.value for item in Classification}
