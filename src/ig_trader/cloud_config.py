"""Fail-closed configuration for the cloud health/runtime boundary."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

NO_EXECUTION = "NO_EXECUTION"

_COMMIT_PATTERN = re.compile(r"(?:unknown|[0-9a-f]{7,64})\Z")
_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
_CREDENTIAL_NAMES = {
    "CST",
    "IG_ACCOUNT_ID",
    "IG_API_KEY",
    "IG_IDENTIFIER",
    "IG_PASSWORD",
    "X_SECURITY_TOKEN",
    "X_SECURITY_TOKEN_VALUE",
}


class UnsafeCloudConfiguration(RuntimeError):
    """Raised before startup when cloud execution authority is ambiguous or unsafe."""


@dataclass(frozen=True)
class CloudConfig:
    """Non-secret runtime configuration exposed through health metadata."""

    host: str
    port: int
    execution_mode: str
    commit_sha: str
    version: str
    image_revision: str
    log_level: str
    shutdown_grace_seconds: float

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> CloudConfig:
        values = os.environ if environment is None else environment
        execution_mode = values.get("EXECUTION_MODE", NO_EXECUTION).strip()
        if execution_mode != NO_EXECUTION:
            raise UnsafeCloudConfiguration("only NO_EXECUTION is accepted")

        paper_trading = values.get("PAPER_TRADING")
        if paper_trading is not None and paper_trading.strip().casefold() in {
            "0",
            "false",
            "no",
            "off",
        }:
            raise UnsafeCloudConfiguration("PAPER_TRADING cannot be disabled")

        if values.get("IG_DEMO", "").strip().casefold() in {"0", "false", "no", "off"}:
            raise UnsafeCloudConfiguration("live IG configuration is prohibited")

        if any(values.get(name, "").strip() for name in _CREDENTIAL_NAMES):
            raise UnsafeCloudConfiguration("broker credentials are not accepted")

        host = values.get("APP_HOST", "0.0.0.0").strip()
        if host not in {"0.0.0.0", "127.0.0.1", "::"}:
            raise UnsafeCloudConfiguration("APP_HOST must be a local bind address")

        port = _integer(values.get("APP_PORT", "8080"), "APP_PORT")
        if not 1 <= port <= 65535:
            raise UnsafeCloudConfiguration("APP_PORT is outside the valid range")

        commit_sha = values.get("APP_COMMIT_SHA", "unknown").strip().casefold()
        if not _COMMIT_PATTERN.fullmatch(commit_sha):
            raise UnsafeCloudConfiguration("APP_COMMIT_SHA is not a commit identity")

        version = values.get("APP_VERSION", "0.1.0").strip()
        if not version or len(version) > 64:
            raise UnsafeCloudConfiguration("APP_VERSION is invalid")

        image_revision = values.get("CONTAINER_APP_REVISION", "local").strip()
        if not image_revision or len(image_revision) > 128:
            raise UnsafeCloudConfiguration("CONTAINER_APP_REVISION is invalid")

        log_level = values.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in _LOG_LEVELS:
            raise UnsafeCloudConfiguration("LOG_LEVEL is invalid")

        shutdown_grace_seconds = _number(
            values.get("SHUTDOWN_GRACE_SECONDS", "10"),
            "SHUTDOWN_GRACE_SECONDS",
        )
        if not 1 <= shutdown_grace_seconds <= 30:
            raise UnsafeCloudConfiguration("SHUTDOWN_GRACE_SECONDS is outside 1..30")

        return cls(
            host=host,
            port=port,
            execution_mode=execution_mode,
            commit_sha=commit_sha,
            version=version,
            image_revision=image_revision,
            log_level=log_level,
            shutdown_grace_seconds=shutdown_grace_seconds,
        )


def _integer(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise UnsafeCloudConfiguration(f"{name} must be an integer") from error


def _number(value: str, name: str) -> float:
    try:
        return float(value)
    except ValueError as error:
        raise UnsafeCloudConfiguration(f"{name} must be numeric") from error
