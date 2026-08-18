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
from typing import Any, TextIO
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
REMEDIATION_PRINCIPAL_NAME = "igtrdevfrc-db-remediation-identity"
GRANT_REPAIR_PRINCIPAL_NAME = "igtrdevfrc-db-grant-repair-identity"
DURABLE_OWNER_NAME = "ig_trader_schema_owner"
DATABASE_NAME = "ig_trader"
REJECT_FUNCTION_SIGNATURE = "trading.reject_append_only_mutation()"

EXPECTED_MIGRATION_HASHES = {
    "001_execution_state": "42dcbe2b47c5fed8223a4831d8c594e78c3180f454b71e15358819a9039c8800",
    "002_execution_lease_fencing": (
        "731b918b573ee232aab3fa709e7a41b5ac03e11f4f81d08458f8fcefcb16599c"
    ),
    "003_shadow_position_state": (
        "2fcd75c532e05a5bf3639b6667a66432261a732332e18e84e08af862033cc421"
    ),
}

_POSTGRES_HOST_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\.postgres\.database\.azure\.com\Z"
)
_INSTANCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_EXECUTION_NONCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}\Z")
_EVIDENCE_PREFIX = "G4B_EVIDENCE="
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
    PASS_OWNERSHIP_INSPECTION = "PASS_OWNERSHIP_INSPECTION"
    PASS_OWNERSHIP_REMEDIATION = "PASS_OWNERSHIP_REMEDIATION"
    PASS_PRIVILEGE_AUDIT = "PASS_PRIVILEGE_AUDIT"
    PASS_PRIVILEGE_REPAIR = "PASS_PRIVILEGE_REPAIR"
    PASS_PRIVILEGE_DRIFT_REPAIR = "PASS_PRIVILEGE_DRIFT_REPAIR"
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


class OwnershipTransferFailure(BootstrapError):
    classification = "OWNERSHIP_TRANSFER_FAILURE"


class RuntimePrivilegeDrift(BootstrapError):
    classification = "RUNTIME_DB_PRIVILEGE_DRIFT"


class UnexpectedRuntimePrivilegeDrift(BootstrapError):
    classification = "RUNTIME_DB_PRIVILEGE_DRIFT_UNEXPECTED"


class MigrationState(StrEnum):
    ABSENT = "ABSENT"
    COMPLETE = "COMPLETE"
    PARTIAL_DRIFTED = "PARTIAL_DRIFTED"


class SchemaClassification(StrEnum):
    """Accepted pre-bootstrap database states."""

    BLANK = "BLANK"
    MIGRATION_001_COMPLETE_ONLY = "001_COMPLETE_ONLY"
    MIGRATIONS_001_AND_002_COMPLETE_003_ABSENT = "001_AND_002_COMPLETE_003_ABSENT"
    MIGRATIONS_001_TO_003_COMPLETE = "001_TO_003_COMPLETE"


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

MIGRATION_003 = MigrationDefinition(
    version="003_shadow_position_state",
    filename="003_shadow_position_state.sql",
    checksum_sha256=EXPECTED_MIGRATION_HASHES["003_shadow_position_state"],
    required_markers=frozenset(
        {
            "relation:shadow_position_state",
            "constraint-count:shadow_position_state:c:11",
            "constraint-count:shadow_position_state:f:1",
            "constraint-count:shadow_position_state:p:1",
            "constraint-count:shadow_position_state:u:1",
            "column:shadow_position_state.intent_id:not-null",
            "column:shadow_position_state.fencing_token:not-null",
            "trigger:shadow_position_state_require_fence",
        }
    ),
)

MIGRATIONS = (MIGRATION_001, MIGRATION_002, MIGRATION_003)
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
class RoleRecord:
    role_name: str
    can_login: bool
    is_superuser: bool
    can_create_role: bool
    can_create_database: bool
    can_replicate: bool
    bypasses_rls: bool
    azure_pg_admin_member: bool


@dataclass(frozen=True)
class OwnershipInventory:
    database_owners: tuple[tuple[str, str], ...]
    schema_owners: tuple[tuple[str, str], ...]
    relation_owners: tuple[tuple[str, str, str], ...]
    function_owners: tuple[tuple[str, str], ...]
    migration_ledger: tuple[tuple[str, str], ...]
    orphan_acl_count: int
    orphan_default_acl_count: int
    orphan_memberships: tuple[tuple[str, str], ...]
    orphan_dependency_count: int


@dataclass(frozen=True)
class RuntimePrivilegeAudit:
    database_connect: bool
    database_create: bool
    schema_usage: bool
    schema_create: bool
    table_privileges: tuple[tuple[str, tuple[str, ...]], ...]
    sequence_privileges: tuple[tuple[str, tuple[str, ...]], ...]
    function_execute: tuple[str, ...]
    role_memberships: tuple[str, ...]
    owned_object_count: int
    missing_required: tuple[str, ...]
    prohibited_present: tuple[str, ...]


@dataclass(frozen=True)
class FunctionPrivilegeProvenance:
    function_owner: str
    function_acl: str | None
    direct_runtime_execute: bool
    public_execute: bool
    role_execute_sources: tuple[str, ...]
    runtime_is_owner: bool
    runtime_effective_execute: bool
    durable_owner_future_public_execute: bool
    classification: str


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
    orphan_identity_object_id: str | None

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
            orphan_object_id = None
        elif mode in {"ownership-inspect", "ownership-remediate"}:
            if identity_name != REMEDIATION_PRINCIPAL_NAME:
                raise IdentityMismatch("remediation identity is required")
            runtime_object_id = _uuid(
                environment.get("RUNTIME_UAMI_OBJECT_ID", ""),
                "RUNTIME_UAMI_OBJECT_ID",
            )
            if runtime_object_id == identity_object_id:
                raise IdentityMismatch("remediation and runtime identities must be distinct")
            orphan_object_id = _uuid(
                environment.get("ORPHAN_UAMI_OBJECT_ID", ""),
                "ORPHAN_UAMI_OBJECT_ID",
            )
            database_user = REMEDIATION_PRINCIPAL_NAME
        elif mode in {
            "privilege-audit",
            "privilege-repair",
            "privilege-drift-repair",
        }:
            if identity_name != GRANT_REPAIR_PRINCIPAL_NAME:
                raise IdentityMismatch("grant-repair identity is required")
            runtime_object_id = _uuid(
                environment.get("RUNTIME_UAMI_OBJECT_ID", ""),
                "RUNTIME_UAMI_OBJECT_ID",
            )
            if runtime_object_id == identity_object_id:
                raise IdentityMismatch("grant-repair and runtime identities must be distinct")
            orphan_object_id = None
            database_user = GRANT_REPAIR_PRINCIPAL_NAME
        elif mode == "runtime-probe":
            if identity_name != RUNTIME_PRINCIPAL_NAME:
                raise IdentityMismatch("runtime identity is required")
            runtime_object_id = None
            orphan_object_id = None
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
            orphan_identity_object_id=orphan_object_id,
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
    for table, column in (
        ("shadow_position_state", "intent_id"),
        ("shadow_position_state", "fencing_token"),
    ):
        row = connection.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'trading' AND table_name = %s AND column_name = %s",
            (table, column),
        ).fetchone()
        if row and row[0] == "NO":
            markers.add(f"column:{table}.{column}:not-null")

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
    third = states[MIGRATION_003.version]
    if first is MigrationState.ABSENT and second is not MigrationState.ABSENT:
        raise DatabaseSchemaDrift("migration ordering is invalid")
    if second is MigrationState.ABSENT and third is not MigrationState.ABSENT:
        raise DatabaseSchemaDrift("migration ordering is invalid")
    if first is MigrationState.ABSENT:
        return MIGRATIONS
    if second is MigrationState.ABSENT:
        return (MIGRATION_002, MIGRATION_003)
    if third is MigrationState.ABSENT:
        return (MIGRATION_003,)
    return ()


