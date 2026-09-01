"""Frozen, fingerprinted configuration for the Shadow Tournament.

The configuration contains observation rules only.  It never grants broker or
execution authority, and a persisted tournament version refuses a changed
fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = ROOT / "shadow01_strategy_config.json"
EXPECTED_SYMBOLS = (
    "EURUSD",
    "GBPUSD",
    "EURGBP",
    "USDJPY",
    "EURJPY",
    "GBPJPY",
    "AUDUSD",
    "NZDUSD",
    "USDCAD",
    "USDCHF",
    "EURCHF",
    "EURAUD",
    "GBPAUD",
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "XAUUSD",
    "XAGUSD",
    "US500",
    "USTECH100",
)
T1_FORMULA = (
    "normalized_n = return_n / max(population_realized_volatility_n, epsilon); "
    "direction is LONG only when normalized_20 and normalized_60 are both positive, "
    "SHORT only when both are negative, otherwise FLAT; "
    "strength = min(trend_strength_cap, (abs(normalized_20) + abs(normalized_60)) / 2)."
)
M1_PERCENTILE_FORMULA = (
    "percentile = (count(prior_values < current_value) + "
    "0.5 * count(prior_values == current_value)) / count(prior_values); "
    "prior_values are the latest valid normalized 5-session returns and exclude current_value."
)


class ShadowConfigError(ValueError):
    """Raised when a purported frozen configuration is incomplete or unsafe."""


@dataclass(frozen=True, init=False)
class ShadowTournamentConfig:
    """Validated immutable configuration and its canonical SHA-256 identity.

    The public ``payload`` accessor intentionally returns a fresh JSON copy.
    A caller therefore cannot mutate nested configuration values after the
    fingerprint has been calculated.
    """

    _canonical_payload_json: str
    fingerprint: str

    def __init__(self, payload: Mapping[str, object], fingerprint: str) -> None:
        canonical_payload = _canonical_payload(dict(payload))
        _validate(canonical_payload)
        canonical_json = json.dumps(
            canonical_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        object.__setattr__(self, "_canonical_payload_json", canonical_json)
        object.__setattr__(self, "fingerprint", str(fingerprint))

    @classmethod
    def verified(cls, payload: Mapping[str, object]) -> ShadowTournamentConfig:
        """Create a config whose fingerprint is derived from its frozen bytes."""

        canonical_payload = _canonical_payload(dict(payload))
        canonical_json = json.dumps(
            canonical_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return cls(canonical_payload, hashlib.sha256(canonical_json.encode("utf-8")).hexdigest())

    @property
    def payload(self) -> dict[str, object]:
        """Return a defensive copy of the frozen canonical payload."""

        value: Any = json.loads(self._canonical_payload_json)
        assert isinstance(value, dict)
        return value

    @property
    def canonical_fingerprint(self) -> str:
        """Fingerprint recomputed from the frozen canonical JSON bytes."""

        return hashlib.sha256(self._canonical_payload_json.encode("utf-8")).hexdigest()

    @property
    def fingerprint_is_valid(self) -> bool:
        """Whether the claimed fingerprint matches the frozen payload."""

        return self.fingerprint == self.canonical_fingerprint

    @property
    def version(self) -> str:
        return str(self.payload["tournament_version"])

    @property
    def universe(self) -> tuple[dict[str, str], ...]:
        values = self.payload["universe"]
        assert isinstance(values, list)
        return tuple(
            {"symbol": str(item["symbol"]), "asset_class": str(item["asset_class"])}
            for item in values
            if isinstance(item, dict)
        )

    @property
    def decision_clock(self) -> dict[str, object]:
        value = self.payload["decision_clock"]
        assert isinstance(value, dict)
        return dict(value)

    def document(self) -> dict[str, object]:
        return {**self.payload, "config_fingerprint": self.fingerprint}


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> ShadowTournamentConfig:
    """Load one deliberately small, exact configuration file without defaults."""

    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ShadowConfigError("SHADOW01_CONFIG_UNAVAILABLE") from error
    if not isinstance(value, dict):
        raise ShadowConfigError("SHADOW01_CONFIG_INVALID")
    payload = _canonical_payload(value)
    _validate(payload)
    return ShadowTournamentConfig.verified(payload)


def write_config_artifacts(config: ShadowTournamentConfig, directory: Path) -> dict[str, Path]:
    """Write review artifacts only when an explicit preparation command requests it."""

    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / "shadow01_config.json"
    fingerprint_path = directory / "shadow01_config_fingerprint.json"
    config_path.write_text(
        json.dumps(config.payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    fingerprint_path.write_text(
        json.dumps(
            {
                "tournament_version": config.version,
                "config_fingerprint": config.fingerprint,
                "execution_authority": "OFF",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"config": config_path, "fingerprint": fingerprint_path}


def _canonical_payload(value: dict[str, object]) -> dict[str, object]:
    """Round-trip JSON so fingerprinting cannot depend on input object aliases."""

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    decoded: Any = json.loads(encoded)
    assert isinstance(decoded, dict)
    return decoded


def _validate(payload: dict[str, object]) -> None:
    if payload.get("schema_version") != "shadow01-strategy-config/1.0":
        raise ShadowConfigError("SHADOW01_CONFIG_SCHEMA_INVALID")
    version = payload.get("tournament_version")
    if not isinstance(version, str) or re.fullmatch(r"SHADOW01-V[1-9][0-9]*", version) is None:
        raise ShadowConfigError("SHADOW01_CONFIG_VERSION_INVALID")
    if payload.get("execution_authority") != "OFF":
        raise ShadowConfigError("SHADOW01_EXECUTION_AUTHORITY_MUST_REMAIN_OFF")
    universe = payload.get("universe")
    if not isinstance(universe, list) or len(universe) != len(EXPECTED_SYMBOLS):
        raise ShadowConfigError("SHADOW01_UNIVERSE_MUST_CONTAIN_20_MARKETS")
    symbols = tuple(item.get("symbol") for item in universe if isinstance(item, dict))
    if symbols != EXPECTED_SYMBOLS:
        raise ShadowConfigError("SHADOW01_UNIVERSE_DOES_NOT_MATCH_FROZEN_DQ03_SCOPE")
    classes = tuple(item.get("asset_class") for item in universe if isinstance(item, dict))
    if any(value not in {"FX", "METAL", "INDEX"} for value in classes):
        raise ShadowConfigError("SHADOW01_ASSET_CLASS_INVALID")
    clock = payload.get("decision_clock")
    if not isinstance(clock, dict) or clock.get("timezone") != "America/New_York":
        raise ShadowConfigError("SHADOW01_CLOCK_TIMEZONE_INVALID")
    if clock.get("local_time") != "17:10" or clock.get("frequency") != "COMPLETED_MARKET_DAY":
        raise ShadowConfigError("SHADOW01_CLOCK_INVALID")
    if clock.get("asset_class_overrides") != {}:
        raise ShadowConfigError("SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED")
    history = payload.get("history")
    if not isinstance(history, dict) or history.get("resolution") != "DAY":
        raise ShadowConfigError("SHADOW01_HISTORY_CONFIG_INVALID")
    if (
        history.get("target_completed_observations") != 300
        or history.get("minimum_completed_observations") != 61
    ):
        raise ShadowConfigError("SHADOW01_HISTORY_DEPTH_INVALID")
    technical = payload.get("technical")
    reversion = payload.get("reversion")
    if not isinstance(technical, dict) or not isinstance(reversion, dict):
        raise ShadowConfigError("SHADOW01_ENGINE_CONFIG_INVALID")
    if (
        technical.get("return_windows") != [1, 5, 20, 60]
        or technical.get("volatility_window") != 20
        or technical.get("atr_window") != 20
        or not _positive_number(technical.get("epsilon"))
        or not _positive_number(technical.get("trend_strength_cap"))
    ):
        raise ShadowConfigError("SHADOW01_TECHNICAL_CONFIG_INVALID")
    if technical.get("trend_formula") != T1_FORMULA:
        raise ShadowConfigError("SHADOW01_T1_FORMULA_NOT_FROZEN")
    lower_percentile = reversion.get("lower_percentile")
    upper_percentile = reversion.get("upper_percentile")
    if (
        reversion.get("return_window") != 5
        or reversion.get("percentile_lookback") != 252
        or not _fraction(lower_percentile)
        or not _fraction(upper_percentile)
        or float(lower_percentile) >= float(upper_percentile)
    ):
        raise ShadowConfigError("SHADOW01_REVERSION_CONFIG_INVALID")
    if reversion.get("percentile_formula") != M1_PERCENTILE_FORMULA:
        raise ShadowConfigError("SHADOW01_M1_FORMULA_NOT_FROZEN")
    quality = payload.get("quality")
    cost = payload.get("cost")
    context = payload.get("context")
    policies = payload.get("policies")
    if not isinstance(quality, dict) or not isinstance(cost, dict):
        raise ShadowConfigError("SHADOW01_CONTEXT_CONFIG_INVALID")
    if (
        not isinstance(quality.get("maximum_price_age_seconds"), int)
        or isinstance(quality.get("maximum_price_age_seconds"), bool)
        or int(quality["maximum_price_age_seconds"]) <= 0
        or (
            quality.get("minimum_completed_observations")
            != history.get("minimum_completed_observations")
        )
        or not _positive_number(cost.get("high_spread_to_atr_fraction"))
        or not isinstance(context, dict)
        or not _fraction(context.get("material_opposition_score"))
        or not isinstance(policies, dict)
        or not _nonnegative_number(policies.get("trend_minimum_strength"))
    ):
        raise ShadowConfigError("SHADOW01_CONTEXT_CONFIG_INVALID")


def _positive_number(value: object) -> bool:
    return _finite_number(value) and float(value) > 0.0


def _nonnegative_number(value: object) -> bool:
    return _finite_number(value) and float(value) >= 0.0


def _fraction(value: object) -> bool:
    return _finite_number(value) and 0.0 < float(value) <= 1.0


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
