"""Development-only REST comparison using the optional ``trading-ig`` package.

This is not a production adapter. It intentionally excludes streaming and all
trading methods; an injected requests session blocks non-allow-listed endpoints
before transmission.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

DEMO_HOST = "demo-api.ig.com"
DEMO_BASE_URL = f"https://{DEMO_HOST}/gateway/deal"
DEFAULT_EPIC = "CS.D.EURGBP.MINI.IP"
DEFAULT_REFERENCE_OUTPUT = Path(".runtime/evidence/g1-auth-trading-ig-reference.json")
EPIC_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
ACCOUNT_ENV_NAMES = (
    "IG_ACCOUNT_ID",
    "IG_ACCOUNT_NUMBER",
    "IG_SERVICE_ACC_NUMBER",
)
SECRET_ENV_NAMES = ("IG_API_KEY", "IG_IDENTIFIER", "IG_PASSWORD", *ACCOUNT_ENV_NAMES)


class Classification(StrEnum):
    PASS = "PASS"
    CODE_DEFECT = "CODE_DEFECT"
    CONFIGURATION_DEFECT = "CONFIGURATION_DEFECT"
    ACCOUNT_RESTRICTION = "ACCOUNT_RESTRICTION"
    API_KEY_RESTRICTION = "API_KEY_RESTRICTION"
    IG_PLATFORM_FAILURE = "IG_PLATFORM_FAILURE"
    INCONCLUSIVE = "INCONCLUSIVE"


class DiagnosticError(RuntimeError):
    def __init__(self, classification: Classification, reason: str) -> None:
        super().__init__(reason)
        self.classification = classification
        self.reason = reason


@dataclass(frozen=True)
class ReferenceConfig:
    identifier: str = field(repr=False)
    password: str = field(repr=False)
    api_key: str = field(repr=False)
    account_id: str = field(repr=False)
    epic: str
    output: Path

    def secret_values(self) -> tuple[str, ...]:
        return (self.identifier, self.password, self.api_key, self.account_id)


def utc_text() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sanitize_path(path: str) -> str:
    return urlparse(path).path or "/"


def endpoint_is_allowed(method: str, path: str) -> bool:
    method = method.upper()
    parsed = urlparse(path)
    clean_path = parsed.path
    query = parse_qs(parsed.query, keep_blank_values=True)
    if method == "POST" and clean_path == "/session" and not query:
        return True
    if method == "DELETE" and clean_path == "/session" and not query:
        return True
    if method == "GET" and clean_path in {"/session", "/accounts"} and not query:
        return True
    if method == "GET" and clean_path.startswith("/markets/") and not query:
        return bool(EPIC_PATTERN.fullmatch(clean_path.removeprefix("/markets/")))
    return False


def endpoint_version_is_allowed(method: str, path: str, version: str) -> bool:
    clean_path = urlparse(path).path
    key = (method.upper(), clean_path)
    exact_versions = {
        ("POST", "/session"): "2",
        ("DELETE", "/session"): "1",
        ("GET", "/session"): "1",
        ("GET", "/accounts"): "1",
    }
    if key in exact_versions:
        return version == exact_versions[key]
    if method.upper() == "GET" and clean_path.startswith("/markets/"):
        return version == "3"
    return False


def classify_http_failure(status: int, error_code: str | None) -> Classification:
    normalized = (error_code or "").lower()
    if "api-key" in normalized or "apikey" in normalized:
        return Classification.API_KEY_RESTRICTION
    if any(term in normalized for term in ("kyc", "agreement", "account-suspended")):
        return Classification.ACCOUNT_RESTRICTION
    if status >= 500:
        return Classification.IG_PLATFORM_FAILURE
    if status in {400, 401}:
        return Classification.CONFIGURATION_DEFECT
    return Classification.INCONCLUSIVE


def _dotenv_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        keys.add(key)
    return keys


def _selected_dotenv(path: Path, names: set[str]) -> dict[str, str]:
    if not path.exists():
        return {}
    selected: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        if key not in names:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if "${" in value:
            raise DiagnosticError(
                Classification.CONFIGURATION_DEFECT,
                f"DOTENV_INTERPOLATION_UNSUPPORTED:{key}",
            )
        selected[key] = value
    return selected


def _value(name: str, selected: Mapping[str, str]) -> str:
    return os.environ.get(name, selected.get(name, "")).strip()


def _parse_true(value: str, reason: str) -> None:
    if value and value.lower() not in {"1", "true", "yes", "on"}:
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, reason)


def load_config(args: argparse.Namespace) -> ReferenceConfig:
    if args.environment != "demo" or args.session_version != 2:
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "DEMO_V2_REQUIRED")
    if args.epic != DEFAULT_EPIC:
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "CONFIGURED_EPIC_REQUIRED")
    dotenv_path = Path(args.dotenv)
    live_keys = [
        key
        for key in {*_dotenv_keys(dotenv_path), *os.environ}
        if key.upper().startswith("IG_LIVE")
    ]
    if live_keys:
        raise DiagnosticError(
            Classification.CONFIGURATION_DEFECT,
            "LIVE_CREDENTIAL_CONFIGURATION_PRESENT",
        )
    preflight = _selected_dotenv(
        dotenv_path,
        {"IG_BASE_URL", "IG_DEMO", "PAPER_TRADING"},
    )
    base_url = _value("IG_BASE_URL", preflight) or DEMO_BASE_URL
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.hostname != DEMO_HOST:
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "DEMO_HOSTNAME_REQUIRED")
    _parse_true(_value("IG_DEMO", preflight), "IG_DEMO_MUST_BE_TRUE")
    _parse_true(_value("PAPER_TRADING", preflight), "PAPER_TRADING_MUST_BE_TRUE")

    secrets = _selected_dotenv(dotenv_path, set(SECRET_ENV_NAMES))
    account_values = {_value(name, secrets) for name in ACCOUNT_ENV_NAMES if _value(name, secrets)}
    if len(account_values) != 1:
        raise DiagnosticError(
            Classification.CONFIGURATION_DEFECT,
            "ACCOUNT_CONFIG_MISSING_OR_AMBIGUOUS",
        )
    required = {
        "identifier": _value("IG_IDENTIFIER", secrets),
        "password": _value("IG_PASSWORD", secrets),
        "api_key": _value("IG_API_KEY", secrets),
        "account_id": next(iter(account_values)),
    }
    if not all(required.values()):
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "REQUIRED_CONFIG_MISSING")
    return ReferenceConfig(
        **required,
        epic=args.epic,
        output=Path(args.output),
    )


class ReferenceRequestGuard:
    """Validate requests made by the optional reference package."""

    def __init__(self) -> None:
        self.network_request_count = 0
        self.order_endpoint_call_count = 0
        self.blocked_endpoint_attempt_count = 0

    def validate(
        self,
        method: str,
        url: str,
        version: str | None = None,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "demo-api.ig.com":
            self.blocked_endpoint_attempt_count += 1
            raise DiagnosticError(
                Classification.CONFIGURATION_DEFECT,
                "TRADING_IG_NON_DEMO_HOST_BLOCKED",
            )
        path = parsed.path.removeprefix("/gateway/deal") or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        if not endpoint_is_allowed(method, path) or not endpoint_version_is_allowed(
            method,
            path,
            version or "1",
        ):
            self.blocked_endpoint_attempt_count += 1
            raise DiagnosticError(
                Classification.CODE_DEFECT,
                f"TRADING_IG_ENDPOINT_BLOCKED:{method.upper()}:{sanitize_path(path)}",
            )
        self.network_request_count += 1


def create_guarded_requests_session() -> Any:
    """Create the requests session only after the dev dependency is available."""

    import requests

    guard = ReferenceRequestGuard()

    class GuardedRequestsSession(requests.Session):
        def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            headers = kwargs.get("headers") or {}
            version = next(
                (value for name, value in headers.items() if str(name).upper() == "VERSION"),
                None,
            )
            guard.validate(method, url, str(version) if version is not None else None)
            return super().request(method, url, *args, **kwargs)

    session = GuardedRequestsSession()
    session.guard = guard
    return session


def _records(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        nested = value.get("accounts")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, Mapping)]
        return []
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict(orient="records")
        if isinstance(converted, list):
            return [item for item in converted if isinstance(item, Mapping)]
    return []


def _exception_details(exc: Exception) -> tuple[int | None, str | None]:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        status = None
    error_code: str | None = None
    json_method = getattr(response, "json", None)
    if callable(json_method):
        try:
            payload = json_method()
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, Mapping) and isinstance(payload.get("errorCode"), str):
            error_code = payload["errorCode"]
    return status, error_code


def run_reference(config: Any) -> dict[str, Any]:
    """Run a minimal REST-only comparison without exposing library responses."""

    document: dict[str, Any] = {
        "diagnostic_timestamp_utc": utc_text(),
        "reference_only": True,
        "production_adapter": False,
        "environment": "DEMO",
        "session_version": 2,
        "session_created": False,
        "configured_account_exists": False,
        "active_account_match": False,
        "market_retrieved": False,
        "http_status": None,
        "ig_errorCode": None,
        "network_request_count": 0,
        "blocked_endpoint_attempt_count": 0,
        "order_endpoint_call_count": 0,
        "final_classification": Classification.INCONCLUSIVE.value,
        "remaining_blocker": None,
    }
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    service: Any = None
    guarded: Any = None
    try:
        try:
            from trading_ig.rest import IGService
        except ImportError:
            document["remaining_blocker"] = "TRADING_IG_DEVELOPMENT_DEPENDENCY_NOT_INSTALLED"
            return document
        guarded = create_guarded_requests_session()
        service = IGService(
            config.identifier,
            config.password,
            config.api_key,
            acc_type="DEMO",
            acc_number=config.account_id,
            session=guarded,
            return_dataframe=False,
        )
        session = service.create_session(version="2")
        document["session_created"] = True
        accounts = _records(service.fetch_accounts())
        document["configured_account_exists"] = any(
            item.get("accountId") == config.account_id for item in accounts
        )
        active = None
        if isinstance(session, Mapping):
            active = session.get("currentAccountId") or session.get("accountId")
        document["active_account_match"] = active == config.account_id
        if not document["configured_account_exists"] or not document["active_account_match"]:
            document["final_classification"] = Classification.CONFIGURATION_DEFECT.value
            document["remaining_blocker"] = "TRADING_IG_ACCOUNT_MISMATCH"
            return document
        service.fetch_market_by_epic(config.epic)
        document["market_retrieved"] = True
        document["final_classification"] = Classification.PASS.value
        return document
    except DiagnosticError as exc:
        document["final_classification"] = exc.classification.value
        document["remaining_blocker"] = exc.reason
        return document
    except Exception as exc:
        status, error_code = _exception_details(exc)
        document["http_status"] = status
        document["ig_errorCode"] = error_code
        document["final_classification"] = (
            classify_http_failure(status, error_code).value
            if status is not None
            else Classification.INCONCLUSIVE.value
        )
        document["remaining_blocker"] = "TRADING_IG_REFERENCE_FAILED"
        return document
    finally:
        logging.disable(previous_disable)
        if service is not None:
            logout = getattr(service, "logout", None)
            if callable(logout):
                try:
                    logout()
                except Exception:
                    document["logout_result"] = "FAILED_SANITIZED"
                    if document["final_classification"] == Classification.PASS.value:
                        document["final_classification"] = Classification.INCONCLUSIVE.value
                        document["remaining_blocker"] = "TRADING_IG_LOGOUT_FAILED"
        if guarded is not None:
            guard = guarded.guard
            document["network_request_count"] = guard.network_request_count
            document["blocked_endpoint_attempt_count"] = guard.blocked_endpoint_attempt_count
            document["order_endpoint_call_count"] = guard.order_endpoint_call_count
            guarded.close()


def reference_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Development-only trading-ig REST comparison",
    )
    parser.add_argument("--environment", required=True, choices=("demo",))
    parser.add_argument("--session-version", required=True, type=int, choices=(2,))
    parser.add_argument("--epic", default=DEFAULT_EPIC)
    parser.add_argument("--output", type=Path, default=DEFAULT_REFERENCE_OUTPUT)
    parser.add_argument("--dotenv", type=Path, default=Path(".env"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = reference_parser().parse_args(argv)
    try:
        config = load_config(args)
    except DiagnosticError as exc:
        print(f"classification={exc.classification.value}")
        return 1
    document = run_reference(config)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if any(secret in serialized for secret in config.secret_values()):
        print("classification=CODE_DEFECT")
        print("report_write=BLOCKED_BY_SECRET_SCAN")
        return 2
    output.write_text(serialized, encoding="utf-8")
    print(f"classification={document['final_classification']}")
    print(f"json_report={output}")
    return 0 if document["final_classification"] == Classification.PASS.value else 1


if __name__ == "__main__":
    raise SystemExit(main())