def classify_schema(snapshot: SchemaSnapshot) -> SchemaClassification:
    """Classify a verified schema without applying migrations or grants."""

    planned = plan_migrations(snapshot)
    if planned == MIGRATIONS:
        return SchemaClassification.BLANK
    if planned == (MIGRATION_002, MIGRATION_003):
        return SchemaClassification.MIGRATION_001_COMPLETE_ONLY
    if planned == (MIGRATION_003,):
        return SchemaClassification.MIGRATIONS_001_AND_002_COMPLETE_003_ABSENT
    if not planned:
        return SchemaClassification.MIGRATIONS_001_TO_003_COMPLETE
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


def read_role_record(connection: Any, role_name: str) -> RoleRecord | None:
    row = connection.execute(
        """
        SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
               rolreplication, rolbypassrls,
               pg_has_role(rolname, 'azure_pg_admin', 'MEMBER')
        FROM pg_roles
        WHERE rolname = %s
        """,
        (role_name,),
    ).fetchone()
    if row is None:
        return None
    return RoleRecord(
        role_name=str(row[0]),
        can_login=bool(row[1]),
        is_superuser=bool(row[2]),
        can_create_role=bool(row[3]),
        can_create_database=bool(row[4]),
        can_replicate=bool(row[5]),
        bypasses_rls=bool(row[6]),
        azure_pg_admin_member=bool(row[7]),
    )


def validate_durable_owner(record: RoleRecord | None) -> None:
    if (
        record is None
        or record.role_name != DURABLE_OWNER_NAME
        or record.can_login
        or record.is_superuser
        or record.can_create_role
        or record.can_create_database
        or record.can_replicate
        or record.bypasses_rls
        or record.azure_pg_admin_member
    ):
        raise OwnershipTransferFailure("durable owner role attributes are unsafe")


def _shadow_position_owner(connection: Any) -> str | None:
    row = connection.execute(
        """
        SELECT pg_get_userbyid(c.relowner)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'trading' AND c.relname = 'shadow_position_state'
        """
    ).fetchone()
    return None if row is None else str(row[0])


