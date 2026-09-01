"""Read-only presentation adapter for the isolated Shadow Tournament store.

This module intentionally reads only the frozen configuration and the separate
Shadow Tournament SQLite document.  It never creates the database, an epoch,
a broker client, or a Demo worker.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "shadow01_strategy_config.json"
DEFAULT_DATABASE_PATH = ROOT / "runtime" / "shadow_tournament.sqlite3"
DEFAULT_REGISTRY_PATH = ROOT / "artifacts" / "dq03" / "instrument_registry.json"


@dataclass(frozen=True)
class ShadowTournamentDashboard:
    """Sanitized, non-authoritative data suitable for the local dashboard."""

    available: bool
    reason: str
    tournament_version: str
    config_fingerprint: str
    execution_authority: str
    epoch_utc: str | None
    epoch_created: bool
    market_matrix: tuple[dict[str, object], ...]
    provider_health: tuple[dict[str, object], ...]
    epoch_readiness: tuple[dict[str, object], ...]
    market_snapshots: tuple[dict[str, object], ...]
    engine_insights: tuple[dict[str, object], ...]
    latest_decisions: tuple[dict[str, object], ...]
    resolved_outcomes: tuple[dict[str, object], ...]
    leaderboard: tuple[dict[str, object], ...]
    factor_audit: tuple[dict[str, object], ...]


def load_shadow_tournament_dashboard(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    database_path: Path = DEFAULT_DATABASE_PATH,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> ShadowTournamentDashboard:
    """Read the Shadow Tournament dashboard document without mutating local state.

    The core package is imported only for this explicit local read.  This lets
    the separately packaged dashboard remain safely unavailable when the
    isolated Shadow Tournament package, configuration, or database is absent.
    """

    try:
        from src.ig_trader.shadow01.config import load_config
        from src.ig_trader.shadow01.registry import load_verified_dq03_registry
        from src.ig_trader.shadow01.storage import ShadowTournamentStore

        config = load_config(config_path)
    except Exception:
        return _unavailable(reason="SHADOW01_CONFIG_UNAVAILABLE")

    try:
        registry_document = load_verified_dq03_registry(config, registry_path).document()
        market_matrix = _market_matrix(registry_document, config.universe)
    except Exception:
        market_matrix = _fallback_market_matrix(config.universe, "DQ03_REGISTRY_UNAVAILABLE")

    try:
        document = ShadowTournamentStore(database_path).dashboard_document(config)
    except Exception:
        return _unavailable(
            reason="SHADOW01_STORAGE_UNREADABLE",
            tournament_version=config.version,
            config_fingerprint=config.fingerprint,
            market_matrix=market_matrix,
        )
    return _coerce_document(
        document,
        config_version=config.version,
        fingerprint=config.fingerprint,
        market_matrix=market_matrix,
    )


def _coerce_document(
    value: object,
    *,
    config_version: str,
    fingerprint: str,
    market_matrix: tuple[dict[str, object], ...],
) -> ShadowTournamentDashboard:
    if not isinstance(value, Mapping):
        return _unavailable(
            reason="SHADOW01_DASHBOARD_DOCUMENT_INVALID",
            tournament_version=config_version,
            config_fingerprint=fingerprint,
            market_matrix=market_matrix,
        )
    available = value.get("available") is True
    authority = _text(value.get("execution_authority"), "UNKNOWN")
    if authority != "OFF":
        authority = "UNKNOWN"
    epoch_created = value.get("epoch_created") is True
    epoch_utc = _optional_text(value.get("epoch_utc")) if epoch_created else None
    return ShadowTournamentDashboard(
        available=available,
        reason=_code(
            value.get("reason"),
            "SHADOW01_DASHBOARD_READY" if available else "UNKNOWN",
        ),
        tournament_version=_text(value.get("tournament_version"), config_version),
        config_fingerprint=_text(value.get("config_fingerprint"), fingerprint),
        execution_authority=authority,
        epoch_utc=epoch_utc,
        epoch_created=epoch_created,
        market_matrix=market_matrix,
        provider_health=_rows(value.get("provider_health")),
        epoch_readiness=_rows(value.get("epoch_readiness")),
        market_snapshots=_rows(value.get("market_snapshots")),
        engine_insights=_rows(value.get("engine_insights")),
        latest_decisions=_rows(value.get("latest_decisions")),
        resolved_outcomes=_rows(value.get("resolved_outcomes")),
        leaderboard=_rows(value.get("leaderboard")),
        factor_audit=_rows(value.get("factor_audit")),
    )


def _unavailable(
    *,
    reason: str,
    tournament_version: str = "SHADOW01-V1",
    config_fingerprint: str = "NOT AVAILABLE",
    market_matrix: tuple[dict[str, object], ...] = (),
) -> ShadowTournamentDashboard:
    return ShadowTournamentDashboard(
        available=False,
        reason=reason,
        tournament_version=tournament_version,
        config_fingerprint=config_fingerprint,
        execution_authority="OFF",
        epoch_utc=None,
        epoch_created=False,
        market_matrix=market_matrix,
        provider_health=(),
        epoch_readiness=(),
        market_snapshots=(),
        engine_insights=(),
        latest_decisions=(),
        resolved_outcomes=(),
        leaderboard=(),
        factor_audit=(),
    )


def _rows(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        return ()
    rows: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        rows.append(
            {str(key): item_value for key, item_value in item.items() if isinstance(key, str)}
        )
    return tuple(rows)


def _market_matrix(
    value: object,
    universe: tuple[dict[str, str], ...],
) -> tuple[dict[str, object], ...]:
    if not isinstance(value, Mapping):
        return _fallback_market_matrix(universe, "DQ03_REGISTRY_INVALID")
    rows = value.get("markets")
    if not isinstance(rows, list) or len(rows) != len(universe):
        return _fallback_market_matrix(universe, "DQ03_REGISTRY_INVALID")
    indexed = {
        row.get("symbol"): row
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("symbol"), str)
    }
    result: list[dict[str, object]] = []
    for configured in universe:
        symbol = configured["symbol"]
        row = indexed.get(symbol)
        if row is None:
            result.append(
                {
                    "symbol": symbol,
                    "asset_class": configured["asset_class"],
                    "epic": None,
                    "state": "MARKET_DATA_UNAVAILABLE",
                    "reason": "DQ03_REGISTRY_ENTRY_MISSING_OR_DUPLICATE",
                }
            )
            continue
        epic = row.get("epic")
        state = _text(row.get("state"), "MARKET_DATA_UNAVAILABLE")
        result.append(
            {
                "symbol": symbol,
                "asset_class": configured["asset_class"],
                "epic": (
                    epic.strip()
                    if isinstance(epic, str) and epic.strip() and state == "AVAILABLE"
                    else None
                ),
                "state": state if state == "AVAILABLE" else "MARKET_DATA_UNAVAILABLE",
                "reason": _code(row.get("reason"), "NOT AVAILABLE"),
            }
        )
    return tuple(result)


def _fallback_market_matrix(
    universe: tuple[dict[str, str], ...],
    reason: str,
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "symbol": item["symbol"],
            "asset_class": item["asset_class"],
            "epic": None,
            "state": "MARKET_DATA_UNAVAILABLE",
            "reason": reason,
        }
        for item in universe
    )


def _text(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    compact = value.strip()
    return compact[:160] if compact else fallback


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    compact = value.strip()
    return compact[:160] if compact else None


def _code(value: object, fallback: str) -> str:
    candidate = _text(value, fallback)
    valid = all(
        character.isupper() or character.isdigit() or character == "_" for character in candidate
    )
    return candidate if valid else fallback
