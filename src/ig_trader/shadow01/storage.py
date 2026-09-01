"""Durable local, append-only evidence storage for SHADOW01.

This store is intentionally separate from ``trading.db`` and all execution
state.  It contains observations and later labels only.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.ig_trader.shadow01.clock import ShadowClockError, require_decision_anchor
from src.ig_trader.shadow01.config import ShadowTournamentConfig
from src.ig_trader.shadow01.models import (
    ContextState,
    CostState,
    Direction,
    FundamentalState,
    MarketSnapshot,
    OutcomeLabel,
    PolicyId,
    QualityState,
    ShadowDecision,
    document,
    expected_factor_tags,
    is_finite_positive,
    require_utc,
)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASE_PATH = ROOT / "runtime" / "shadow_tournament.sqlite3"
_EXPECTED_ASSET_CLASSES = frozenset({"FX", "METAL", "INDEX"})
_OUTCOME_HORIZONS = (1, 3, 5, 10, 20)
_ENGINE_IDS = frozenset({"TECHNICAL_STATE", "T1", "M1", "X1", "F1", "Q1", "C1"})
_POLICY_ENGINES = {
    PolicyId.P0_TECHNICAL_TREND_ONLY: "T1",
    PolicyId.P1_TECHNICAL_REVERSION_ONLY: "M1",
    PolicyId.P2_TREND_PLUS_CROSS_ASSET: "T1",
    PolicyId.P3_CONSERVATIVE_CONTEXT: "T1",
}
_MARKET_OBSERVATION_ABSENT = "ABSENT"
_MARKET_OBSERVATION_COMPLETE = "COMPLETE"
_MARKET_OBSERVATION_PARTIAL = "PARTIAL"


class ShadowStoreError(RuntimeError):
    """Append-only or epoch governance cannot be proven."""


@dataclass(frozen=True)
class PendingOutcomeBasis:
    """Immutable entry evidence for a directional decision awaiting later labels.

    Missing horizons are deliberately absent from ``outcome_labels`` until a
    later completed-session read can resolve them.  Writing a provisional
    ``BLOCKED`` row would be irreversible because the label table is
    append-only.
    """

    decision: ShadowDecision
    entry_price: float
    atr20_over_price: float | None
    missing_horizons: tuple[int, ...]


class ShadowTournamentStore:
    """SQLite store that refuses changed config, rewritten decisions, and backfill."""

    def __init__(self, path: Path = DEFAULT_DATABASE_PATH) -> None:
        self.path = path
        self._schema_initialized = False

    def initialize(self) -> None:
        """Create the isolated local schema; no epoch is created by this method."""

        if self._schema_initialized and self.path.is_file():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(_SCHEMA)
            _migrate_schema(connection)
            connection.executescript(_TRIGGERS)
        self._schema_initialized = True

    def register_version(self, config: ShadowTournamentConfig, *, now: datetime) -> None:
        """Persist one version/fingerprint pair, rejecting silent rule changes."""

        config_json = _validated_config_json(config)
        timestamp = _timestamp(now)
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT config_fingerprint, config_json
                FROM tournament_runs
                WHERE tournament_version = ?
                """,
                (config.version,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO tournament_runs (
                        tournament_version, config_fingerprint, epoch_utc, created_at_utc,
                        execution_authority, config_json
                    ) VALUES (?, ?, NULL, ?, 'OFF', ?)
                    """,
                    (config.version, config.fingerprint, timestamp, config_json),
                )
            elif row[0] != config.fingerprint or row[1] != config_json:
                raise ShadowStoreError("SHADOW01_VERSION_FINGERPRINT_CONFLICT")

    def create_epoch(
        self,
        config: ShadowTournamentConfig,
        *,
        epoch_utc: datetime,
        authorization_phrase: str,
    ) -> None:
        """Create a permanent epoch only after the explicit human-gate phrase.

        No worker path calls this method.  The caller must invoke it separately
        after engineering review and a successful real read-only snapshot.
        """

        _validated_config_json(config)
        epoch = _decision_anchor(config, epoch_utc)
        expected_phrase = f"START {config.version} EPOCH"
        if authorization_phrase != expected_phrase:
            raise ShadowStoreError("SHADOW01_EPOCH_HUMAN_AUTHORIZATION_REQUIRED")
        self.register_version(config, now=epoch)
        value = _timestamp(epoch)
        with self._connection() as connection:
            if not _has_epoch_readiness(
                connection,
                config,
                epoch_utc=value,
            ):
                raise ShadowStoreError("SHADOW01_EPOCH_READINESS_REQUIRED")
            result = connection.execute(
                """
                UPDATE tournament_runs
                SET epoch_utc = ?, epoch_authorization = ?
                WHERE tournament_version = ?
                  AND config_fingerprint = ?
                  AND epoch_utc IS NULL
                  AND execution_authority = 'OFF'
                """,
                (value, authorization_phrase, config.version, config.fingerprint),
            )
        if result.rowcount != 1:
            raise ShadowStoreError("SHADOW01_EPOCH_ALREADY_EXISTS_OR_VERSION_CONFLICT")

    def record_epoch_readiness(
        self,
        config: ShadowTournamentConfig,
        *,
        snapshot: MarketSnapshot,
        provider_probe_observed_at: datetime,
    ) -> None:
        """Append proof for a later human-gated epoch; this never creates one.

        The caller must supply a real, completed-bar snapshot obtained through
        the read-only adapter and reference an already persisted successful
        universal provider/clock probe.  The snapshot is retained as separate
        pre-epoch evidence, not as a prospective tournament observation.
        """

        config_json = _validated_config_json(config)
        snapshot_at = _decision_anchor(config, snapshot.decision_timestamp_utc)
        provider_probe_at = _decision_anchor(config, provider_probe_observed_at)
        _require_configured_snapshot(config, snapshot)
        self.register_version(config, now=snapshot_at)
        with self._connection() as connection:
            if _epoch_from_connection(connection, config) is not None:
                raise ShadowStoreError("SHADOW01_EPOCH_ALREADY_EXISTS")
            if not _has_verified_universal_probe(connection, provider_probe_at):
                raise ShadowStoreError("SHADOW01_EPOCH_PROVIDER_PROBE_REQUIRED")
            connection.execute(
                """
                INSERT INTO epoch_readiness (
                    tournament_version, config_fingerprint, config_json,
                    provider_probe_at_utc, snapshot_at_utc, instrument, epic,
                    input_data_fingerprint, snapshot_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    config.version,
                    config.fingerprint,
                    config_json,
                    _timestamp(provider_probe_at),
                    _timestamp(snapshot_at),
                    snapshot.instrument,
                    snapshot.epic,
                    snapshot.input_data_fingerprint,
                    _json(document(snapshot)),
                    _timestamp(datetime.now(UTC)),
                ),
            )

    def epoch(self, config: ShadowTournamentConfig) -> datetime | None:
        """Read epoch state without creating a database or changing a schema."""

        _validated_config_json(config)
        if not self.path.is_file():
            return None
        with self._connection(read_only=True) as connection:
            return _epoch_from_connection(connection, config)

    def append_provider_health(
        self,
        *,
        observed_at: datetime,
        provider: str,
        status: str,
        detail: str | None,
        data: dict[str, object] | None = None,
    ) -> None:
        """Record a provider/pre-epoch probe without producing a decision."""

        if not provider.strip() or not status.strip():
            raise ShadowStoreError("SHADOW01_PROVIDER_HEALTH_INVALID")
        self.initialize()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO provider_health (
                    observed_at_utc, provider, status, detail, data_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _timestamp(observed_at),
                    provider,
                    status,
                    detail,
                    _json(data or {}),
                ),
            )

    def append_snapshot(
        self,
        config: ShadowTournamentConfig,
        *,
        decision_timestamp_utc: datetime,
        instrument: str,
        epic: str,
        snapshot_data: dict[str, object],
        input_data_fingerprint: str,
    ) -> None:
        anchored_timestamp = self._require_epoch(config, decision_timestamp_utc)
        _require_configured_instrument(config, instrument)
        if not epic.strip():
            raise ShadowStoreError("SHADOW01_SNAPSHOT_EPIC_UNPROVEN")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO market_snapshots (
                    tournament_version, decision_timestamp_utc, instrument, epic,
                    input_data_fingerprint, snapshot_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    config.version,
                    _timestamp(anchored_timestamp),
                    instrument,
                    epic,
                    input_data_fingerprint,
                    _json(snapshot_data),
                    _timestamp(datetime.now(UTC)),
                ),
            )

    def append_engine_insight(
        self,
        config: ShadowTournamentConfig,
        *,
        decision_timestamp_utc: datetime,
        instrument: str,
        engine_id: str,
        insight: dict[str, object],
    ) -> None:
        anchored_timestamp = self._require_epoch(config, decision_timestamp_utc)
        _require_configured_instrument(config, instrument)
        if engine_id not in _ENGINE_IDS:
            raise ShadowStoreError("SHADOW01_ENGINE_ID_UNPROVEN")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO engine_insights (
                    tournament_version, decision_timestamp_utc, instrument, engine_id,
                    insight_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    config.version,
                    _timestamp(anchored_timestamp),
                    instrument,
                    engine_id,
                    _json(insight),
                    _timestamp(datetime.now(UTC)),
                ),
            )

    def append_decision(self, decision: ShadowDecision) -> None:
        """Append a decision after proving it is not before the immutable epoch."""

        _validate_decision_persistence_contract(decision)
        anchored_timestamp = self._require_epoch_for_decision(decision)
        value = decision.document()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO shadow_decisions (
                    decision_id, tournament_version, config_fingerprint,
                    decision_timestamp_utc, instrument, epic, policy_id, direction,
                    technical_engine, technical_score, cross_asset_state,
                    fundamental_context, quality_state, cost_state, factor_tags_json,
                    reason_codes_json, input_data_fingerprint, created_at_utc, decision_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.tournament_version,
                    decision.config_fingerprint,
                    _timestamp(anchored_timestamp),
                    decision.instrument,
                    decision.epic,
                    decision.policy_id.value,
                    decision.direction.value,
                    decision.technical_engine,
                    decision.technical_score,
                    decision.cross_asset_state.value,
                    decision.fundamental_context.value,
                    decision.quality_state.value,
                    decision.cost_state.value,
                    _json(list(decision.factor_tags)),
                    _json(list(decision.reason_codes)),
                    decision.input_data_fingerprint,
                    _timestamp(decision.created_at),
                    _json(value),
                ),
            )

    def market_observation_state(
        self,
        config: ShadowTournamentConfig,
        *,
        decision_timestamp_utc: datetime,
        instrument: str,
    ) -> str:
        """Return whether one market/anchor has a coherent immutable bundle.

        This is deliberately a local, read-only preflight.  A restart can use
        it before touching the provider, so a replay of an already committed
        anchor never turns into a unique-key failure after a fresh metadata
        request.  A mixture of rows is not treated as reusable evidence: it is
        reported as ``PARTIAL`` and the caller must fail closed.
        """

        _validated_config_json(config)
        anchored_timestamp = _decision_anchor(config, decision_timestamp_utc)
        _require_configured_instrument(config, instrument)
        if not self.path.is_file():
            return _MARKET_OBSERVATION_ABSENT
        with self._connection(read_only=True) as connection:
            return _market_observation_state(
                connection,
                config,
                anchored_timestamp=anchored_timestamp,
                instrument=instrument,
            )

    def append_market_observation(
        self,
        config: ShadowTournamentConfig,
        *,
        decision_timestamp_utc: datetime,
        instrument: str,
        epic: str,
        snapshot_data: dict[str, object],
        input_data_fingerprint: str,
        engine_insights: dict[str, dict[str, object]],
        decisions: tuple[ShadowDecision, ...],
    ) -> bool:
        """Atomically append one complete market observation bundle.

        Snapshot, engine, and policy rows are one causal unit.  The former
        per-row commits could strand an append-only snapshot when a process
        stopped midway through the remaining rows; SQLite now commits all of
        them together or rolls all of them back.  A complete replay returns
        ``False`` without mutation, while any legacy/integrity partial state
        raises a fail-closed error instead of attempting to overwrite it.
        """

        _validated_config_json(config)
        anchored_timestamp = self._require_epoch(config, decision_timestamp_utc)
        _require_configured_instrument(config, instrument)
        if not isinstance(epic, str) or not epic.strip():
            raise ShadowStoreError("SHADOW01_SNAPSHOT_EPIC_UNPROVEN")
        if not isinstance(input_data_fingerprint, str) or not input_data_fingerprint.strip():
            raise ShadowStoreError("SHADOW01_SNAPSHOT_FINGERPRINT_UNPROVEN")
        if not isinstance(snapshot_data, dict):
            raise ShadowStoreError("SHADOW01_MARKET_OBSERVATION_SNAPSHOT_INVALID")
        if set(engine_insights) != _ENGINE_IDS or not all(
            isinstance(value, dict) for value in engine_insights.values()
        ):
            raise ShadowStoreError("SHADOW01_MARKET_OBSERVATION_ENGINE_BUNDLE_INVALID")
        if len(decisions) != len(_POLICY_ENGINES) or {
            decision.policy_id for decision in decisions
        } != set(_POLICY_ENGINES):
            raise ShadowStoreError("SHADOW01_MARKET_OBSERVATION_DECISION_BUNDLE_INVALID")
        for decision in decisions:
            _validate_decision_persistence_contract(decision)
            if (
                decision.tournament_version != config.version
                or decision.config_fingerprint != config.fingerprint
                or decision.decision_timestamp_utc != anchored_timestamp
                or decision.instrument != instrument
                or decision.epic != epic
                or decision.input_data_fingerprint != input_data_fingerprint
            ):
                raise ShadowStoreError("SHADOW01_MARKET_OBSERVATION_DECISION_BUNDLE_INVALID")

        self.initialize()
        with self._connection() as connection:
            # Take the write lock before looking for prior evidence so two
            # local restarts cannot both observe an absent bundle and race into
            # a unique constraint.  The context manager rolls this whole unit
            # back on every exception, including a process interruption before
            # the final commit.
            connection.execute("BEGIN IMMEDIATE")
            epoch = _epoch_from_connection(connection, config)
            if epoch is None:
                raise ShadowStoreError("SHADOW01_EPOCH_NOT_CREATED")
            if anchored_timestamp < epoch:
                raise ShadowStoreError("SHADOW01_NO_RETROSPECTIVE_DECISIONS")
            state = _market_observation_state(
                connection,
                config,
                anchored_timestamp=anchored_timestamp,
                instrument=instrument,
            )
            if state == _MARKET_OBSERVATION_COMPLETE:
                return False
            if state == _MARKET_OBSERVATION_PARTIAL:
                raise ShadowStoreError("SHADOW01_MARKET_OBSERVATION_PARTIAL_EVIDENCE")
            timestamp = _timestamp(anchored_timestamp)
            created_at = _timestamp(datetime.now(UTC))
            connection.execute(
                """
                INSERT INTO market_snapshots (
                    tournament_version, decision_timestamp_utc, instrument, epic,
                    input_data_fingerprint, snapshot_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    config.version,
                    timestamp,
                    instrument,
                    epic,
                    input_data_fingerprint,
                    _json(snapshot_data),
                    created_at,
                ),
            )
            for engine_id in sorted(_ENGINE_IDS):
                connection.execute(
                    """
                    INSERT INTO engine_insights (
                        tournament_version, decision_timestamp_utc, instrument, engine_id,
                        insight_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        config.version,
                        timestamp,
                        instrument,
                        engine_id,
                        _json(engine_insights[engine_id]),
                        created_at,
                    ),
                )
            for decision in decisions:
                value = decision.document()
                connection.execute(
                    """
                    INSERT INTO shadow_decisions (
                        decision_id, tournament_version, config_fingerprint,
                        decision_timestamp_utc, instrument, epic, policy_id, direction,
                        technical_engine, technical_score, cross_asset_state,
                        fundamental_context, quality_state, cost_state, factor_tags_json,
                        reason_codes_json, input_data_fingerprint, created_at_utc, decision_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision.decision_id,
                        decision.tournament_version,
                        decision.config_fingerprint,
                        timestamp,
                        decision.instrument,
                        decision.epic,
                        decision.policy_id.value,
                        decision.direction.value,
                        decision.technical_engine,
                        decision.technical_score,
                        decision.cross_asset_state.value,
                        decision.fundamental_context.value,
                        decision.quality_state.value,
                        decision.cost_state.value,
                        _json(list(decision.factor_tags)),
                        _json(list(decision.reason_codes)),
                        decision.input_data_fingerprint,
                        _timestamp(decision.created_at),
                        _json(value),
                    ),
                )
        return True

    def append_outcome(self, outcome: OutcomeLabel) -> None:
        """Store a later label separately; feature code has no outcome query API."""

        self.initialize()
        with self._connection() as connection:
            decision = connection.execute(
                "SELECT direction FROM shadow_decisions WHERE decision_id = ?",
                (outcome.decision_id,),
            ).fetchone()
            if decision is None:
                raise ShadowStoreError("SHADOW01_OUTCOME_DECISION_UNKNOWN")
            if decision[0] not in {Direction.LONG.value, Direction.SHORT.value}:
                raise ShadowStoreError("SHADOW01_OUTCOME_REQUIRES_DIRECTIONAL_DECISION")
            connection.execute(
                """
                INSERT INTO outcome_labels (
                    decision_id, horizon_sessions, reference_entry_price, future_price,
                    raw_directional_return, atr_normalized_return, cost_adjusted_result,
                    outcome_timestamp_utc, quality, blocked_reason, outcome_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.decision_id,
                    outcome.horizon_sessions,
                    outcome.reference_entry_price,
                    outcome.future_price,
                    outcome.raw_directional_return,
                    outcome.atr_normalized_return,
                    outcome.cost_adjusted_result,
                    _timestamp(outcome.outcome_timestamp_utc)
                    if outcome.outcome_timestamp_utc is not None
                    else None,
                    outcome.quality.value,
                    outcome.blocked_reason,
                    _json(document(outcome)),
                    _timestamp(datetime.now(UTC)),
                ),
            )

    def pending_outcome_bases(
        self,
        config: ShadowTournamentConfig,
        *,
        instrument: str,
    ) -> tuple[PendingOutcomeBasis, ...]:
        """Read immutable, unlabelled directional decisions for one market.

        This is intentionally a read-only query.  It joins a decision to the
        causal snapshot and the recorded technical state at the same frozen
        timestamp, then returns only outcome horizons not already labelled.
        It never inserts a provisional outcome for a horizon that has not yet
        completed.
        """

        _validated_config_json(config)
        _require_configured_instrument(config, instrument)
        if not self.path.is_file():
            return ()
        with self._connection(read_only=True) as connection:
            if _epoch_from_connection(connection, config) is None:
                return ()
            rows = connection.execute(
                """
                SELECT d.decision_json, s.snapshot_json, technical.insight_json
                FROM shadow_decisions d
                JOIN market_snapshots s
                  ON s.tournament_version = d.tournament_version
                 AND s.decision_timestamp_utc = d.decision_timestamp_utc
                 AND s.instrument = d.instrument
                LEFT JOIN engine_insights technical
                  ON technical.tournament_version = d.tournament_version
                 AND technical.decision_timestamp_utc = d.decision_timestamp_utc
                 AND technical.instrument = d.instrument
                 AND technical.engine_id = 'TECHNICAL_STATE'
                WHERE d.tournament_version = ?
                  AND d.config_fingerprint = ?
                  AND d.instrument = ?
                  AND d.direction IN ('LONG', 'SHORT')
                ORDER BY d.decision_timestamp_utc, d.policy_id
                """,
                (config.version, config.fingerprint, instrument),
            ).fetchall()
            pending: list[PendingOutcomeBasis] = []
            for row in rows:
                decision = _decision_from_json(str(row["decision_json"]))
                if (
                    decision.tournament_version != config.version
                    or decision.config_fingerprint != config.fingerprint
                    or decision.instrument != instrument
                ):
                    raise ShadowStoreError("SHADOW01_OUTCOME_DECISION_EVIDENCE_INVALID")
                existing = {
                    int(item[0])
                    for item in connection.execute(
                        "SELECT horizon_sessions FROM outcome_labels WHERE decision_id = ?",
                        (decision.decision_id,),
                    ).fetchall()
                    if int(item[0]) in _OUTCOME_HORIZONS
                }
                missing = tuple(horizon for horizon in _OUTCOME_HORIZONS if horizon not in existing)
                if not missing:
                    continue
                entry_price, atr20_over_price = _outcome_basis_from_json(
                    str(row["snapshot_json"]),
                    str(row["insight_json"]) if row["insight_json"] is not None else None,
                    decision,
                )
                pending.append(
                    PendingOutcomeBasis(
                        decision=decision,
                        entry_price=entry_price,
                        atr20_over_price=atr20_over_price,
                        missing_horizons=missing,
                    )
                )
        return tuple(pending)

    def dashboard_document(self, config: ShadowTournamentConfig) -> dict[str, object]:
        """Read a compact, safe view for the local dashboard without mutation."""

        try:
            _validated_config_json(config)
        except ShadowStoreError as error:
            return _empty_dashboard(config, str(error))
        if not self.path.is_file():
            return _empty_dashboard(config, "SHADOW01_STORAGE_NOT_CREATED")
        try:
            with self._connection(read_only=True) as connection:
                epoch = _epoch_from_connection(connection, config)
                provider = _rows(
                    connection.execute(
                        """
                        SELECT observed_at_utc, provider, status, detail
                        FROM provider_health ORDER BY id DESC LIMIT 30
                        """
                    )
                )
                readiness = _rows(
                    connection.execute(
                        """
                        SELECT provider_probe_at_utc, snapshot_at_utc, instrument, epic,
                               input_data_fingerprint
                        FROM epoch_readiness
                        WHERE tournament_version = ? AND config_fingerprint = ?
                        ORDER BY id DESC LIMIT 20
                        """,
                        (config.version, config.fingerprint),
                    )
                )
                markets = _rows(
                    connection.execute(
                        """
                        SELECT decision_timestamp_utc, instrument, epic, snapshot_json
                        FROM market_snapshots WHERE tournament_version = ?
                        ORDER BY decision_timestamp_utc DESC, instrument LIMIT 20
                        """,
                        (config.version,),
                    )
                )
                insights = _rows(
                    connection.execute(
                        """
                        SELECT decision_timestamp_utc, instrument, engine_id, insight_json
                        FROM engine_insights WHERE tournament_version = ?
                        ORDER BY decision_timestamp_utc DESC, instrument, engine_id LIMIT 280
                        """,
                        (config.version,),
                    )
                )
                decisions = _rows(
                    connection.execute(
                        """
                        SELECT decision_timestamp_utc, instrument, policy_id, direction,
                               technical_engine, quality_state, cost_state, factor_tags_json,
                               reason_codes_json
                        FROM shadow_decisions WHERE tournament_version = ?
                        ORDER BY decision_timestamp_utc DESC, instrument, policy_id LIMIT 160
                        """,
                        (config.version,),
                    )
                )
                leaderboard_decisions = _rows(
                    connection.execute(
                        """
                        SELECT decision_id, policy_id, technical_engine, instrument, direction
                        FROM shadow_decisions
                        WHERE tournament_version = ?
                        """,
                        (config.version,),
                    )
                )
                outcomes = _rows(
                    connection.execute(
                        """
                        SELECT d.decision_id, d.policy_id, d.technical_engine, d.instrument,
                               d.direction, o.horizon_sessions, o.raw_directional_return,
                               o.atr_normalized_return, o.quality
                        FROM outcome_labels o
                        JOIN shadow_decisions d ON d.decision_id = o.decision_id
                        WHERE d.tournament_version = ?
                        """,
                        (config.version,),
                    )
                )
                resolved_outcomes = _rows(
                    connection.execute(
                        """
                        SELECT d.decision_timestamp_utc, d.instrument, d.policy_id,
                               d.technical_engine, o.horizon_sessions,
                               o.outcome_timestamp_utc, o.quality,
                               o.reference_entry_price, o.future_price,
                               o.raw_directional_return, o.atr_normalized_return,
                               o.cost_adjusted_result, o.blocked_reason
                        FROM outcome_labels o
                        JOIN shadow_decisions d ON d.decision_id = o.decision_id
                        WHERE d.tournament_version = ?
                        ORDER BY o.created_at_utc DESC, d.instrument, d.policy_id
                        LIMIT 500
                        """,
                        (config.version,),
                    )
                )
        except (sqlite3.Error, ShadowStoreError):
            return _empty_dashboard(config, "SHADOW01_STORAGE_UNREADABLE")
        return {
            "available": True,
            "tournament_version": config.version,
            "config_fingerprint": config.fingerprint,
            "execution_authority": "OFF",
            "epoch_utc": epoch.isoformat() if epoch else None,
            "epoch_created": epoch is not None,
            "provider_health": provider,
            "epoch_readiness": readiness,
            "market_snapshots": [_decode_snapshot(row) for row in markets],
            "engine_insights": [_decode_insight(row) for row in insights],
            "latest_decisions": [_decode_decision(row) for row in decisions],
            "resolved_outcomes": resolved_outcomes,
            "leaderboard": _leaderboard(
                leaderboard_decisions,
                outcomes,
                _asset_classes_by_instrument(config),
            ),
            "factor_audit": _factor_audit(decisions),
        }

    def _require_epoch(self, config: ShadowTournamentConfig, timestamp: datetime) -> datetime:
        _validated_config_json(config)
        value = _decision_anchor(config, timestamp)
        epoch = self.epoch(config)
        if epoch is None:
            raise ShadowStoreError("SHADOW01_EPOCH_NOT_CREATED")
        if value < epoch:
            raise ShadowStoreError("SHADOW01_NO_RETROSPECTIVE_DECISIONS")
        return value

    def _require_epoch_for_decision(self, decision: ShadowDecision) -> datetime:
        self.initialize()
        with self._connection(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT tournament_version, config_fingerprint, config_json, epoch_utc,
                       execution_authority
                FROM tournament_runs WHERE tournament_version = ?
                """,
                (decision.tournament_version,),
            ).fetchone()
        if row is None:
            raise ShadowStoreError("SHADOW01_DECISION_VERSION_UNPROVEN")
        stored_config = _config_from_row(row)
        if (
            stored_config.version != decision.tournament_version
            or stored_config.fingerprint != decision.config_fingerprint
        ):
            raise ShadowStoreError("SHADOW01_DECISION_VERSION_UNPROVEN")
        _require_configured_instrument(stored_config, decision.instrument)
        if decision.technical_engine not in {"T1", "M1"}:
            raise ShadowStoreError("SHADOW01_DECISION_ENGINE_UNPROVEN")
        anchored_timestamp = _decision_anchor(stored_config, decision.decision_timestamp_utc)
        if row[3] is None or anchored_timestamp < _parse_timestamp(row[3]):
            raise ShadowStoreError("SHADOW01_NO_RETROSPECTIVE_DECISIONS")
        return anchored_timestamp

    @contextmanager
    def _connection(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        if read_only:
            connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(self.path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            if not read_only:
                connection.commit()
        except Exception:
            if not read_only:
                connection.rollback()
            raise
        finally:
            connection.close()


def _timestamp(value: datetime) -> str:
    return require_utc(value).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _decision_from_json(raw: str) -> ShadowDecision:
    """Rebuild a persisted decision before it may become an outcome basis."""

    value = _json_object(raw, "SHADOW01_OUTCOME_DECISION_EVIDENCE_INVALID")
    try:
        factor_tags = value["factor_tags"]
        reason_codes = value["reason_codes"]
        if (
            not isinstance(factor_tags, list)
            or not isinstance(reason_codes, list)
            or not all(isinstance(item, str) for item in factor_tags)
            or not all(isinstance(item, str) for item in reason_codes)
        ):
            raise ValueError("collection invalid")
        return ShadowDecision(
            decision_id=str(value["decision_id"]),
            tournament_version=str(value["tournament_version"]),
            config_fingerprint=str(value["config_fingerprint"]),
            decision_timestamp_utc=_parse_timestamp(str(value["decision_timestamp_utc"])),
            instrument=str(value["instrument"]),
            epic=str(value["epic"]),
            policy_id=PolicyId(str(value["policy_id"])),
            direction=Direction(str(value["direction"])),
            technical_engine=str(value["technical_engine"]),
            technical_score=value.get("technical_score"),
            cross_asset_state=ContextState(str(value["cross_asset_state"])),
            fundamental_context=FundamentalState(str(value["fundamental_context"])),
            quality_state=QualityState(str(value["quality_state"])),
            cost_state=CostState(str(value["cost_state"])),
            factor_tags=tuple(factor_tags),
            reason_codes=tuple(reason_codes),
            input_data_fingerprint=str(value["input_data_fingerprint"]),
            created_at=_parse_timestamp(str(value["created_at"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ShadowStoreError("SHADOW01_OUTCOME_DECISION_EVIDENCE_INVALID") from error


def _outcome_basis_from_json(
    snapshot_raw: str,
    technical_raw: str | None,
    decision: ShadowDecision,
) -> tuple[float, float | None]:
    """Extract only the entry close and saved ATR fact from causal evidence."""

    snapshot = _json_object(snapshot_raw, "SHADOW01_OUTCOME_SNAPSHOT_EVIDENCE_INVALID")
    try:
        snapshot_timestamp = _parse_timestamp(str(snapshot["decision_timestamp_utc"]))
        bars = snapshot["completed_bars"]
        if (
            snapshot.get("instrument") != decision.instrument
            or snapshot.get("epic") != decision.epic
            or snapshot_timestamp != decision.decision_timestamp_utc
            or not isinstance(bars, list)
            or not bars
            or not isinstance(bars[-1], dict)
        ):
            raise ValueError("snapshot identity invalid")
        final_bar = bars[-1]
        completed_at = _parse_timestamp(str(final_bar["completed_at"]))
        entry_price = final_bar["close"]
        if completed_at >= decision.decision_timestamp_utc or not is_finite_positive(entry_price):
            raise ValueError("snapshot entry invalid")
    except (KeyError, TypeError, ValueError) as error:
        raise ShadowStoreError("SHADOW01_OUTCOME_SNAPSHOT_EVIDENCE_INVALID") from error

    if technical_raw is None:
        return float(entry_price), None
    technical = _json_object(technical_raw, "SHADOW01_OUTCOME_TECHNICAL_EVIDENCE_INVALID")
    atr20_over_price = technical.get("atr20_over_price")
    if atr20_over_price is None:
        return float(entry_price), None
    if not is_finite_positive(atr20_over_price):
        raise ShadowStoreError("SHADOW01_OUTCOME_TECHNICAL_EVIDENCE_INVALID")
    return float(entry_price), float(atr20_over_price)


def _json_object(raw: str, code: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ShadowStoreError(code) from error
    if not isinstance(value, dict):
        raise ShadowStoreError(code)
    return value


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, object]]:
    return [dict(row) for row in cursor.fetchall()]


def _validated_config_json(config: ShadowTournamentConfig) -> str:
    """Return the exact canonical payload only for an OFF configuration.

    This is deliberately checked at every public persistence boundary.  A
    dataclass instance built with a hand-written, mismatching fingerprint must
    never create a run row, an epoch, or an observation record.
    """

    if not isinstance(config, ShadowTournamentConfig) or not config.fingerprint_is_valid:
        raise ShadowStoreError("SHADOW01_CONFIG_FINGERPRINT_INVALID")
    try:
        payload = config.payload
        version = config.version
    except (KeyError, TypeError, ValueError) as error:
        raise ShadowStoreError("SHADOW01_CONFIG_FINGERPRINT_INVALID") from error
    if (
        not version.startswith("SHADOW01-V")
        or payload.get("tournament_version") != version
        or payload.get("execution_authority") != "OFF"
    ):
        raise ShadowStoreError("SHADOW01_CONFIG_FINGERPRINT_INVALID")
    return _json(payload)


def _decision_anchor(config: ShadowTournamentConfig, timestamp: datetime) -> datetime:
    """Convert only the frozen New York decision clock into a UTC instant."""

    try:
        return require_decision_anchor(config, timestamp)
    except ShadowClockError as error:
        raise ShadowStoreError(str(error)) from error


def _asset_classes_by_instrument(config: ShadowTournamentConfig) -> dict[str, str]:
    return {item["symbol"]: item["asset_class"] for item in config.universe}


def _require_configured_instrument(config: ShadowTournamentConfig, instrument: str) -> None:
    if instrument not in _asset_classes_by_instrument(config):
        raise ShadowStoreError("SHADOW01_INSTRUMENT_OUTSIDE_FROZEN_UNIVERSE")


def _require_configured_snapshot(config: ShadowTournamentConfig, snapshot: MarketSnapshot) -> None:
    asset_classes = _asset_classes_by_instrument(config)
    configured_asset_class = asset_classes.get(snapshot.instrument)
    if configured_asset_class is None:
        raise ShadowStoreError("SHADOW01_INSTRUMENT_OUTSIDE_FROZEN_UNIVERSE")
    if configured_asset_class != snapshot.asset_class.value:
        raise ShadowStoreError("SHADOW01_SNAPSHOT_ASSET_CLASS_UNPROVEN")


def _validate_decision_persistence_contract(decision: ShadowDecision) -> None:
    """Recheck all frozen policy gates before an append-only write.

    Normal callers already receive these checks in the model and policy layers,
    but this boundary remains defensive against a deliberately mutated frozen
    dataclass or a direct object constructor.
    """

    expected_engine = _POLICY_ENGINES.get(decision.policy_id)
    if expected_engine is None or decision.technical_engine != expected_engine:
        raise ShadowStoreError("SHADOW01_DECISION_POLICY_ENGINE_INVALID")
    if decision.quality_state is QualityState.BLOCKED and decision.direction is not Direction.BLOCK:
        raise ShadowStoreError("SHADOW01_DECISION_QUALITY_BLOCK_REQUIRED")
    directional = decision.direction in {Direction.LONG, Direction.SHORT}
    if (
        directional
        and decision.policy_id is PolicyId.P2_TREND_PLUS_CROSS_ASSET
        and decision.cross_asset_state not in {ContextState.SUPPORTIVE, ContextState.NEUTRAL}
    ):
        raise ShadowStoreError("SHADOW01_DECISION_P2_CONTEXT_BLOCK_REQUIRED")
    if (
        directional
        and decision.policy_id is PolicyId.P3_CONSERVATIVE_CONTEXT
        and (
            decision.cost_state is CostState.COST_HIGH
            or decision.cross_asset_state is ContextState.OPPOSES
            or decision.fundamental_context is FundamentalState.EVENT_RISK
        )
    ):
        raise ShadowStoreError("SHADOW01_DECISION_P3_CONTEXT_BLOCK_REQUIRED")
    try:
        expected_tags = expected_factor_tags(decision.instrument, decision.direction)
    except ValueError as error:
        raise ShadowStoreError("SHADOW01_DECISION_FACTOR_TAGS_INVALID") from error
    if decision.factor_tags != expected_tags:
        raise ShadowStoreError("SHADOW01_DECISION_FACTOR_TAGS_INVALID")


def _config_from_row(row: sqlite3.Row) -> ShadowTournamentConfig:
    """Rebuild and validate the canonical config retained with the run row."""

    raw = row["config_json"]
    if not isinstance(raw, str):
        raise ShadowStoreError("SHADOW01_VERSION_GOVERNANCE_UNKNOWN")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ShadowStoreError("SHADOW01_VERSION_GOVERNANCE_UNKNOWN") from error
    if not isinstance(payload, dict):
        raise ShadowStoreError("SHADOW01_VERSION_GOVERNANCE_UNKNOWN")
    stored = ShadowTournamentConfig(payload, str(row["config_fingerprint"]))
    if (
        not stored.fingerprint_is_valid
        or stored.version != str(row["tournament_version"])
        or _validated_config_json(stored) != raw
    ):
        raise ShadowStoreError("SHADOW01_VERSION_GOVERNANCE_UNKNOWN")
    return stored


def _has_verified_universal_probe(connection: sqlite3.Connection, observed_at: datetime) -> bool:
    """Check the single all-asset-class probe emitted by the read-only runner."""

    rows = connection.execute(
        """
        SELECT data_json
        FROM provider_health
        WHERE observed_at_utc = ?
          AND provider = 'IG_READ_ONLY'
          AND status = 'HEALTHY'
          AND detail = 'SHADOW01_READ_ONLY_CLOCK_PROBE_OK'
        ORDER BY id DESC
        """,
        (_timestamp(observed_at),),
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(str(row[0]))
        except json.JSONDecodeError:
            continue
        asset_classes = data.get("asset_classes") if isinstance(data, dict) else None
        if (
            isinstance(asset_classes, list)
            and {str(item) for item in asset_classes} >= _EXPECTED_ASSET_CLASSES
        ):
            return True
    return False


def _has_epoch_readiness(
    connection: sqlite3.Connection,
    config: ShadowTournamentConfig,
    *,
    epoch_utc: str,
) -> bool:
    config_json = _validated_config_json(config)
    rows = connection.execute(
        """
        SELECT provider_probe_at_utc
        FROM epoch_readiness
        WHERE tournament_version = ?
          AND config_fingerprint = ?
          AND config_json = ?
          AND snapshot_at_utc <= ?
        ORDER BY id DESC
        """,
        (config.version, config.fingerprint, config_json, epoch_utc),
    ).fetchall()
    for row in rows:
        try:
            provider_probe_at = _parse_timestamp(str(row[0]))
        except (TypeError, ValueError):
            continue
        if _has_verified_universal_probe(connection, provider_probe_at):
            return True
    return False


def _migrate_schema(connection: sqlite3.Connection) -> None:
    """Fail closed for pre-existing run rows that lack canonical payload bytes."""

    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(tournament_runs)").fetchall()
    }
    if "config_json" not in columns:
        connection.execute("ALTER TABLE tournament_runs ADD COLUMN config_json TEXT")


def _epoch_from_connection(
    connection: sqlite3.Connection, config: ShadowTournamentConfig
) -> datetime | None:
    row = connection.execute(
        """
        SELECT tournament_version, epoch_utc, config_fingerprint, execution_authority, config_json
        FROM tournament_runs WHERE tournament_version = ?
        """,
        (config.version,),
    ).fetchone()
    if row is None:
        return None
    stored = _config_from_row(row)
    if (
        stored.fingerprint != config.fingerprint
        or _validated_config_json(config) != str(row["config_json"])
        or row["execution_authority"] != "OFF"
    ):
        raise ShadowStoreError("SHADOW01_VERSION_GOVERNANCE_UNKNOWN")
    return _parse_timestamp(str(row["epoch_utc"])) if row["epoch_utc"] else None


def _market_observation_state(
    connection: sqlite3.Connection,
    config: ShadowTournamentConfig,
    *,
    anchored_timestamp: datetime,
    instrument: str,
) -> str:
    """Classify one anchor's local evidence without mutating it.

    The uniqueness constraints protect each row class independently, but the
    runtime needs a stronger all-or-nothing definition before it may skip a
    provider read on restart.  Any unexpected row mix is deliberately partial,
    never silently accepted as an observation.
    """

    timestamp = _timestamp(anchored_timestamp)
    snapshot_count = int(
        connection.execute(
            """
            SELECT count(*)
            FROM market_snapshots
            WHERE tournament_version = ?
              AND decision_timestamp_utc = ?
              AND instrument = ?
            """,
            (config.version, timestamp, instrument),
        ).fetchone()[0]
    )
    engine_rows = connection.execute(
        """
        SELECT engine_id
        FROM engine_insights
        WHERE tournament_version = ?
          AND decision_timestamp_utc = ?
          AND instrument = ?
        """,
        (config.version, timestamp, instrument),
    ).fetchall()
    decision_rows = connection.execute(
        """
        SELECT policy_id, config_fingerprint
        FROM shadow_decisions
        WHERE tournament_version = ?
          AND decision_timestamp_utc = ?
          AND instrument = ?
        """,
        (config.version, timestamp, instrument),
    ).fetchall()
    if snapshot_count == 0 and not engine_rows and not decision_rows:
        return _MARKET_OBSERVATION_ABSENT

    engine_ids = {str(row[0]) for row in engine_rows}
    policy_ids = {str(row[0]) for row in decision_rows}
    fingerprints = {str(row[1]) for row in decision_rows}
    expected_policy_ids = {policy.value for policy in _POLICY_ENGINES}
    if (
        snapshot_count == 1
        and len(engine_rows) == len(_ENGINE_IDS)
        and engine_ids == _ENGINE_IDS
        and len(decision_rows) == len(expected_policy_ids)
        and policy_ids == expected_policy_ids
        and fingerprints == {config.fingerprint}
    ):
        return _MARKET_OBSERVATION_COMPLETE
    return _MARKET_OBSERVATION_PARTIAL


def _decode_snapshot(row: dict[str, object]) -> dict[str, object]:
    raw = row.pop("snapshot_json", "{}")
    try:
        document = json.loads(str(raw))
    except json.JSONDecodeError:
        document = {}
    return {**row, "snapshot": document if isinstance(document, dict) else {}}


def _decode_insight(row: dict[str, object]) -> dict[str, object]:
    raw = row.pop("insight_json", "{}")
    try:
        insight = json.loads(str(raw))
    except json.JSONDecodeError:
        insight = {}
    return {**row, "insight": insight if isinstance(insight, dict) else {}}


def _decode_decision(row: dict[str, object]) -> dict[str, object]:
    for field in ("factor_tags_json", "reason_codes_json"):
        try:
            row[field.removesuffix("_json")] = json.loads(str(row.pop(field, "[]")))
        except json.JSONDecodeError:
            row[field.removesuffix("_json")] = []
    return row


def _leaderboard(
    decisions: list[dict[str, object]],
    outcomes: list[dict[str, object]],
    asset_classes_by_instrument: dict[str, str],
) -> list[dict[str, object]]:
    """Build evidence-only rows at every configured policy/engine/market horizon.

    Flat and blocked opinions stay in the denominator as abstentions.  A
    missing or blocked delayed label never becomes a zero return, which keeps
    coverage honest while the tournament is still young.
    """

    groups: dict[tuple[str, str, str, str, int], dict[str, object]] = {}
    for decision in decisions:
        policy_id = str(decision.get("policy_id", "UNPROVEN"))
        engine_id = str(decision.get("technical_engine", "UNPROVEN"))
        instrument = str(decision.get("instrument", "UNPROVEN"))
        asset_class = asset_classes_by_instrument.get(instrument, "UNPROVEN")
        directional = decision.get("direction") in {Direction.LONG.value, Direction.SHORT.value}
        for horizon in _OUTCOME_HORIZONS:
            key = (policy_id, engine_id, instrument, asset_class, horizon)
            group = groups.setdefault(
                key,
                {
                    "decision_count": 0,
                    "directional_decision_count": 0,
                    "outcomes": [],
                },
            )
            group["decision_count"] = int(group["decision_count"]) + 1
            if directional:
                group["directional_decision_count"] = int(group["directional_decision_count"]) + 1

    for outcome in outcomes:
        try:
            horizon = int(outcome["horizon_sessions"])
        except (KeyError, TypeError, ValueError):
            continue
        if horizon not in _OUTCOME_HORIZONS:
            continue
        policy_id = str(outcome.get("policy_id", "UNPROVEN"))
        engine_id = str(outcome.get("technical_engine", "UNPROVEN"))
        instrument = str(outcome.get("instrument", "UNPROVEN"))
        key = (
            policy_id,
            engine_id,
            instrument,
            asset_classes_by_instrument.get(instrument, "UNPROVEN"),
            horizon,
        )
        group = groups.get(key)
        if group is not None:
            rows = group["outcomes"]
            assert isinstance(rows, list)
            rows.append(outcome)

    result: list[dict[str, object]] = []
    for (policy_id, engine_id, instrument, asset_class, horizon), group in sorted(groups.items()):
        decision_count = int(group["decision_count"])
        directional_count = int(group["directional_decision_count"])
        items = group["outcomes"]
        assert isinstance(items, list)
        labels = [item for item in items if isinstance(item, dict)]
        valid = [item for item in labels if item.get("raw_directional_return") is not None]
        returns = [float(item["raw_directional_return"]) for item in valid]
        atr_returns = [
            float(item["atr_normalized_return"])
            for item in valid
            if item.get("atr_normalized_return") is not None
        ]
        resolved_count = len(valid)
        label_count = len(labels)
        abstention_count = decision_count - directional_count
        result.append(
            {
                "policy_id": policy_id,
                "technical_engine": engine_id,
                "instrument": instrument,
                "asset_class": asset_class,
                "horizon_sessions": horizon,
                "decision_count": decision_count,
                "directional_decision_count": directional_count,
                "abstention_count": abstention_count,
                "abstention_rate": abstention_count / decision_count if decision_count else None,
                "label_count": label_count,
                "label_coverage": label_count / directional_count if directional_count else None,
                "resolved_count": resolved_count,
                "resolved_coverage": resolved_count / directional_count
                if directional_count
                else None,
                "blocked_outcome_count": sum(item.get("quality") == "BLOCKED" for item in labels),
                "directional_accuracy": sum(value > 0 for value in returns) / resolved_count
                if resolved_count
                else None,
                "mean_directional_return": sum(returns) / resolved_count
                if resolved_count
                else None,
                "median_directional_return": _median(returns),
                "mean_atr_normalized_return": sum(atr_returns) / len(atr_returns)
                if atr_returns
                else None,
                "evidence_status": _evidence_status(resolved_count),
            }
        )
    return result


def _factor_audit(decisions: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for decision in decisions:
        timestamp = str(decision.get("decision_timestamp_utc"))
        policy = str(decision.get("policy_id"))
        tags = decision.get("factor_tags")
        if not isinstance(tags, list) or decision.get("direction") not in {"LONG", "SHORT"}:
            continue
        key = (timestamp, policy)
        counts[key] += 1
        groups[key].update(str(tag) for tag in tags)
    return [
        {
            "decision_timestamp_utc": timestamp,
            "policy_id": policy,
            "directional_decision_count": counts[(timestamp, policy)],
            "unique_factor_bets": sorted(groups[(timestamp, policy)]),
            "unique_factor_bet_count": len(groups[(timestamp, policy)]),
        }
        for timestamp, policy in sorted(groups, reverse=True)
    ]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def _evidence_status(count: int) -> str:
    if count < 30:
        return "INSUFFICIENT_EVIDENCE"
    if count < 100:
        return "EARLY_EVIDENCE"
    return "EVALUABLE"


def _empty_dashboard(config: ShadowTournamentConfig, reason: str) -> dict[str, object]:
    return {
        "available": False,
        "reason": reason,
        "tournament_version": config.version,
        "config_fingerprint": config.fingerprint,
        "execution_authority": "OFF",
        "epoch_utc": None,
        "epoch_created": False,
        "provider_health": [],
        "epoch_readiness": [],
        "market_snapshots": [],
        "engine_insights": [],
        "latest_decisions": [],
        "resolved_outcomes": [],
        "leaderboard": [],
        "factor_audit": [],
    }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tournament_runs (
    tournament_version TEXT PRIMARY KEY,
    config_fingerprint TEXT NOT NULL,
    config_json TEXT NOT NULL,
    epoch_utc TEXT,
    epoch_authorization TEXT,
    created_at_utc TEXT NOT NULL,
    execution_authority TEXT NOT NULL CHECK (execution_authority = 'OFF')
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY,
    tournament_version TEXT NOT NULL REFERENCES tournament_runs(tournament_version),
    decision_timestamp_utc TEXT NOT NULL,
    instrument TEXT NOT NULL,
    epic TEXT NOT NULL,
    input_data_fingerprint TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (tournament_version, decision_timestamp_utc, instrument)
);

CREATE TABLE IF NOT EXISTS engine_insights (
    id INTEGER PRIMARY KEY,
    tournament_version TEXT NOT NULL REFERENCES tournament_runs(tournament_version),
    decision_timestamp_utc TEXT NOT NULL,
    instrument TEXT NOT NULL,
    engine_id TEXT NOT NULL,
    insight_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (tournament_version, decision_timestamp_utc, instrument, engine_id)
);

CREATE TABLE IF NOT EXISTS shadow_decisions (
    decision_id TEXT PRIMARY KEY,
    tournament_version TEXT NOT NULL REFERENCES tournament_runs(tournament_version),
    config_fingerprint TEXT NOT NULL,
    decision_timestamp_utc TEXT NOT NULL,
    instrument TEXT NOT NULL,
    epic TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT', 'FLAT', 'BLOCK')),
    technical_engine TEXT NOT NULL,
    technical_score REAL,
    cross_asset_state TEXT NOT NULL,
    fundamental_context TEXT NOT NULL,
    quality_state TEXT NOT NULL,
    cost_state TEXT NOT NULL,
    factor_tags_json TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    input_data_fingerprint TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    UNIQUE (tournament_version, decision_timestamp_utc, instrument, policy_id)
);

CREATE TABLE IF NOT EXISTS outcome_labels (
    id INTEGER PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES shadow_decisions(decision_id),
    horizon_sessions INTEGER NOT NULL CHECK (horizon_sessions IN (1, 3, 5, 10, 20)),
    reference_entry_price REAL,
    future_price REAL,
    raw_directional_return REAL,
    atr_normalized_return REAL,
    cost_adjusted_result REAL,
    outcome_timestamp_utc TEXT,
    quality TEXT NOT NULL,
    blocked_reason TEXT,
    outcome_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (decision_id, horizon_sessions)
);

CREATE TABLE IF NOT EXISTS provider_health (
    id INTEGER PRIMARY KEY,
    observed_at_utc TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS epoch_readiness (
    id INTEGER PRIMARY KEY,
    tournament_version TEXT NOT NULL REFERENCES tournament_runs(tournament_version),
    config_fingerprint TEXT NOT NULL,
    config_json TEXT NOT NULL,
    provider_probe_at_utc TEXT NOT NULL,
    snapshot_at_utc TEXT NOT NULL,
    instrument TEXT NOT NULL,
    epic TEXT NOT NULL,
    input_data_fingerprint TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (tournament_version, config_fingerprint, snapshot_at_utc, instrument)
);
"""


_TRIGGERS = """
DROP TRIGGER IF EXISTS shadow01_tournament_config_immutable;
DROP TRIGGER IF EXISTS shadow01_epoch_immutable;
DROP TRIGGER IF EXISTS shadow01_tournament_runs_append_only_insert;
DROP TRIGGER IF EXISTS shadow01_tournament_runs_append_only_update;
DROP TRIGGER IF EXISTS shadow01_tournament_runs_append_only_delete;
DROP TRIGGER IF EXISTS shadow01_epoch_readiness_append_only_insert;
DROP TRIGGER IF EXISTS shadow01_epoch_readiness_append_only_update;
DROP TRIGGER IF EXISTS shadow01_epoch_readiness_append_only_delete;
DROP TRIGGER IF EXISTS shadow01_provider_health_append_only_update;
DROP TRIGGER IF EXISTS shadow01_provider_health_append_only_delete;
DROP TRIGGER IF EXISTS shadow01_market_snapshots_append_only_update;
DROP TRIGGER IF EXISTS shadow01_market_snapshots_append_only_delete;
DROP TRIGGER IF EXISTS shadow01_engine_insights_append_only_update;
DROP TRIGGER IF EXISTS shadow01_engine_insights_append_only_delete;
DROP TRIGGER IF EXISTS shadow01_decisions_append_only_update;
DROP TRIGGER IF EXISTS shadow01_decisions_append_only_delete;
DROP TRIGGER IF EXISTS shadow01_outcomes_append_only_update;
DROP TRIGGER IF EXISTS shadow01_outcomes_append_only_delete;

CREATE TRIGGER shadow01_tournament_runs_append_only_insert
BEFORE INSERT ON tournament_runs
WHEN NEW.epoch_utc IS NOT NULL
  OR NEW.epoch_authorization IS NOT NULL
  OR NEW.execution_authority != 'OFF'
  OR NEW.config_json IS NULL
BEGIN SELECT RAISE(ABORT, 'SHADOW01 tournament runs must begin without an epoch'); END;

CREATE TRIGGER shadow01_tournament_runs_append_only_update
BEFORE UPDATE ON tournament_runs
WHEN NOT (
    OLD.epoch_utc IS NULL
    AND OLD.epoch_authorization IS NULL
    AND NEW.epoch_utc IS NOT NULL
    AND NEW.epoch_authorization = ('START ' || OLD.tournament_version || ' EPOCH')
    AND NEW.tournament_version = OLD.tournament_version
    AND NEW.config_fingerprint = OLD.config_fingerprint
    AND NEW.config_json = OLD.config_json
    AND NEW.created_at_utc = OLD.created_at_utc
    AND NEW.execution_authority = 'OFF'
    AND EXISTS (
        SELECT 1
        FROM epoch_readiness readiness
        WHERE readiness.tournament_version = OLD.tournament_version
          AND readiness.config_fingerprint = OLD.config_fingerprint
          AND readiness.config_json = OLD.config_json
          AND readiness.snapshot_at_utc <= NEW.epoch_utc
          AND EXISTS (
              SELECT 1
              FROM provider_health probe
              WHERE probe.observed_at_utc = readiness.provider_probe_at_utc
                AND probe.provider = 'IG_READ_ONLY'
                AND probe.status = 'HEALTHY'
                AND probe.detail = 'SHADOW01_READ_ONLY_CLOCK_PROBE_OK'
          )
    )
)
BEGIN SELECT RAISE(ABORT, 'SHADOW01 tournament runs are append-only'); END;
CREATE TRIGGER shadow01_tournament_runs_append_only_delete
BEFORE DELETE ON tournament_runs
BEGIN SELECT RAISE(ABORT, 'SHADOW01 tournament runs are append-only'); END;

CREATE TRIGGER shadow01_epoch_readiness_append_only_insert
BEFORE INSERT ON epoch_readiness
WHEN NOT EXISTS (
    SELECT 1
    FROM tournament_runs
    WHERE tournament_version = NEW.tournament_version
      AND config_fingerprint = NEW.config_fingerprint
      AND config_json = NEW.config_json
      AND epoch_utc IS NULL
      AND execution_authority = 'OFF'
)
BEGIN SELECT RAISE(ABORT, 'SHADOW01 epoch readiness requires an unstarted proven run'); END;
CREATE TRIGGER shadow01_epoch_readiness_append_only_update
BEFORE UPDATE ON epoch_readiness
BEGIN SELECT RAISE(ABORT, 'SHADOW01 epoch readiness is append-only'); END;
CREATE TRIGGER shadow01_epoch_readiness_append_only_delete
BEFORE DELETE ON epoch_readiness
BEGIN SELECT RAISE(ABORT, 'SHADOW01 epoch readiness is append-only'); END;

CREATE TRIGGER shadow01_provider_health_append_only_update
BEFORE UPDATE ON provider_health
BEGIN SELECT RAISE(ABORT, 'SHADOW01 provider health is append-only'); END;
CREATE TRIGGER shadow01_provider_health_append_only_delete
BEFORE DELETE ON provider_health
BEGIN SELECT RAISE(ABORT, 'SHADOW01 provider health is append-only'); END;

CREATE TRIGGER shadow01_market_snapshots_append_only_update
BEFORE UPDATE ON market_snapshots
BEGIN SELECT RAISE(ABORT, 'SHADOW01 market snapshots are append-only'); END;
CREATE TRIGGER shadow01_market_snapshots_append_only_delete
BEFORE DELETE ON market_snapshots
BEGIN SELECT RAISE(ABORT, 'SHADOW01 market snapshots are append-only'); END;
CREATE TRIGGER shadow01_engine_insights_append_only_update
BEFORE UPDATE ON engine_insights
BEGIN SELECT RAISE(ABORT, 'SHADOW01 engine insights are append-only'); END;
CREATE TRIGGER shadow01_engine_insights_append_only_delete
BEFORE DELETE ON engine_insights
BEGIN SELECT RAISE(ABORT, 'SHADOW01 engine insights are append-only'); END;
CREATE TRIGGER shadow01_decisions_append_only_update
BEFORE UPDATE ON shadow_decisions
BEGIN SELECT RAISE(ABORT, 'SHADOW01 decisions are append-only'); END;
CREATE TRIGGER shadow01_decisions_append_only_delete
BEFORE DELETE ON shadow_decisions
BEGIN SELECT RAISE(ABORT, 'SHADOW01 decisions are append-only'); END;
CREATE TRIGGER shadow01_outcomes_append_only_update
BEFORE UPDATE ON outcome_labels
BEGIN SELECT RAISE(ABORT, 'SHADOW01 outcomes are append-only'); END;
CREATE TRIGGER shadow01_outcomes_append_only_delete
BEFORE DELETE ON outcome_labels
BEGIN SELECT RAISE(ABORT, 'SHADOW01 outcomes are append-only'); END;
"""