def _shadow_owner_membership_exists(connection: Any) -> bool:
    return bool(
        connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_auth_members membership
                JOIN pg_roles parent ON parent.oid = membership.roleid
                JOIN pg_roles member ON member.oid = membership.member
                WHERE parent.rolname = %s AND member.rolname = %s
            )
            """,
            (DURABLE_OWNER_NAME, BOOTSTRAP_PRINCIPAL_NAME),
        ).fetchone()[0]
    )


def _transfer_shadow_position_owner(connection: Any) -> None:
    owner = _shadow_position_owner(connection)
    if owner is None:
        raise OwnershipTransferFailure("shadow position state table is absent")
    if owner not in {BOOTSTRAP_PRINCIPAL_NAME, DURABLE_OWNER_NAME}:
        raise OwnershipTransferFailure("shadow position state has an unexpected owner")
    if owner == BOOTSTRAP_PRINCIPAL_NAME:
        try:
            connection.execute(
                f"ALTER TABLE trading.shadow_position_state OWNER TO {DURABLE_OWNER_NAME}"
            )
        except Exception:
            raise OwnershipTransferFailure("shadow position ownership transfer failed") from None
    if _shadow_position_owner(connection) != DURABLE_OWNER_NAME:
        raise OwnershipTransferFailure("shadow position owner verification failed")


def _entra_principal_names(connection: Any, admin_only: bool) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT * FROM pg_catalog.pgaadauth_list_principals(%s)",
        (admin_only,),
    ).fetchall()
    return tuple(sorted(str(row[0]) for row in rows))


def verify_remediation_administrator(connection: Any, config: JobConfig) -> None:
    remediation = read_entra_principal(connection, REMEDIATION_PRINCIPAL_NAME)
    orphan = read_entra_principal(connection, BOOTSTRAP_PRINCIPAL_NAME)
    runtime = read_entra_principal(connection, RUNTIME_PRINCIPAL_NAME)
    if (
        remediation is None
        or remediation.object_id != config.job_identity_object_id.casefold()
        or remediation.principal_type != "service"
        or not remediation.is_admin
        or not remediation.azure_pg_admin_member
    ):
        raise IdentityMismatch("temporary remediation identity is not the current Entra admin")
    if (
        orphan is None
        or config.orphan_identity_object_id is None
        or orphan.object_id != config.orphan_identity_object_id.casefold()
        or orphan.principal_type != "service"
        or not orphan.is_admin
        or not orphan.azure_pg_admin_member
    ):
        raise IdentityMismatch("orphan bootstrap administrator identity is not exact")
    if runtime is None or config.runtime_identity_object_id is None:
        raise IdentityMismatch("runtime database principal is absent")
    validate_runtime_principal(runtime, config.runtime_identity_object_id)


def inspect_ownership(connection: Any) -> OwnershipInventory:
    database_owners = tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            """
            SELECT datname, pg_get_userbyid(datdba)
            FROM pg_database
            WHERE datname IN ('postgres', %s)
            ORDER BY datname
            """,
            (DATABASE_NAME,),
        ).fetchall()
    )
    schema_owners = tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            """
            SELECT nspname, pg_get_userbyid(nspowner)
            FROM pg_namespace
            WHERE nspname = 'trading'
            ORDER BY nspname
            """
        ).fetchall()
    )
    relation_owners = tuple(
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            """
            SELECT c.relname, c.relkind, pg_get_userbyid(c.relowner)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'trading'
              AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
            ORDER BY c.relkind, c.relname
            """
        ).fetchall()
    )
    function_owners = tuple(
        (f"{row[0]}({row[1]})", str(row[2]))
        for row in connection.execute(
            """
            SELECT p.proname, pg_get_function_identity_arguments(p.oid),
                   pg_get_userbyid(p.proowner)
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'trading'
            ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)
            """
        ).fetchall()
    )
    migration_ledger = tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT version, checksum_sha256 FROM trading.schema_migrations ORDER BY version"
        ).fetchall()
    )
    acl_count = int(
        connection.execute(
            """
            WITH target AS (SELECT oid FROM pg_roles WHERE rolname = %s), acl_rows AS (
                SELECT x.grantee, x.grantor FROM pg_class c
                CROSS JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) x
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'trading'
                UNION ALL
                SELECT x.grantee, x.grantor FROM pg_namespace n
                CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) x
                WHERE n.nspname = 'trading'
                UNION ALL
                SELECT x.grantee, x.grantor FROM pg_proc p
                CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'trading'
                UNION ALL
                SELECT x.grantee, x.grantor FROM pg_database d
                CROSS JOIN LATERAL aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) x
                WHERE d.datname = %s
            )
            SELECT count(*) FROM acl_rows, target
            WHERE grantee = target.oid OR grantor = target.oid
            """,
            (BOOTSTRAP_PRINCIPAL_NAME, DATABASE_NAME),
        ).fetchone()[0]
    )
    default_acl_count = int(
        connection.execute(
            """
            WITH target AS (SELECT oid FROM pg_roles WHERE rolname = %s)
            SELECT count(*)
            FROM pg_default_acl d
            CROSS JOIN target
            LEFT JOIN LATERAL aclexplode(d.defaclacl) x ON true
            WHERE d.defaclrole = target.oid
               OR x.grantee = target.oid
               OR x.grantor = target.oid
            """,
            (BOOTSTRAP_PRINCIPAL_NAME,),
        ).fetchone()[0]
    )
    memberships = tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            """
            SELECT parent.rolname, member.rolname
            FROM pg_auth_members m
            JOIN pg_roles parent ON parent.oid = m.roleid
            JOIN pg_roles member ON member.oid = m.member
            WHERE parent.rolname = %s OR member.rolname = %s
            ORDER BY parent.rolname, member.rolname
            """,
            (BOOTSTRAP_PRINCIPAL_NAME, BOOTSTRAP_PRINCIPAL_NAME),
        ).fetchall()
    )
    owned_count = (
        sum(owner == BOOTSTRAP_PRINCIPAL_NAME for _, owner in database_owners + schema_owners)
        + sum(owner == BOOTSTRAP_PRINCIPAL_NAME for _, _, owner in relation_owners)
        + sum(owner == BOOTSTRAP_PRINCIPAL_NAME for _, owner in function_owners)
    )
    non_admin_memberships = tuple(
        item for item in memberships if item != ("azure_pg_admin", BOOTSTRAP_PRINCIPAL_NAME)
    )
    return OwnershipInventory(
        database_owners=database_owners,
        schema_owners=schema_owners,
        relation_owners=relation_owners,
        function_owners=function_owners,
        migration_ledger=migration_ledger,
        orphan_acl_count=acl_count,
        orphan_default_acl_count=default_acl_count,
        orphan_memberships=memberships,
        orphan_dependency_count=(
            owned_count + acl_count + default_acl_count + len(non_admin_memberships)
        ),
    )


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
    "REVOKE EXECUTE ON FUNCTION trading.reject_append_only_mutation() FROM PUBLIC",
    (
        "GRANT SELECT ON trading.schema_migrations, trading.worker_leases, "
        "trading.execution_cycle_claims, trading.trade_intents, "
        "trading.lifecycle_events, trading.broker_references, trading.position_state, "
        "trading.shadow_position_state, "
        f"trading.reconciliation_state, trading.evidence_metadata TO {_ROLE}"
    ),
    f"GRANT INSERT, UPDATE ON trading.execution_cycle_claims TO {_ROLE}",
    f"GRANT INSERT, UPDATE ON trading.trade_intents TO {_ROLE}",
    f"GRANT INSERT ON trading.lifecycle_events TO {_ROLE}",
    f"GRANT INSERT ON trading.broker_references TO {_ROLE}",
    f"GRANT INSERT, UPDATE ON trading.position_state TO {_ROLE}",
    f"GRANT INSERT, UPDATE ON trading.shadow_position_state TO {_ROLE}",
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
    "shadow_position_state": {"SELECT", "INSERT", "UPDATE"},
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


def read_exact_runtime_privileges(connection: Any) -> RuntimePrivilegeAudit:
    """Audit effective privileges by object OID, even when schema USAGE is absent."""

    role = RUNTIME_PRINCIPAL_NAME
    record = read_role_record(connection, role)
    if record is None:
        raise IdentityMismatch("runtime PostgreSQL role is absent")
    database_connect, database_create = connection.execute(
        "SELECT has_database_privilege(%s, current_database(), 'CONNECT'), "
        "has_database_privilege(%s, current_database(), 'CREATE')",
        (role, role),
    ).fetchone()
    schema_row = connection.execute(
        """
        SELECT has_schema_privilege(%s, oid, 'USAGE'),
               has_schema_privilege(%s, oid, 'CREATE')
        FROM pg_namespace
        WHERE nspname = 'trading'
        """,
        (role, role),
    ).fetchone()
    if schema_row is None:
        raise DatabaseSchemaDrift("trading schema is absent")
    schema_usage, schema_create = schema_row
    table_rows = connection.execute(
        """
        SELECT c.relname, c.oid
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'trading' AND c.relkind IN ('r', 'p')
        ORDER BY c.relname
        """
    ).fetchall()
    table_oids = {str(row[0]): int(row[1]) for row in table_rows}
    if set(table_oids) != set(_TABLE_PRIVILEGES):
        raise DatabaseSchemaDrift("runtime table footprint differs from review")
    table_privileges: list[tuple[str, tuple[str, ...]]] = []
    missing: list[str] = []
    prohibited: list[str] = []
    for table, oid in sorted(table_oids.items()):
        actual: list[str] = []
        required = _TABLE_PRIVILEGES[table]
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            present = bool(
                connection.execute(
                    "SELECT has_table_privilege(%s, %s::oid, %s)",
                    (role, oid, privilege),
                ).fetchone()[0]
            )
            label = f"trading.{table}:{privilege}"
            if present:
                actual.append(privilege)
            if privilege in required and not present:
                missing.append(label)
            if privilege not in required and present:
                prohibited.append(label)
        table_privileges.append((table, tuple(actual)))
    sequence_rows = connection.execute(
        """
        SELECT c.relname, c.oid
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'trading' AND c.relkind = 'S'
        ORDER BY c.relname
        """
    ).fetchall()
    required_sequence = "lifecycle_events_sequence_seq"
    sequence_privileges: list[tuple[str, tuple[str, ...]]] = []
    for row in sequence_rows:
        name, oid = str(row[0]), int(row[1])
        actual = []
        for privilege in ("USAGE", "SELECT", "UPDATE"):
            present = bool(
                connection.execute(
                    "SELECT has_sequence_privilege(%s, %s::oid, %s)",
                    (role, oid, privilege),
                ).fetchone()[0]
            )
            if present:
                actual.append(privilege)
            label = f"trading.{name}:{privilege}"
            if name == required_sequence and privilege in {"USAGE", "SELECT"} and not present:
                missing.append(label)
            if present and (name != required_sequence or privilege == "UPDATE"):
                prohibited.append(label)
        sequence_privileges.append((name, tuple(actual)))
    approved_function_oids: set[int] = set()
    function_execute: list[str] = []
    for signature in _FUNCTION_SIGNATURES:
        row = connection.execute("SELECT to_regprocedure(%s)::oid", (signature,)).fetchone()
        if row is None or row[0] is None:
            raise DatabaseSchemaDrift("approved lease function is absent")
        oid = int(row[0])
        approved_function_oids.add(oid)
        if connection.execute(
            "SELECT has_function_privilege(%s, %s::oid, 'EXECUTE')",
            (role, oid),
        ).fetchone()[0]:
            function_execute.append(signature)
        else:
            missing.append(f"{signature}:EXECUTE")
    for row in connection.execute(
        """
        SELECT p.oid, p.proname, pg_get_function_identity_arguments(p.oid)
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'trading'
        ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)
        """
    ).fetchall():
        oid = int(row[0])
        if oid in approved_function_oids:
            continue
        if connection.execute(
            "SELECT has_function_privilege(%s, %s::oid, 'EXECUTE')",
            (role, oid),
        ).fetchone()[0]:
            prohibited.append(f"trading.{row[1]}({row[2]}):EXECUTE")
    memberships = tuple(
        str(row[0])
        for row in connection.execute(
            """
            SELECT parent.rolname
            FROM pg_auth_members m
            JOIN pg_roles parent ON parent.oid = m.roleid
            JOIN pg_roles member ON member.oid = m.member
            WHERE member.rolname = %s
            ORDER BY parent.rolname
            """,
            (role,),
        ).fetchall()
    )
    owned_count = int(
        connection.execute(
            """
            SELECT
                (SELECT count(*) FROM pg_database
                 WHERE datdba = (SELECT oid FROM pg_roles WHERE rolname = %s))
              + (SELECT count(*) FROM pg_namespace
                 WHERE nspowner = (SELECT oid FROM pg_roles WHERE rolname = %s))
              + (SELECT count(*) FROM pg_class c JOIN pg_namespace n
                    ON n.oid = c.relnamespace
                 WHERE n.nspname = 'trading'
                   AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = %s))
              + (SELECT count(*) FROM pg_proc p JOIN pg_namespace n
                    ON n.oid = p.pronamespace
                 WHERE n.nspname = 'trading'
                   AND p.proowner = (SELECT oid FROM pg_roles WHERE rolname = %s))
            """,
            (role, role, role, role),
        ).fetchone()[0]
    )
    if not database_connect:
        missing.append("database:CONNECT")
    if not schema_usage:
        missing.append("trading:USAGE")
    if database_create:
        prohibited.append("database:CREATE")
    if schema_create:
        prohibited.append("trading:CREATE")
    if record.is_superuser:
        prohibited.append("role:SUPERUSER")
    if record.can_create_database:
        prohibited.append("role:CREATEDB")
    if record.can_create_role:
        prohibited.append("role:CREATEROLE")
    if record.azure_pg_admin_member:
        prohibited.append("role:azure_pg_admin")
    if memberships:
        prohibited.extend(f"role-membership:{name}" for name in memberships)
    if owned_count:
        prohibited.append(f"owned-objects:{owned_count}")
    return RuntimePrivilegeAudit(
        database_connect=bool(database_connect),
        database_create=bool(database_create),
        schema_usage=bool(schema_usage),
        schema_create=bool(schema_create),
        table_privileges=tuple(table_privileges),
        sequence_privileges=tuple(sequence_privileges),
        function_execute=tuple(function_execute),
        role_memberships=memberships,
        owned_object_count=owned_count,
        missing_required=tuple(sorted(missing)),
        prohibited_present=tuple(sorted(prohibited)),
    )


def apply_runtime_grants(connection: Any) -> None:
    for statement in GRANT_STATEMENTS:
        connection.execute(statement)
    connection.commit()


def verify_grant_repair_administrator(connection: Any, config: JobConfig) -> None:
    repair = read_entra_principal(connection, GRANT_REPAIR_PRINCIPAL_NAME)
    runtime = read_entra_principal(connection, RUNTIME_PRINCIPAL_NAME)
    if (
        repair is None
        or repair.object_id != config.job_identity_object_id.casefold()
        or repair.principal_type != "service"
        or not repair.is_admin
        or not repair.azure_pg_admin_member
    ):
        raise IdentityMismatch("temporary grant-repair identity is not the Entra admin")
    if runtime is None or config.runtime_identity_object_id is None:
        raise IdentityMismatch("runtime database principal is absent")
    validate_runtime_principal(runtime, config.runtime_identity_object_id)
    validate_durable_owner(read_role_record(connection, DURABLE_OWNER_NAME))
    if read_entra_principal(connection, DURABLE_OWNER_NAME) is not None:
        raise IdentityMismatch("durable owner must not have an Entra mapping")


def _runtime_privilege_evidence(audit: RuntimePrivilegeAudit) -> dict[str, Any]:
    return {
        "actual": {
            "database_connect": audit.database_connect,
            "database_create": audit.database_create,
            "function_execute": list(audit.function_execute),
            "owned_object_count": audit.owned_object_count,
            "role_memberships": list(audit.role_memberships),
            "schema_create": audit.schema_create,
            "schema_usage": audit.schema_usage,
            "sequence_privileges": {
                name: list(privileges) for name, privileges in audit.sequence_privileges
            },
            "table_privileges": {
                name: list(privileges) for name, privileges in audit.table_privileges
            },
        },
        "expected": {
            "database": ["CONNECT"],
            "functions": [f"{signature}:EXECUTE" for signature in _FUNCTION_SIGNATURES],
            "role_memberships": [],
            "schema": ["USAGE"],
            "sequence_privileges": {"lifecycle_events_sequence_seq": ["SELECT", "USAGE"]},
            "table_privileges": {
                name: sorted(privileges) for name, privileges in _TABLE_PRIVILEGES.items()
            },
        },
        "excess": list(audit.prohibited_present),
        "missing": list(audit.missing_required),
    }


def read_function_provenance(connection: Any, signature: str) -> FunctionPrivilegeProvenance:
    row = connection.execute(
        """
        SELECT owner_role.rolname,
               p.proacl::text,
               EXISTS (
                   SELECT 1
                   FROM aclexplode(
                       COALESCE(p.proacl, acldefault('f', p.proowner))
                   ) acl
                   JOIN pg_roles grantee ON grantee.oid = acl.grantee
                   WHERE grantee.rolname = %s AND acl.privilege_type = 'EXECUTE'
               ),
               EXISTS (
                   SELECT 1
                   FROM aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) acl
                   WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
               ),
               p.proowner = (SELECT oid FROM pg_roles WHERE rolname = %s),
               has_function_privilege(%s, p.oid, 'EXECUTE')
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_roles owner_role ON owner_role.oid = p.proowner
        WHERE p.oid = to_regprocedure(%s)
        """,
        (
            RUNTIME_PRINCIPAL_NAME,
            RUNTIME_PRINCIPAL_NAME,
            RUNTIME_PRINCIPAL_NAME,
            signature,
        ),
    ).fetchone()
    if row is None:
        raise DatabaseSchemaDrift("reviewed function is absent")
    role_sources = tuple(
        str(item[0])
        for item in connection.execute(
            """
            SELECT DISTINCT grantee.rolname
            FROM pg_proc p
            CROSS JOIN LATERAL aclexplode(
                COALESCE(p.proacl, acldefault('f', p.proowner))
            ) acl
            JOIN pg_roles grantee ON grantee.oid = acl.grantee
            WHERE p.oid = to_regprocedure(%s)
              AND acl.privilege_type = 'EXECUTE'
              AND grantee.rolname <> %s
              AND pg_has_role(%s, grantee.oid, 'USAGE')
            ORDER BY grantee.rolname
            """,
            (
                signature,
                RUNTIME_PRINCIPAL_NAME,
                RUNTIME_PRINCIPAL_NAME,
            ),
        ).fetchall()
    )
    future_public = bool(
        connection.execute(
            """
            WITH owner_role AS (
                SELECT oid FROM pg_roles WHERE rolname = %s
            ), global_acl AS (
                SELECT COALESCE(
                    (SELECT d.defaclacl
                     FROM pg_default_acl d, owner_role o
                     WHERE d.defaclrole = o.oid
                       AND d.defaclobjtype = 'f'
                       AND d.defaclnamespace = 0),
                    acldefault('f', (SELECT oid FROM owner_role))
                ) AS acl
            )
            SELECT EXISTS (
                SELECT 1 FROM global_acl g, LATERAL aclexplode(g.acl) a
                WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'
            ) OR EXISTS (
                SELECT 1
                FROM pg_default_acl d
                JOIN owner_role o ON o.oid = d.defaclrole
                CROSS JOIN LATERAL aclexplode(d.defaclacl) a
                WHERE d.defaclobjtype = 'f'
                  AND d.defaclnamespace <> 0
                  AND a.grantee = 0
                  AND a.privilege_type = 'EXECUTE'
            )
            """,
            (DURABLE_OWNER_NAME,),
        ).fetchone()[0]
    )
    direct_runtime = bool(row[2])
    public_execute = bool(row[3])
    runtime_is_owner = bool(row[4])
    runtime_effective = bool(row[5])
    if direct_runtime:
        classification = "DIRECT_GRANT"
    elif public_execute:
        classification = "PUBLIC_GRANT"
    elif role_sources:
        classification = "ROLE_INHERITANCE"
    elif runtime_is_owner:
        classification = "OWNER_PRIVILEGE"
    elif runtime_effective:
        classification = "OTHER"
    else:
        classification = "NONE"
    return FunctionPrivilegeProvenance(
        function_owner=str(row[0]),
        function_acl=None if row[1] is None else str(row[1]),
        direct_runtime_execute=direct_runtime,
        public_execute=public_execute,
        role_execute_sources=role_sources,
        runtime_is_owner=runtime_is_owner,
        runtime_effective_execute=runtime_effective,
        durable_owner_future_public_execute=future_public,
        classification=classification,
    )


def read_reject_function_provenance(connection: Any) -> FunctionPrivilegeProvenance:
    return read_function_provenance(connection, REJECT_FUNCTION_SIGNATURE)


def _function_provenance_evidence(
    provenance: FunctionPrivilegeProvenance,
) -> dict[str, Any]:
    return {
        "classification": provenance.classification,
        "direct_runtime_execute": provenance.direct_runtime_execute,
        "durable_owner_future_public_execute": (provenance.durable_owner_future_public_execute),
        "function_acl": provenance.function_acl,
        "function_owner": provenance.function_owner,
        "public_execute": provenance.public_execute,
        "role_execute_sources": list(provenance.role_execute_sources),
        "runtime_effective_execute": provenance.runtime_effective_execute,
        "runtime_is_owner": provenance.runtime_is_owner,
    }


def _grant_missing_runtime_privileges(connection: Any, audit: RuntimePrivilegeAudit) -> None:
    missing = set(audit.missing_required)
    if "database:CONNECT" in missing:
        connection.execute(f"GRANT CONNECT ON DATABASE {DATABASE_NAME} TO {_ROLE}")
        missing.remove("database:CONNECT")
    connection.execute(f'GRANT {DURABLE_OWNER_NAME} TO "{GRANT_REPAIR_PRINCIPAL_NAME}"')
    connection.execute(f"SET ROLE {DURABLE_OWNER_NAME}")
    try:
        if "trading:USAGE" in missing:
            connection.execute(f"GRANT USAGE ON SCHEMA trading TO {_ROLE}")
            missing.remove("trading:USAGE")
        for table, required in _TABLE_PRIVILEGES.items():
            for privilege in sorted(required):
                label = f"trading.{table}:{privilege}"
                if label in missing:
                    connection.execute(f"GRANT {privilege} ON TABLE trading.{table} TO {_ROLE}")
                    missing.remove(label)
        sequence = "lifecycle_events_sequence_seq"
        for privilege in ("SELECT", "USAGE"):
            label = f"trading.{sequence}:{privilege}"
            if label in missing:
                connection.execute(f"GRANT {privilege} ON SEQUENCE trading.{sequence} TO {_ROLE}")
                missing.remove(label)
        for signature in _FUNCTION_SIGNATURES:
            label = f"{signature}:EXECUTE"
            if label in missing:
                connection.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_ROLE}")
                missing.remove(label)
    finally:
        connection.execute("RESET ROLE")
    if missing:
        raise PrivilegeMismatch("missing privilege set contains an unsupported grant")


def run_privilege_audit(
    config: JobConfig,
    connection_factory: Callable[[], Any],
    administrator_connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    with administrator_connection_factory() as administrator_connection:
        if _current_user(administrator_connection) != GRANT_REPAIR_PRINCIPAL_NAME:
            raise IdentityMismatch("grant-repair connection identity is unexpected")
        verify_grant_repair_administrator(administrator_connection, config)
    with connection_factory() as connection:
        if _current_user(connection) != GRANT_REPAIR_PRINCIPAL_NAME:
            raise IdentityMismatch("grant-repair connection identity is unexpected")
        connection_tls = _connection_uses_tls(connection)
        audit = read_exact_runtime_privileges(connection)
        provenance = read_reject_function_provenance(connection)
        connection.rollback()
    return {
        "classification": BootstrapClassification.PASS_PRIVILEGE_AUDIT,
        "connection_tls": connection_tls,
        "database": DATABASE_NAME,
        "event": "runtime_privilege_audit",
        "mode": "read_only",
        "principal_admin": False,
        "principal_owner": False,
        "read_only": True,
        "reject_function_provenance": _function_provenance_evidence(provenance),
        "runtime_principal": RUNTIME_PRINCIPAL_NAME,
        "runtime_privileges": _runtime_privilege_evidence(audit),
        "token_acquired": True,
        "token_memory_only": True,
        "tls_required": True,
    }


def run_privilege_repair(
    config: JobConfig,
    connection_factory: Callable[[], Any],
    administrator_connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    with administrator_connection_factory() as administrator_connection:
        if _current_user(administrator_connection) != GRANT_REPAIR_PRINCIPAL_NAME:
            raise IdentityMismatch("grant-repair connection identity is unexpected")
        verify_grant_repair_administrator(administrator_connection, config)
    before: RuntimePrivilegeAudit
    membership_granted = False
    try:
        with connection_factory() as connection:
            if _current_user(connection) != GRANT_REPAIR_PRINCIPAL_NAME:
                raise IdentityMismatch("grant-repair connection identity is unexpected")
            connection_tls = _connection_uses_tls(connection)
            before = read_exact_runtime_privileges(connection)
            if before.prohibited_present:
                raise RuntimePrivilegeDrift("runtime role has prohibited effective privileges")
            _grant_missing_runtime_privileges(connection, before)
            connection.commit()
            membership_granted = True
    finally:
        if membership_granted:
            with administrator_connection_factory() as administrator_connection:
                administrator_connection.execute(
                    f'REVOKE {DURABLE_OWNER_NAME} FROM "{GRANT_REPAIR_PRINCIPAL_NAME}"'
                )
                administrator_connection.commit()
    with administrator_connection_factory() as administrator_connection:
        membership_present = bool(
            administrator_connection.execute(
                "SELECT pg_has_role(%s, %s, 'MEMBER')",
                (GRANT_REPAIR_PRINCIPAL_NAME, DURABLE_OWNER_NAME),
            ).fetchone()[0]
        )
        administrator_connection.rollback()
    if membership_present:
        raise PrivilegeMismatch("temporary durable-owner membership remains")
    with connection_factory() as connection:
        after = read_exact_runtime_privileges(connection)
        connection.rollback()
    if after.missing_required or after.prohibited_present:
        raise PrivilegeMismatch("runtime privileges do not match the accepted final set")
    return {
        "classification": BootstrapClassification.PASS_PRIVILEGE_REPAIR,
        "connection_tls": connection_tls,
        "database": DATABASE_NAME,
        "event": "runtime_privilege_repair",
        "grants_restored": list(before.missing_required),
        "mode": "grant_only_missing",
        "owner_membership_removed": True,
        "runtime_principal": RUNTIME_PRINCIPAL_NAME,
        "runtime_privileges_after": _runtime_privilege_evidence(after),
        "runtime_privileges_before": _runtime_privilege_evidence(before),
        "token_acquired": True,
        "token_memory_only": True,
        "tls_required": True,
    }


def run_privilege_drift_repair(
    config: JobConfig,
    connection_factory: Callable[[], Any],
    administrator_connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    with administrator_connection_factory() as administrator_connection:
        if _current_user(administrator_connection) != GRANT_REPAIR_PRINCIPAL_NAME:
            raise IdentityMismatch("grant-repair connection identity is unexpected")
        verify_grant_repair_administrator(administrator_connection, config)
    known_excess = f"{REJECT_FUNCTION_SIGNATURE}:EXECUTE"
    membership_granted = False
    before: RuntimePrivilegeAudit
    provenance_before: FunctionPrivilegeProvenance
    try:
        with connection_factory() as connection:
            if _current_user(connection) != GRANT_REPAIR_PRINCIPAL_NAME:
                raise IdentityMismatch("grant-repair connection identity is unexpected")
            connection_tls = _connection_uses_tls(connection)
            before = read_exact_runtime_privileges(connection)
            provenance_before = read_reject_function_provenance(connection)
            if before.prohibited_present != (known_excess,):
                raise UnexpectedRuntimePrivilegeDrift(
                    "runtime privilege excess differs from approved repair"
                )
            if (
                provenance_before.classification != "PUBLIC_GRANT"
                or provenance_before.direct_runtime_execute
                or provenance_before.role_execute_sources
                or provenance_before.runtime_is_owner
            ):
                raise UnexpectedRuntimePrivilegeDrift(
                    "append-only function privilege source is unexpected"
                )
            if len(before.missing_required) != 28:
                raise UnexpectedRuntimePrivilegeDrift(
                    "missing runtime privilege set differs from approved repair"
                )
            connection.execute(f'GRANT {DURABLE_OWNER_NAME} TO "{GRANT_REPAIR_PRINCIPAL_NAME}"')
            membership_granted = True
            connection.execute(f"SET ROLE {DURABLE_OWNER_NAME}")
            try:
                connection.execute(
                    f"REVOKE EXECUTE ON FUNCTION {REJECT_FUNCTION_SIGNATURE} FROM PUBLIC"
                )
                if provenance_before.durable_owner_future_public_execute:
                    connection.execute(
                        "ALTER DEFAULT PRIVILEGES "
                        f"FOR ROLE {DURABLE_OWNER_NAME} "
                        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
                    )
            finally:
                connection.execute("RESET ROLE")
            _grant_missing_runtime_privileges(connection, before)
            connection.commit()
    finally:
        if membership_granted:
            with administrator_connection_factory() as administrator_connection:
                administrator_connection.execute(
                    f'REVOKE {DURABLE_OWNER_NAME} FROM "{GRANT_REPAIR_PRINCIPAL_NAME}"'
                )
                administrator_connection.commit()
    with administrator_connection_factory() as administrator_connection:
        membership_present = bool(
            administrator_connection.execute(
                "SELECT pg_has_role(%s, %s, 'MEMBER')",
                (GRANT_REPAIR_PRINCIPAL_NAME, DURABLE_OWNER_NAME),
            ).fetchone()[0]
        )
        administrator_connection.rollback()
    if membership_present:
        raise PrivilegeMismatch("temporary durable-owner membership remains")
    with connection_factory() as connection:
        after = read_exact_runtime_privileges(connection)
        provenance_after = read_reject_function_provenance(connection)
        connection.rollback()
    if after.missing_required or after.prohibited_present:
        raise PrivilegeMismatch("runtime privileges do not match the accepted final set")
    if (
        provenance_after.runtime_effective_execute
        or provenance_after.public_execute
        or provenance_after.durable_owner_future_public_execute
    ):
        raise PrivilegeMismatch("append-only function ACL repair is incomplete")
    return {
        "classification": BootstrapClassification.PASS_PRIVILEGE_DRIFT_REPAIR,
        "connection_tls": connection_tls,
        "database": DATABASE_NAME,
        "event": "runtime_privilege_drift_repair",
        "excess_repaired": [known_excess],
        "future_public_function_default_hardened": (
            provenance_before.durable_owner_future_public_execute
        ),
        "grants_restored": list(before.missing_required),
        "mode": "known_public_acl_and_grant_only_missing",
        "owner_membership_removed": True,
        "reject_function_provenance_after": _function_provenance_evidence(provenance_after),
        "reject_function_provenance_before": _function_provenance_evidence(provenance_before),
        "runtime_principal": RUNTIME_PRINCIPAL_NAME,
        "runtime_privileges_after": _runtime_privilege_evidence(after),
        "runtime_privileges_before": _runtime_privilege_evidence(before),
        "token_acquired": True,
        "token_memory_only": True,
        "tls_required": True,
    }


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


def _connection_uses_tls(connection: Any) -> bool:
    row = connection.execute("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()").fetchone()
    if row is None or not bool(row[0]):
        raise DatabaseUnavailable("PostgreSQL connection is not protected by TLS")
    return True


def schema_inspection_evidence(
    snapshot: SchemaSnapshot,
    sources: Sequence[MigrationSource],
    classification: SchemaClassification,
    planned: Sequence[MigrationDefinition],
    *,
    connection_tls: bool,
) -> dict[str, Any]:
    """Build the complete sanitized, read-only schema classification record."""

    unknown_object_count = len(snapshot.markers - _KNOWN_MARKERS) + len(
        set(snapshot.ledger) - set(EXPECTED_MIGRATION_HASHES)
    )
    return {
        "classification": BootstrapClassification.PASS_SCHEMA_INSPECTION,
        "connection_tls": connection_tls,
        "database": DATABASE_NAME,
        "drift_detected": False,
        "event": "db_schema_inspection",
        "migration_001": snapshot.state(MIGRATION_001).value,
        "migration_001_expected_hash": MIGRATION_001.checksum_sha256,
        "migration_002": snapshot.state(MIGRATION_002).value,
        "migration_002_expected_hash": MIGRATION_002.checksum_sha256,
        "migration_003": snapshot.state(MIGRATION_003).value,
        "migration_003_expected_hash": MIGRATION_003.checksum_sha256,
        "migration_hashes": {
            source.definition.version: source.definition.checksum_sha256 for source in sources
        },
        "migration_state": {
            migration.version: snapshot.state(migration).value for migration in MIGRATIONS
        },
        "mode": "read_only",
        "planned_migrations": [migration.version for migration in planned],
        "read_only": True,
        "schema_classification": classification,
        "schema_constraints_verified": True,
        "token_acquired": True,
        "token_memory_only": True,
        "tls_required": True,
        "unknown_object_count": unknown_object_count,
    }


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
        connection_tls = _connection_uses_tls(connection)
        snapshot = inspect_schema(connection)
        classification = classify_schema(snapshot)
        planned = plan_migrations(snapshot)
        connection.rollback()
    return schema_inspection_evidence(
        snapshot,
        sources,
        classification,
        planned,
        connection_tls=connection_tls,
    )


def _ownership_evidence(inventory: OwnershipInventory) -> dict[str, Any]:
    relation_counts: dict[str, int] = {}
    for _, kind, _ in inventory.relation_owners:
        relation_counts[kind] = relation_counts.get(kind, 0) + 1
    return {
        "database_owners": [list(item) for item in inventory.database_owners],
        "schema_owners": [list(item) for item in inventory.schema_owners],
        "relation_counts_by_kind": relation_counts,
        "relation_owner_counts": {
            owner: sum(item[2] == owner for item in inventory.relation_owners)
            for owner in sorted({item[2] for item in inventory.relation_owners})
        },
        "function_count": len(inventory.function_owners),
        "function_owner_counts": {
            owner: sum(item[1] == owner for item in inventory.function_owners)
            for owner in sorted({item[1] for item in inventory.function_owners})
        },
        "migration_ledger": dict(inventory.migration_ledger),
        "orphan_acl_count": inventory.orphan_acl_count,
        "orphan_default_acl_count": inventory.orphan_default_acl_count,
        "orphan_memberships": [list(item) for item in inventory.orphan_memberships],
        "orphan_dependency_count": inventory.orphan_dependency_count,
    }


def _require_complete_reviewed_schema(connection: Any) -> SchemaSnapshot:
    snapshot = inspect_schema(connection)
    if plan_migrations(snapshot):
        raise DatabaseSchemaDrift("ownership repair requires both accepted migrations")
    if dict(snapshot.ledger) != EXPECTED_MIGRATION_HASHES:
        raise DatabaseSchemaDrift("migration ledger differs from accepted hashes")
    return snapshot


def run_ownership_inspection(
    config: JobConfig,
    connection_factory: Callable[[], Any],
    administrator_connection_factory: Callable[[], Any],
    migration_root: Path,
) -> dict[str, Any]:
    load_migration_sources(migration_root)
    with administrator_connection_factory() as administrator_connection:
        if _current_user(administrator_connection) != REMEDIATION_PRINCIPAL_NAME:
            raise IdentityMismatch("remediation connection identity is unexpected")
        verify_remediation_administrator(administrator_connection, config)
        all_principals = _entra_principal_names(administrator_connection, False)
        admin_principals = _entra_principal_names(administrator_connection, True)
        durable_mapping_absent = (
            read_entra_principal(administrator_connection, DURABLE_OWNER_NAME) is None
        )
    with connection_factory() as connection:
        if _current_user(connection) != REMEDIATION_PRINCIPAL_NAME:
            raise IdentityMismatch("remediation connection identity is unexpected")
        connection_tls = _connection_uses_tls(connection)
        _require_complete_reviewed_schema(connection)
        inventory = inspect_ownership(connection)
        durable = read_role_record(connection, DURABLE_OWNER_NAME)
        runtime = read_role_record(connection, RUNTIME_PRINCIPAL_NAME)
        orphan = read_role_record(connection, BOOTSTRAP_PRINCIPAL_NAME)
        connection.rollback()
    if runtime is None or runtime.azure_pg_admin_member:
        raise IdentityMismatch("runtime PostgreSQL role is absent or administrative")
    return {
        "classification": BootstrapClassification.PASS_OWNERSHIP_INSPECTION,
        "connection_tls": connection_tls,
        "database": DATABASE_NAME,
        "durable_owner_mapping_absent": durable_mapping_absent,
        "durable_owner_present": durable is not None,
        "entra_admin_principals": list(admin_principals),
        "entra_principals": list(all_principals),
        "event": "db_ownership_inspection",
        "migration_hashes": EXPECTED_MIGRATION_HASHES,
        "mode": "read_only",
        "orphan_role_present": orphan is not None,
        "ownership": _ownership_evidence(inventory),
        "read_only": True,
        "runtime_non_admin": True,
        "runtime_non_owner": all(
            owner != RUNTIME_PRINCIPAL_NAME
            for _, owner in inventory.schema_owners + inventory.function_owners
        )
        and all(owner != RUNTIME_PRINCIPAL_NAME for _, _, owner in inventory.relation_owners),
        "token_acquired": True,
        "token_memory_only": True,
        "tls_required": True,
    }


def run_ownership_remediation(
    config: JobConfig,
    connection_factory: Callable[[], Any],
    administrator_connection_factory: Callable[[], Any],
    migration_root: Path,
) -> dict[str, Any]:
    load_migration_sources(migration_root)
    with administrator_connection_factory() as administrator_connection:
        if _current_user(administrator_connection) != REMEDIATION_PRINCIPAL_NAME:
            raise IdentityMismatch("remediation connection identity is unexpected")
        verify_remediation_administrator(administrator_connection, config)
        durable = read_role_record(administrator_connection, DURABLE_OWNER_NAME)
        if durable is None:
            administrator_connection.execute(
                f"CREATE ROLE {DURABLE_OWNER_NAME} "
                "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS"
            )
            administrator_connection.commit()
            durable_created = True
        else:
            durable_created = False
        validate_durable_owner(read_role_record(administrator_connection, DURABLE_OWNER_NAME))
        if read_entra_principal(administrator_connection, DURABLE_OWNER_NAME) is not None:
            raise IdentityMismatch("durable owner must not have an Entra mapping")
    with connection_factory() as connection:
        if _current_user(connection) != REMEDIATION_PRINCIPAL_NAME:
            raise IdentityMismatch("remediation connection identity is unexpected")
        connection_tls = _connection_uses_tls(connection)
        _require_complete_reviewed_schema(connection)
        before = inspect_ownership(connection)
        allowed_memberships = {
            ("azure_pg_admin", BOOTSTRAP_PRINCIPAL_NAME),
            (RUNTIME_PRINCIPAL_NAME, BOOTSTRAP_PRINCIPAL_NAME),
        }
        unexpected_memberships = tuple(
            item for item in before.orphan_memberships if item not in allowed_memberships
        )
        if before.orphan_default_acl_count or unexpected_memberships:
            raise OwnershipTransferFailure(
                "orphan has unreviewed default privileges or role memberships"
            )
        connection.execute(
            f'REASSIGN OWNED BY "{BOOTSTRAP_PRINCIPAL_NAME}" TO {DURABLE_OWNER_NAME}'
        )
        if (RUNTIME_PRINCIPAL_NAME, BOOTSTRAP_PRINCIPAL_NAME) in before.orphan_memberships:
            connection.execute(
                f'REVOKE "{RUNTIME_PRINCIPAL_NAME}" FROM "{BOOTSTRAP_PRINCIPAL_NAME}"'
            )
        connection.execute(
            f'REVOKE ALL PRIVILEGES ON DATABASE {DATABASE_NAME} FROM "{BOOTSTRAP_PRINCIPAL_NAME}"'
        )
        connection.execute(
            f'REVOKE ALL PRIVILEGES ON SCHEMA trading FROM "{BOOTSTRAP_PRINCIPAL_NAME}"'
        )
        connection.execute(
            f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA trading "
            f'FROM "{BOOTSTRAP_PRINCIPAL_NAME}"'
        )
        connection.execute(
            f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA trading "
            f'FROM "{BOOTSTRAP_PRINCIPAL_NAME}"'
        )
        connection.execute(
            f"REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA trading "
            f'FROM "{BOOTSTRAP_PRINCIPAL_NAME}"'
        )
        after = inspect_ownership(connection)
        _require_complete_reviewed_schema(connection)
        application_owners = {owner for _, owner in after.schema_owners + after.function_owners} | {
            owner for _, _, owner in after.relation_owners
        }
        if application_owners != {DURABLE_OWNER_NAME}:
            raise OwnershipTransferFailure("application objects are not durably owned")
        if after.orphan_dependency_count:
            raise OwnershipTransferFailure("orphan database dependencies remain")
        connection.commit()
    return {
        "classification": BootstrapClassification.PASS_OWNERSHIP_REMEDIATION,
        "connection_tls": connection_tls,
        "database": DATABASE_NAME,
        "durable_owner": DURABLE_OWNER_NAME,
        "durable_owner_created": durable_created,
        "durable_owner_entra_mapping": False,
        "durable_owner_login": False,
        "event": "db_ownership_remediation",
        "migration_hashes": EXPECTED_MIGRATION_HASHES,
        "mode": "ownership_remediation",
        "objects_transferred": {
            "functions": sum(
                owner == BOOTSTRAP_PRINCIPAL_NAME for _, owner in before.function_owners
            ),
            "relations": sum(
                owner == BOOTSTRAP_PRINCIPAL_NAME for _, _, owner in before.relation_owners
            ),
            "schemas": sum(owner == BOOTSTRAP_PRINCIPAL_NAME for _, owner in before.schema_owners),
        },
        "orphan_dependency_count_after": after.orphan_dependency_count,
        "runtime_role_membership_removed": (
            (RUNTIME_PRINCIPAL_NAME, BOOTSTRAP_PRINCIPAL_NAME) in before.orphan_memberships
        ),
        "ownership_after": _ownership_evidence(after),
        "runtime_non_admin": True,
        "runtime_non_owner": True,
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
        validate_durable_owner(read_role_record(administrator_connection, DURABLE_OWNER_NAME))
        if read_entra_principal(administrator_connection, DURABLE_OWNER_NAME) is not None:
            raise IdentityMismatch("durable owner must not have an Entra mapping")
        principal_created = ensure_runtime_principal(
            administrator_connection,
            config.runtime_identity_object_id,
        )
        if _shadow_owner_membership_exists(administrator_connection):
            raise OwnershipTransferFailure("durable owner membership already exists")
        membership_granted = True
        try:
            administrator_connection.execute(
                f'GRANT "{DURABLE_OWNER_NAME}" TO "{BOOTSTRAP_PRINCIPAL_NAME}"'
            )
            administrator_connection.commit()
            with connection_factory() as connection:
                if _current_user(connection) != BOOTSTRAP_PRINCIPAL_NAME:
                    raise IdentityMismatch("bootstrap connection identity is unexpected")
                connection_tls = _connection_uses_tls(connection)
                before = inspect_schema(connection)
                applied = apply_required_migrations(
                    connection,
                    sources,
                    BOOTSTRAP_PRINCIPAL_NAME,
                )
                _transfer_shadow_position_owner(connection)
                principal = read_entra_principal(connection, RUNTIME_PRINCIPAL_NAME)
                if principal is None:
                    raise IdentityMismatch(
                        "runtime principal is absent from the application database"
                    )
                validate_runtime_principal(principal, config.runtime_identity_object_id)
                apply_runtime_grants(connection)
                privileges = read_runtime_privileges(connection)
                validate_runtime_privileges(privileges)
                after = inspect_schema(connection)
        finally:
            if membership_granted:
                try:
                    administrator_connection.rollback()
                    administrator_connection.execute(
                        f'REVOKE "{DURABLE_OWNER_NAME}" FROM "{BOOTSTRAP_PRINCIPAL_NAME}"'
                    )
                    administrator_connection.commit()
                except Exception:
                    raise OwnershipTransferFailure(
                        "temporary durable owner membership removal failed"
                    ) from None
                if _shadow_owner_membership_exists(administrator_connection):
                    raise OwnershipTransferFailure(
                        "temporary durable owner membership remains"
                    )
    return {
        "classification": BootstrapClassification.PASS_BOOTSTRAP_ADMIN,
        "connection_tls": connection_tls,
        "database": DATABASE_NAME,
        "event": "db_bootstrap_admin",
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
        "mode": "bootstrap_admin",
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
        current_user = _current_user(connection)
        if current_user != RUNTIME_PRINCIPAL_NAME:
            raise IdentityMismatch("runtime connection identity is unexpected")
        connection_tls = _connection_uses_tls(connection)
        try:
            privileges = read_runtime_privileges(connection)
        except Exception as error:
            if error.__class__.__module__.startswith("psycopg"):
                raise PrivilegeMismatch("runtime privilege inspection failed") from None
            raise
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
        "connection_tls": connection_tls,
        "current_user": current_user,
        "authorized_for_broker_execution": False,
        "database": DATABASE_NAME,
        "event": "runtime_db_probe",
        "initial_fencing_token": lease.fencing_token,
        "lease_acquired": True,
        "lease_released": True,
        "lease_renewed": True,
        "mode": "runtime_probe",
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


def emit_sanitized_evidence(
    evidence: Mapping[str, Any],
    *,
    stream: TextIO | None = None,
) -> None:
    """Emit exactly one flushed, machine-readable, token-safe console record."""

    serialized = json.dumps(evidence, separators=(",", ":"), sort_keys=True)
    if re.search(r"eyJ[A-Za-z0-9_-]{12,}\.", serialized):
        raise BootstrapError("evidence contains token-shaped material")
    if stream is None:
        stream = sys.stdout
    print(_EVIDENCE_PREFIX + serialized, file=stream, flush=True)


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


def validate_execution_nonce(value: str) -> str:
    nonce = value.strip()
    if not _EXECUTION_NONCE_PATTERN.fullmatch(nonce):
        raise BootstrapError("execution nonce is invalid")
    return nonce


def _default_migration_root() -> Path:
    return Path(__file__).resolve().parents[2] / "migrations" / "postgresql"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=(
            "observability-canary",
            "schema-inspect",
            "bootstrap-admin",
            "ownership-inspect",
            "ownership-remediate",
            "privilege-audit",
            "privilege-repair",
            "privilege-drift-repair",
            "runtime-probe",
        ),
    )
    parser.add_argument("--execution-nonce", required=True)
    parser.add_argument("--evidence", type=Path, default=Path("/tmp/db-job-evidence.json"))
    parser.add_argument("--migration-root", type=Path, default=_default_migration_root())
    arguments = parser.parse_args()
    try:
        nonce = validate_execution_nonce(arguments.execution_nonce)
        if arguments.mode == "observability-canary":
            evidence = {
                "classification": "CANARY_PASS",
                "event": "observability_canary",
                "nonce": nonce,
            }
        else:
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
            elif arguments.mode == "ownership-inspect":
                evidence = run_ownership_inspection(
                    config,
                    factory,
                    factory.administrator_connection,
                    arguments.migration_root,
                )
            elif arguments.mode == "ownership-remediate":
                evidence = run_ownership_remediation(
                    config,
                    factory,
                    factory.administrator_connection,
                    arguments.migration_root,
                )
            elif arguments.mode == "privilege-audit":
                evidence = run_privilege_audit(
                    config,
                    factory,
                    factory.administrator_connection,
                )
            elif arguments.mode == "privilege-repair":
                evidence = run_privilege_repair(
                    config,
                    factory,
                    factory.administrator_connection,
                )
            elif arguments.mode == "privilege-drift-repair":
                evidence = run_privilege_drift_repair(
                    config,
                    factory,
                    factory.administrator_connection,
                )
            else:
                evidence = run_runtime_probe(config, factory)
            evidence["execution_nonce"] = nonce
            evidence["token_acquired"] = factory.token_acquired
        write_sanitized_evidence(arguments.evidence, evidence)
        emit_sanitized_evidence(evidence)
        return 0
    except BootstrapError as error:
        failure = {
            "classification": error.classification,
            "event": "db_job_failure",
            "status": "fail_closed",
        }
        emit_sanitized_evidence(failure, stream=sys.stderr)
        return 2
    except LeaseError:
        failure = {
            "classification": BootstrapClassification.DATABASE_UNAVAILABLE,
            "event": "db_job_failure",
            "status": "fail_closed",
        }
        emit_sanitized_evidence(failure, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
