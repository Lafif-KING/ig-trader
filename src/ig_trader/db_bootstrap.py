"""Finite PostgreSQL bootstrap and runtime-probe jobs for the private Azure path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from ig_trader.execution_lease import (
    EXECUTION_LEASE_NAME,
    POSTGRES_ENTRA_SCOPE,
    FencedOperation,
    FencingRejected,
    LeaseError,
    LeaseRecord,
    PostgresExecutionLeaseStore,
)

RUNTIME_PRINCIPAL_NAME = "igtrdevfrc-execution-identity"
BOOTSTRAP_PRINCIPAL_NAME = "igtrdevfrc-db-bootstrap-identity"
DATABASE_NAME = "ig_trader"

EXPECTED_MIGRATION_HASHES = {
    "001_execution_state": "42dcbe2b47c5fed8223a4831d8c594e78c3180f454b71e15358819a9039c8800",
    "002_execution_lease_fencing": (
        "731b918b573ee232aab3fa709e7a41b5ac03e11f4f81d08458f8fcefcb16599c"
    ),
}

_POSTGRES_HOST_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\.postgres\.database\.azure\.com\Z"
)
_INSTANCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_FORBIDDEN_ENVIRONMENT = {
    "DATABASE_URL",
    "PGPASSWORD",
    "POSTGRES_PASSWORD",
    "POSTGRESQL_PASSWORD",
}


class BootstrapClassification(StrEnum):
    """Sanitized terminal classifications emitted by the finite jobs."""

    PASS_SCHEMA_INSPECTION = "PASS_SCHEMA_INSPECTION"
    PASS_BOOTSTRAP_ADMIN = "PASS_BOOTSTRAP_ADMIN"
    PASS_RUNTIME_PROBE = "PASS_RUNTIME_PROBE"
    DATABASE_SCHEMA_DRIFT = "DATABASE_SCHEMA_DRIFT"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    PRIVILEGE_MISMATCH = "PRIVILEGE_MISMATCH"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    CONFIGURATION_REJECTED = "CONFIGURATION_REJECTED"


class BootstrapError(RuntimeError):
    """Base error whose classification is safe to expose without details."""

    classification = BootstrapClassification.CONFIGURATION_REJECTED


class DatabaseSchemaDrift(BootstrapError):
    classification = BootstrapClassification.DATABASE_SCHEMA_DRIFT


class IdentityMismatch(BootstrapError):
    classification = BootstrapClassification.IDENTITY_MISMATCH


class PrivilegeMismatch(BootstrapError):
    classification = BootstrapClassification.PRIVILEGE_MISMATCH


class DatabaseUnavailable(BootstrapError):
    classification = BootstrapClassification.DATABASE_UNAVAILABLE


class MigrationState(StrEnum):
    ABSENT = "ABSENT"
    COMPLETE = "COMPLETE"
    PARTIAL_DRIFTED = "PARTIAL_DRIFTED"


class SchemaClassification(StrEnum):
    """Accepted pre-bootstrap database states."""

    BLANK = "BLANK"
    MIGRATION_001_COMPLETE_ONLY = "001_COMPLETE_ONLY"
    MIGRATIONS_001_AND_002_COMPLETE = "001_AND_002_COMPLETE"


@dataclass(frozen=True)
class MigrationDefinition:
    version: str
    filename: str
    checksum_sha256: str
    required_markers: frozenset[str]


MIGRATION_001 = MigrationDefinition(
    version="001_execution_state",
    filename="001_execution_state.sql",
    checksum_sha256=EXPECTED_MIGRATION_HASHES["001_execution_state"],
    required_markers=frozenset(
        {
            "schema:trading",
            "relation:schema_migrations",
            "relation:trade_intents",
            "relation:lifecycle_events",
            "relation:lifecycle_events_sequence_seq",
            "relation:broker_references",
            "relation:position_state",
            "relation:reconciliation_state",
            "relation:evidence_metadata",
            "relation:worker_leases",
            "constraint-count:schema_migrations:c:1",
            "constraint-count:schema_migrations:p:1",
            "constraint-count:trade_intents:c:6",
            "constraint-count:trade_intents:p:1",
            "constraint-count:trade_intents:u:1",
            "constraint-count:lifecycle_events:c:2",
            "constraint-count:lifecycle_events:f:1",
            "constraint-count:lifecycle_events:p:1",
            "constraint-count:lifecycle_events:u:1",
            "constraint-count:broker_references:c:3",
            "constraint-count:broker_references:f:1",
            "constraint-count:broker_references:p:1",
            "constraint-count:broker_references:u:2",
            "constraint-count:position_state:c:3",
            "constraint-count:position_state:f:1",
            "constraint-count:position_state:p:1",
            "constraint-count:position_state:u:2",
            "constraint-count:reconciliation_state:c:3",
            "constraint-count:reconciliation_state:p:1",
            "constraint-count:evidence_metadata:c:3",
            "constraint-count:evidence_metadata:f:1",
            "constraint-count:evidence_metadata:p:1",
            "constraint-count:evidence_metadata:u:1",
            "constraint-count:worker_leases:c:4",
            "constraint-count:worker_leases:p:1",
            "function:reject_append_only_mutation",
            "trigger:lifecycle_events_append_only",
            "trigger:evidence_metadata_append_only",
        }
    ),
)

MIGRATION_002 = MigrationDefinition(
    version="002_execution_lease_fencing",
    filename="002_execution_lease_fencing.sql",
    checksum_sha256=EXPECTED_MIGRATION_HASHES["002_execution_lease_fencing"],
    required_markers=frozenset(
        {
            "relation:execution_cycle_claims",
            "relation:worker_lease_fencing_token_seq",
            "constraint-count:execution_cycle_claims:c:4",
            "constraint-count:execution_cycle_claims:p:1",
            "column:worker_leases.heartbeat_at:not-null",
            "function:acquire_execution_lease",
            "function:renew_execution_lease",
            "function:release_execution_lease",
            "function:assert_execution_fence",
            "function:require_current_execution_fence",
            "trigger:execution_cycle_claims_require_fence",
            "trigger:trade_intents_require_fence",
            "trigger:lifecycle_events_require_fence",
            "trigger:broker_references_require_fence",
            "trigger:position_state_require_fence",
            "trigger:reconciliation_state_require_fence",
            "trigger:evidence_metadata_require_fence",
        }
    ),
)

MIGRATIONS = (MIGRATION_001, MIGRATION_002)
_KNOWN_MARKERS = frozenset().union(*(migration.required_markers for migration in MIGRATIONS))


@dataclass(frozen=True)
class MigrationSource:
    definition: MigrationDefinition
    sql: str


@dataclass(frozen=True)
class SchemaSnapshot:
    markers: frozenset[str]
    ledger: Mapping[str, str]

    def state(self, migration: MigrationDefinition) -> MigrationState:
        present = self.markers & migration.required_markers
        if not present:
            return MigrationState.ABSENT
        if present == migration.required_markers:
            return MigrationState.COMPLETE
        return MigrationState.PARTIAL_DRIFTED


@dataclass(frozen=True)
class PrincipalRecord:
    role_name: str
    principal_type: str
    object_id: str
    is_admin: bool
    is_superuser: bool = False
    can_create_role: bool = False
    can_create_database: bool = False
    azure_pg_admin_member: bool = False


@dataclass(frozen=True)
class PrivilegeSnapshot:
    database_connect: bool
    database_create: bool
    schema_usage: bool
    schema_create: bool
    missing_required: tuple[str, ...]
    prohibited_present: tuple[str, ...]
    owned_object_count: int


@dataclass(frozen=True)
class JobConfig:
    mode: str
    host: str
    database: str
    database_user: str
    client_id: str
    job_identity_name: str
    job_identity_object_id: str
    runtime_identity_object_id: str | None

    @classmethod
    def from_environment(cls, mode: str, environment: Mapping[str, str]) -> JobConfig:
        if any(environment.get(name, "").strip() for name in _FORBIDDEN_ENVIRONMENT):
            raise BootstrapError("password and DSN configuration is prohibited")
        host = environment.get("POSTGRES_HOST", "").strip().casefold()
        database = environment.get("POSTGRES_DATABASE", DATABASE_NAME).strip()
        client_id = _uuid(environment.get("AZURE_CLIENT_ID", ""), "AZURE_CLIENT_ID")
        identity_name = environment.get("JOB_IDENTITY_NAME", "").strip()
        identity_object_id = _uuid(
            environment.get("JOB_UAMI_OBJECT_ID", ""),
            "JOB_UAMI_OBJECT_ID",
        )
        if not _POSTGRES_HOST_PATTERN.fullmatch(host):
            raise BootstrapError("private Azure PostgreSQL host is required")
        if database != DATABASE_NAME:
            raise BootstrapError("only the accepted ig_trader database is supported")
        if mode in {"bootstrap-admin", "schema-inspect"}:
            if identity_name != BOOTSTRAP_PRINCIPAL_NAME:
                raise IdentityMismatch("bootstrap identity is required")
            runtime_object_id = None
            if mode == "bootstrap-admin":
                runtime_object_id = _uuid(
                    environment.get("RUNTIME_UAMI_OBJECT_ID", ""),
                    "RUNTIME_UAMI_OBJECT_ID",
                )
                if runtime_object_id == identity_object_id:
                    raise IdentityMismatch("bootstrap and runtime identities must be distinct")
            database_user = BOOTSTRAP_PRINCIPAL_NAME
        elif mode == "runtime-probe":
            if identity_name != RUNTIME_PRINCIPAL_NAME:
                raise IdentityMismatch("runtime identity is required")
            runtime_object_id = None
            database_user = RUNTIME_PRINCIPAL_NAME
        else:
            raise BootstrapError("unsupported job mode")
        return cls(
            mode=mode,
            host=host,
            database=database,
            database_user=database_user,
            client_id=client_id,
            job_identity_name=identity_name,
            job_identity_object_id=identity_object_id,
            runtime_identity_object_id=runtime_object_id,
        )


class ManagedIdentityConnectionFactory:
    """Create TLS PostgreSQL connections without retaining or logging access tokens."""

    def __init__(self, config: JobConfig) -> None:
        from azure.identity import ManagedIdentityCredential

        self._config = config
        self._credential = ManagedIdentityCredential(client_id=config.client_id)
        self.token_acquired = False

    def __call__(self) -> Any:
        return self.connect_database(self._config.database)

    def administrator_connection(self) -> Any:
        """Connect to the required system database for Entra role management."""

        return self.connect_database("postgres")

    def connect_database(self, database: str) -> Any:
        try:
            import psycopg

            token = self._credential.get_token(POSTGRES_ENTRA_SCOPE).token
            if not token:
                raise RuntimeError("empty token")
            self.token_acquired = True
            return psycopg.connect(
                host=self._config.host,
                port=5432,
                dbname=database,
                user=self._config.database_user,
                password=token,
                sslmode="require",
                connect_timeout=10,
                application_name=f"ig-trader-{self._config.mode}",
                options="-c statement_timeout=30000 -c lock_timeout=5000",
            )
        except Exception:
            raise DatabaseUnavailable("managed-identity PostgreSQL connection failed") from None


def load_migration_sources(root: Path) -> tuple[MigrationSource, ...]:
    sources: list[MigrationSource] = []
    for definition in MIGRATIONS:
        path = root / definition.filename
        try:
            content = path.read_bytes()
        except OSError:
            raise DatabaseSchemaDrift("migration source is unavailable") from None
        checksum = hashlib.sha256(content).hexdigest()
        if checksum != definition.checksum_sha256:
            raise DatabaseSchemaDrift("migration source checksum differs from review")
        sql = content.decode("utf-8-sig")
        if not sql.strip().startswith("BEGIN;") or not sql.strip().endswith("COMMIT;"):
            raise DatabaseSchemaDrift("migration transaction boundary is missing")
        sources.append(MigrationSource(definition=definition, sql=sql))
    return tuple(sources)


def inspect_schema(connection: Any) -> SchemaSnapshot:
    schema_exists = bool(
        connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'trading')"
        ).fetchone()[0]
    )
    if not schema_exists:
        return SchemaSnapshot(markers=frozenset(), ledger={})

    markers = {"schema:trading"}
    relations = connection.execute(
        """
        SELECT c.relname
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'trading' AND c.relkind IN ('r', 'p', 'S')
        """
    ).fetchall()
    markers.update(f"relation:{row[0]}" for row in relations)
    functions = connection.execute(
        """
        SELECT DISTINCT p.proname
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname = 'trading'
        """
    ).fetchall()
    markers.update(f"function:{row[0]}" for row in functions)
    triggers = connection.execute(
        """
        SELECT t.tgname
        FROM pg_trigger AS t
        JOIN pg_class AS c ON c.oid = t.tgrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'trading' AND NOT t.tgisinternal
        """
    ).fetchall()
    markers.update(f"trigger:{row[0]}" for row in triggers)
    constraints = connection.execute(
        """
        SELECT c.relname, constraint_record.contype, count(*)
        FROM pg_constraint AS constraint_record
        JOIN pg_class AS c ON c.oid = constraint_record.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'trading'
        GROUP BY c.relname, constraint_record.contype
        """
    ).fetchall()
    markers.update(f"constraint-count:{row[0]}:{row[1]}:{row[2]}" for row in constraints)
    heartbeat = connection.execute(
        """
        SELECT is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'trading'
          AND table_name = 'worker_leases'
          AND column_name = 'heartbeat_at'
        """
    ).fetchone()
    if heartbeat and heartbeat[0] == "NO":
        markers.add("column:worker_leases.heartbeat_at:not-null")

    ledger: dict[str, str] = {}
    if "relation:schema_migrations" in markers:
        ledger = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT version, checksum_sha256 FROM trading.schema_migrations"
            ).fetchall()
        }
    return SchemaSnapshot(markers=frozenset(markers), ledger=ledger)


def plan_migrations(snapshot: SchemaSnapshot) -> tuple[MigrationDefinition, ...]:
    unexpected = snapshot.markers - _KNOWN_MARKERS
    unexpected_versions = set(snapshot.ledger) - set(EXPECTED_MIGRATION_HASHES)
    if unexpected or unexpected_versions:
        raise DatabaseSchemaDrift("unexpected trading schema objects exist")

    states = {migration.version: snapshot.state(migration) for migration in MIGRATIONS}
    for migration in MIGRATIONS:
        state = states[migration.version]
        ledger_hash = snapshot.ledger.get(migration.version)
        if state is MigrationState.PARTIAL_DRIFTED:
            raise DatabaseSchemaDrift("migration is partially present")
        if state is MigrationState.COMPLETE and ledger_hash != migration.checksum_sha256:
            raise DatabaseSchemaDrift("complete migration lacks its reviewed checksum")
        if state is MigrationState.ABSENT and ledger_hash is not None:
            raise DatabaseSchemaDrift("migration ledger and schema disagree")

    first = states[MIGRATION_001.version]
    second = states[MIGRATION_002.version]
    if first is MigrationState.ABSENT and second is not MigrationState.ABSENT:
        raise DatabaseSchemaDrift("migration ordering is invalid")
    if first is MigrationState.ABSENT:
        return MIGRATIONS
    if second is MigrationState.ABSENT:
        return (MIGRATION_002,)
    return ()


def classify_schema(snapshot: SchemaSnapshot) -> SchemaClassification:
    """Classify a verified schema without applying migrations or grants."""

    planned = plan_migrations(snapshot)
    if planned == MIGRATIONS:
        return SchemaClassification.BLANK
    if planned == (MIGRATION_002,):
        return SchemaClassification.MIGRATION_001_COMPLETE_ONLY
    if not planned:
        return SchemaClassification.MIGRATIONS_001_AND_002_COMPLETE
    raise DatabaseSchemaDrift("migration plan does not match an accepted state")


def apply_required_migrations(
    connection: Any,
    sources: Sequence[MigrationSource],
    applied_by: str,
) -> tuple[str, ...]:
    source_by_version = {source.definition.version: source for source in sources}
    planned = plan_migrations(inspect_schema(connection))
    connection.rollback()
    actions: list[str] = []
    for definition in planned:
        source = source_by_version[definition.version]
        migration_body = source.sql.strip().removeprefix("BEGIN;").removesuffix("COMMIT;")
        connection.execute(migration_body)
        connection.execute(
            """
            INSERT INTO trading.schema_migrations (
                version, checksum_sha256, applied_by
            ) VALUES (%s, %s, %s)
            """,
            (definition.version, definition.checksum_sha256, applied_by),
        )
        connection.commit()
        snapshot = inspect_schema(connection)
        if (
            snapshot.state(definition) is not MigrationState.COMPLETE
            or snapshot.ledger.get(definition.version) != definition.checksum_sha256
        ):
            raise DatabaseSchemaDrift("migration verification failed")
        connection.rollback()
        actions.append(definition.version)
    plan_migrations(inspect_schema(connection))
    connection.rollback()
    return tuple(actions)


def read_entra_principal(connection: Any, role_name: str) -> PrincipalRecord | None:
    rows = connection.execute(
        "SELECT * FROM pg_catalog.pgaadauth_list_principals(false)"
    ).fetchall()
    matching = [row for row in rows if str(row[0]) == role_name]
    if not matching:
        return None
    if len(matching) != 1:
        raise IdentityMismatch("database principal is ambiguous")
    row = matching[0]
    flags = connection.execute(
        """
        SELECT rolsuper, rolcreaterole, rolcreatedb,
               pg_has_role(%s, 'azure_pg_admin', 'MEMBER')
        FROM pg_roles
        WHERE rolname = %s
        """,
        (role_name, role_name),
    ).fetchone()
    if flags is None:
        raise IdentityMismatch("Entra principal has no PostgreSQL role")
    return PrincipalRecord(
        role_name=role_name,
        principal_type=str(row[1]).casefold(),
        object_id=str(row[2]).casefold(),
        is_admin=bool(row[5]),
        is_superuser=bool(flags[0]),
        can_create_role=bool(flags[1]),
        can_create_database=bool(flags[2]),
        azure_pg_admin_member=bool(flags[3]),
    )


def validate_runtime_principal(record: PrincipalRecord, expected_object_id: str) -> None:
    if (
        record.role_name != RUNTIME_PRINCIPAL_NAME
        or record.principal_type != "service"
        or record.object_id != expected_object_id.casefold()
        or record.is_admin
        or record.is_superuser
        or record.can_create_role
        or record.can_create_database
        or record.azure_pg_admin_member
    ):
        raise IdentityMismatch("runtime database principal is not the approved non-admin")


def verify_bootstrap_administrator(connection: Any, config: JobConfig) -> None:
    record = read_entra_principal(connection, BOOTSTRAP_PRINCIPAL_NAME)
    if (
        record is None
        or record.principal_type != "service"
        or record.object_id != config.job_identity_object_id.casefold()
        or not record.is_admin
        or not record.azure_pg_admin_member
    ):
        raise IdentityMismatch("temporary bootstrap identity is not the current Entra admin")


def ensure_runtime_principal(connection: Any, runtime_object_id: str) -> bool:
    record = read_entra_principal(connection, RUNTIME_PRINCIPAL_NAME)
    created = False
    if record is None:
        connection.execute(
            """
            SELECT *
            FROM pg_catalog.pgaadauth_create_principal_with_oid(
                %s, %s, 'service', false, false
            )
            """,
            (RUNTIME_PRINCIPAL_NAME, runtime_object_id),
        )
        connection.commit()
        created = True
        record = read_entra_principal(connection, RUNTIME_PRINCIPAL_NAME)
    if record is None:
        raise IdentityMismatch("runtime principal creation was not observable")
    validate_runtime_principal(record, runtime_object_id)
    return created


_ROLE = f'"{RUNTIME_PRINCIPAL_NAME}"'
GRANT_STATEMENTS = (
    f"REVOKE ALL ON DATABASE {DATABASE_NAME} FROM {_ROLE}",
    f"GRANT CONNECT ON DATABASE {DATABASE_NAME} TO {_ROLE}",
    f"REVOKE CREATE ON SCHEMA public FROM {_ROLE}",
    f"REVOKE ALL ON SCHEMA trading FROM {_ROLE}",
    f"GRANT USAGE ON SCHEMA trading TO {_ROLE}",
    f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA trading FROM {_ROLE}",
    f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA trading FROM {_ROLE}",
    (
        "GRANT SELECT ON trading.schema_migrations, trading.worker_leases, "
        "trading.execution_cycle_claims, trading.trade_intents, "
        "trading.lifecycle_events, trading.broker_references, trading.position_state, "
        f"trading.reconciliation_state, trading.evidence_metadata TO {_ROLE}"
    ),
    f"GRANT INSERT, UPDATE ON trading.execution_cycle_claims TO {_ROLE}",
    f"GRANT INSERT, UPDATE ON trading.trade_intents TO {_ROLE}",
    f"GRANT INSERT ON trading.lifecycle_events TO {_ROLE}",
    f"GRANT INSERT ON trading.broker_references TO {_ROLE}",
    f"GRANT INSERT, UPDATE ON trading.position_state TO {_ROLE}",
    f"GRANT INSERT, UPDATE ON trading.reconciliation_state TO {_ROLE}",
    f"GRANT INSERT ON trading.evidence_metadata TO {_ROLE}",
    f"GRANT USAGE, SELECT ON SEQUENCE trading.lifecycle_events_sequence_seq TO {_ROLE}",
    (
        "GRANT EXECUTE ON FUNCTION "
        "trading.acquire_execution_lease(text, text, double precision) "
        f"TO {_ROLE}"
    ),
    (
        "GRANT EXECUTE ON FUNCTION "
        "trading.renew_execution_lease(text, text, bigint, double precision) "
        f"TO {_ROLE}"
    ),
    (f"GRANT EXECUTE ON FUNCTION trading.release_execution_lease(text, text, bigint) TO {_ROLE}"),
    (
        "GRANT EXECUTE ON FUNCTION "
        "trading.assert_execution_fence(text, text, bigint, text) "
        f"TO {_ROLE}"
    ),
    (f"GRANT EXECUTE ON FUNCTION trading.require_current_execution_fence() TO {_ROLE}"),
)

_TABLE_PRIVILEGES = {
    "schema_migrations": {"SELECT"},
    "worker_leases": {"SELECT"},
    "execution_cycle_claims": {"SELECT", "INSERT", "UPDATE"},
    "trade_intents": {"SELECT", "INSERT", "UPDATE"},
    "lifecycle_events": {"SELECT", "INSERT"},
    "broker_references": {"SELECT", "INSERT"},
    "position_state": {"SELECT", "INSERT", "UPDATE"},
    "reconciliation_state": {"SELECT", "INSERT", "UPDATE"},
    "evidence_metadata": {"SELECT", "INSERT"},
}

_FUNCTION_SIGNATURES = (
    "trading.acquire_execution_lease(text,text,double precision)",
    "trading.renew_execution_lease(text,text,bigint,double precision)",
    "trading.release_execution_lease(text,text,bigint)",
    "trading.assert_execution_fence(text,text,bigint,text)",
    "trading.require_current_execution_fence()",
)


def apply_runtime_grants(connection: Any) -> None:
    for statement in GRANT_STATEMENTS:
        connection.execute(statement)
    connection.commit()


def read_runtime_privileges(connection: Any) -> PrivilegeSnapshot:
    role = RUNTIME_PRINCIPAL_NAME
    database_connect, database_create = connection.execute(
        "SELECT has_database_privilege(%s, current_database(), 'CONNECT'), "
        "has_database_privilege(%s, current_database(), 'CREATE')",
        (role, role),
    ).fetchone()
    schema_usage, schema_create = connection.execute(
        "SELECT has_schema_privilege(%s, 'trading', 'USAGE'), "
        "has_schema_privilege(%s, 'trading', 'CREATE')",
        (role, role),
    ).fetchone()
    missing: list[str] = []
    prohibited: list[str] = []
    for table, required in _TABLE_PRIVILEGES.items():
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            present = bool(
                connection.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f"trading.{table}", privilege),
                ).fetchone()[0]
            )
            label = f"trading.{table}:{privilege}"
            if privilege in required and not present:
                missing.append(label)
            if privilege not in required and present:
                prohibited.append(label)
    for privilege in ("USAGE", "SELECT"):
        if not connection.execute(
            "SELECT has_sequence_privilege(%s, %s, %s)",
            (role, "trading.lifecycle_events_sequence_seq", privilege),
        ).fetchone()[0]:
            missing.append(f"trading.lifecycle_events_sequence_seq:{privilege}")
    for signature in _FUNCTION_SIGNATURES:
        if not connection.execute(
            "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
            (role, signature),
        ).fetchone()[0]:
            missing.append(f"{signature}:EXECUTE")
    owned = int(
        connection.execute(
            """
            SELECT
                (SELECT count(*) FROM pg_class c JOIN pg_namespace n
                    ON n.oid = c.relnamespace
                 WHERE n.nspname = 'trading' AND pg_get_userbyid(c.relowner) = %s)
              + (SELECT count(*) FROM pg_proc p JOIN pg_namespace n
                    ON n.oid = p.pronamespace
                 WHERE n.nspname = 'trading' AND pg_get_userbyid(p.proowner) = %s)
            """,
            (role, role),
        ).fetchone()[0]
    )
    if database_create:
        prohibited.append("database:CREATE")
    if schema_create:
        prohibited.append("trading:CREATE")
    return PrivilegeSnapshot(
        database_connect=bool(database_connect),
        database_create=bool(database_create),
        schema_usage=bool(schema_usage),
        schema_create=bool(schema_create),
        missing_required=tuple(sorted(missing)),
        prohibited_present=tuple(sorted(prohibited)),
        owned_object_count=owned,
    )


def validate_runtime_privileges(snapshot: PrivilegeSnapshot) -> None:
    if (
        not snapshot.database_connect
        or not snapshot.schema_usage
        or snapshot.database_create
        or snapshot.schema_create
        or snapshot.missing_required
        or snapshot.prohibited_present
        or snapshot.owned_object_count
    ):
        raise PrivilegeMismatch("runtime database privileges differ from the approved set")


def _privilege_evidence(snapshot: PrivilegeSnapshot) -> dict[str, Any]:
    return {
        "database_connect": snapshot.database_connect,
        "database_create": snapshot.database_create,
        "missing_required": list(snapshot.missing_required),
        "owned_object_count": snapshot.owned_object_count,
        "prohibited_present": list(snapshot.prohibited_present),
        "schema_create": snapshot.schema_create,
        "schema_usage": snapshot.schema_usage,
    }


def _current_user(connection: Any) -> str:
    return str(connection.execute("SELECT current_user").fetchone()[0])


def run_schema_inspection(
    config: JobConfig,
    connection_factory: Callable[[], Any],
    administrator_connection_factory: Callable[[], Any],
    migration_root: Path,
) -> dict[str, Any]:
    """Inspect the database as the temporary admin without mutating it."""

    sources = load_migration_sources(migration_root)
    with administrator_connection_factory() as administrator_connection:
        if _current_user(administrator_connection) != BOOTSTRAP_PRINCIPAL_NAME:
            raise IdentityMismatch("bootstrap connection identity is unexpected")
        verify_bootstrap_administrator(administrator_connection, config)
    with connection_factory() as connection:
        if _current_user(connection) != BOOTSTRAP_PRINCIPAL_NAME:
            raise IdentityMismatch("bootstrap connection identity is unexpected")
        snapshot = inspect_schema(connection)
        classification = classify_schema(snapshot)
        planned = plan_migrations(snapshot)
        connection.rollback()
    return {
        "classification": BootstrapClassification.PASS_SCHEMA_INSPECTION,
        "database": DATABASE_NAME,
        "migration_hashes": {
            source.definition.version: source.definition.checksum_sha256 for source in sources
        },
        "migration_state": {
            migration.version: snapshot.state(migration).value for migration in MIGRATIONS
        },
        "planned_migrations": [migration.version for migration in planned],
        "read_only": True,
        "schema_classification": classification,
        "schema_constraints_verified": True,
        "token_acquired": True,
        "token_memory_only": True,
        "tls_required": True,
    }


def run_bootstrap_admin(
    config: JobConfig,
    connection_factory: Callable[[], Any],
    administrator_connection_factory: Callable[[], Any],
    migration_root: Path,
) -> dict[str, Any]:
    if config.runtime_identity_object_id is None:
        raise IdentityMismatch("runtime identity object ID is required")
    sources = load_migration_sources(migration_root)
    with administrator_connection_factory() as administrator_connection:
        if _current_user(administrator_connection) != BOOTSTRAP_PRINCIPAL_NAME:
            raise IdentityMismatch("bootstrap connection identity is unexpected")
        verify_bootstrap_administrator(administrator_connection, config)
        principal_created = ensure_runtime_principal(
            administrator_connection,
            config.runtime_identity_object_id,
        )
    with connection_factory() as connection:
        if _current_user(connection) != BOOTSTRAP_PRINCIPAL_NAME:
            raise IdentityMismatch("bootstrap connection identity is unexpected")
        before = inspect_schema(connection)
        applied = apply_required_migrations(
            connection,
            sources,
            BOOTSTRAP_PRINCIPAL_NAME,
        )
        principal = read_entra_principal(connection, RUNTIME_PRINCIPAL_NAME)
        if principal is None:
            raise IdentityMismatch("runtime principal is absent from the application database")
        validate_runtime_principal(principal, config.runtime_identity_object_id)
        apply_runtime_grants(connection)
        privileges = read_runtime_privileges(connection)
        validate_runtime_privileges(privileges)
        after = inspect_schema(connection)
    return {
        "classification": BootstrapClassification.PASS_BOOTSTRAP_ADMIN,
        "database": DATABASE_NAME,
        "migration_before": {
            migration.version: before.state(migration).value for migration in MIGRATIONS
        },
        "migration_after": {
            migration.version: after.state(migration).value for migration in MIGRATIONS
        },
        "migration_hashes": {
            migration.version: migration.checksum_sha256 for migration in MIGRATIONS
        },
        "migrations_applied": list(applied),
        "principal_admin": False,
        "principal_created": principal_created,
        "principal_object_id_verified": True,
        "principal_type": "service",
        "runtime_principal": RUNTIME_PRINCIPAL_NAME,
        "runtime_privileges": _privilege_evidence(privileges),
        "token_acquired": True,
        "token_memory_only": True,
        "tls_required": True,
    }


def run_runtime_probe(
    config: JobConfig,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    with connection_factory() as connection:
        if _current_user(connection) != RUNTIME_PRINCIPAL_NAME:
            raise IdentityMismatch("runtime connection identity is unexpected")
        privileges = read_runtime_privileges(connection)
        validate_runtime_privileges(privileges)

    instance = _probe_instance_id(os.environ)
    store = PostgresExecutionLeaseStore(connection_factory)
    lease = store.acquire(EXECUTION_LEASE_NAME, instance, 15)
    if lease is None:
        raise DatabaseUnavailable("execution lease is currently held")
    successor: LeaseRecord | None = None
    stale_rejected = False
    try:
        renewed = store.renew(lease, 15)
        if renewed is None or renewed.fencing_token != lease.fencing_token:
            raise DatabaseUnavailable("lease renewal failed")
        lease = renewed
        store.run_fenced(lease, FencedOperation.RECONCILIATION, lambda _cursor: None)
        if not store.release(lease):
            raise DatabaseUnavailable("lease release failed")
        time.sleep(0.01)
        successor = store.acquire(EXECUTION_LEASE_NAME, f"{instance}-next"[:128], 15)
        if successor is None or successor.fencing_token <= lease.fencing_token:
            raise DatabaseUnavailable("successor lease acquisition failed")
        try:
            store.run_fenced(lease, FencedOperation.RECONCILIATION, lambda _cursor: None)
        except FencingRejected:
            stale_rejected = True
        if not stale_rejected:
            raise PrivilegeMismatch("stale fencing token was not rejected")
    finally:
        if successor is not None:
            store.release(successor)
    return {
        "classification": BootstrapClassification.PASS_RUNTIME_PROBE,
        "authorized_for_broker_execution": False,
        "database": DATABASE_NAME,
        "initial_fencing_token": lease.fencing_token,
        "lease_acquired": True,
        "lease_released": True,
        "lease_renewed": True,
        "principal_admin": False,
        "runtime_principal": RUNTIME_PRINCIPAL_NAME,
        "runtime_privileges": _privilege_evidence(privileges),
        "stale_fencing_token_rejected": stale_rejected,
        "successor_fencing_token": successor.fencing_token,
        "successor_token_strictly_newer": successor.fencing_token > lease.fencing_token,
        "token_acquired": True,
        "token_memory_only": True,
        "tls_required": True,
        "used_password": False,
        "used_sqlite_fallback": False,
    }


def write_sanitized_evidence(path: Path, evidence: Mapping[str, Any]) -> None:
    serialized = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if re.search(r"eyJ[A-Za-z0-9_-]{12,}\.", serialized):
        raise BootstrapError("evidence contains token-shaped material")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


def _probe_instance_id(environment: Mapping[str, str]) -> str:
    value = (
        environment.get("CONTAINER_APP_JOB_EXECUTION_NAME", "").strip()
        or environment.get("HOSTNAME", "").strip()
        or "runtime-probe"
    )
    value = f"probe-{value}"[:128]
    if not _INSTANCE_PATTERN.fullmatch(value):
        raise BootstrapError("runtime probe identity is invalid")
    return value


def _uuid(value: str, name: str) -> str:
    try:
        return str(UUID(value.strip()))
    except ValueError:
        raise BootstrapError(f"{name} must be a UUID") from None


def _default_migration_root() -> Path:
    return Path(__file__).resolve().parents[2] / "migrations" / "postgresql"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=("schema-inspect", "bootstrap-admin", "runtime-probe"),
    )
    parser.add_argument("--evidence", type=Path, default=Path("/tmp/db-job-evidence.json"))
    parser.add_argument("--migration-root", type=Path, default=_default_migration_root())
    arguments = parser.parse_args()
    try:
        config = JobConfig.from_environment(arguments.mode, os.environ)
        factory = ManagedIdentityConnectionFactory(config)
        if arguments.mode == "schema-inspect":
            evidence = run_schema_inspection(
                config,
                factory,
                factory.administrator_connection,
                arguments.migration_root,
            )
        elif arguments.mode == "bootstrap-admin":
            evidence = run_bootstrap_admin(
                config,
                factory,
                factory.administrator_connection,
                arguments.migration_root,
            )
        else:
            evidence = run_runtime_probe(config, factory)
        evidence["token_acquired"] = factory.token_acquired
        write_sanitized_evidence(arguments.evidence, evidence)
        print(json.dumps(evidence, sort_keys=True))
        return 0
    except BootstrapError as error:
        failure = {"classification": error.classification, "status": "fail_closed"}
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 2
    except LeaseError:
        failure = {
            "classification": BootstrapClassification.DATABASE_UNAVAILABLE,
            "status": "fail_closed",
        }
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
