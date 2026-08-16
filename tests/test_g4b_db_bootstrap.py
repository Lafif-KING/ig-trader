"""G4B-02B2A database bootstrap, probe, image, and IaC contracts."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from src.ig_trader.db_bootstrap import (
    BOOTSTRAP_PRINCIPAL_NAME,
    EXPECTED_MIGRATION_HASHES,
    MIGRATION_001,
    MIGRATION_002,
    RUNTIME_PRINCIPAL_NAME,
    BootstrapError,
    DatabaseSchemaDrift,
    IdentityMismatch,
    JobConfig,
    MigrationState,
    PrincipalRecord,
    PrivilegeMismatch,
    PrivilegeSnapshot,
    SchemaSnapshot,
    apply_required_migrations,
    inspect_schema,
    load_migration_sources,
    plan_migrations,
    validate_runtime_principal,
    validate_runtime_privileges,
    write_sanitized_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_ROOT = ROOT / "migrations" / "postgresql"
POSTGRES_INTEGRATION_ENV = "RUN_POSTGRES_INTEGRATION"
POSTGRES_DSN_ENV = "TEST_POSTGRES_DSN"
RUNTIME_OBJECT_ID = "00000000-0000-0000-0000-000000000101"
BOOTSTRAP_OBJECT_ID = "00000000-0000-0000-0000-000000000202"
CLIENT_ID = "00000000-0000-0000-0000-000000000303"


def _markers(*definitions: object) -> frozenset[str]:
    values: set[str] = set()
    for definition in definitions:
        values.update(definition.required_markers)  # type: ignore[attr-defined]
    return frozenset(values)


def _ledger(*versions: str) -> dict[str, str]:
    return {version: EXPECTED_MIGRATION_HASHES[version] for version in versions}


def _environment(mode: str) -> dict[str, str]:
    bootstrap = mode == "bootstrap-admin"
    values = {
        "AZURE_CLIENT_ID": CLIENT_ID,
        "JOB_IDENTITY_NAME": (BOOTSTRAP_PRINCIPAL_NAME if bootstrap else RUNTIME_PRINCIPAL_NAME),
        "JOB_UAMI_OBJECT_ID": BOOTSTRAP_OBJECT_ID if bootstrap else RUNTIME_OBJECT_ID,
        "POSTGRES_DATABASE": "ig_trader",
        "POSTGRES_HOST": "example.postgres.database.azure.com",
    }
    if bootstrap:
        values["RUNTIME_UAMI_OBJECT_ID"] = RUNTIME_OBJECT_ID
    return values


def test_blank_database_plans_001_then_002() -> None:
    snapshot = SchemaSnapshot(markers=frozenset(), ledger={})

    assert plan_migrations(snapshot) == (MIGRATION_001, MIGRATION_002)
    assert snapshot.state(MIGRATION_001) is MigrationState.ABSENT


def test_001_present_and_002_absent_plans_only_002() -> None:
    snapshot = SchemaSnapshot(
        markers=_markers(MIGRATION_001),
        ledger=_ledger(MIGRATION_001.version),
    )

    assert plan_migrations(snapshot) == (MIGRATION_002,)


def test_both_migrations_present_are_verified_without_reapplication() -> None:
    snapshot = SchemaSnapshot(
        markers=_markers(MIGRATION_001, MIGRATION_002),
        ledger=_ledger(MIGRATION_001.version, MIGRATION_002.version),
    )

    assert plan_migrations(snapshot) == ()


def test_partial_001_fails_closed() -> None:
    snapshot = SchemaSnapshot(
        markers=frozenset({"schema:trading", "relation:trade_intents"}),
        ledger={},
    )

    with pytest.raises(DatabaseSchemaDrift):
        plan_migrations(snapshot)


def test_partial_002_fails_closed() -> None:
    snapshot = SchemaSnapshot(
        markers=_markers(MIGRATION_001) | {"relation:worker_lease_fencing_token_seq"},
        ledger=_ledger(MIGRATION_001.version),
    )

    with pytest.raises(DatabaseSchemaDrift):
        plan_migrations(snapshot)


def test_complete_schema_without_reviewed_ledger_hash_fails_closed() -> None:
    snapshot = SchemaSnapshot(markers=_markers(MIGRATION_001), ledger={})

    with pytest.raises(DatabaseSchemaDrift):
        plan_migrations(snapshot)


def test_migration_hash_mismatch_fails_before_database_access(tmp_path: Path) -> None:
    for name in (MIGRATION_001.filename, MIGRATION_002.filename):
        (tmp_path / name).write_bytes((MIGRATION_ROOT / name).read_bytes())
    (tmp_path / MIGRATION_002.filename).write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")

    with pytest.raises(DatabaseSchemaDrift):
        load_migration_sources(tmp_path)


def test_reviewed_migration_hashes_are_stable() -> None:
    sources = load_migration_sources(MIGRATION_ROOT)

    assert {item.definition.version: item.definition.checksum_sha256 for item in sources} == (
        EXPECTED_MIGRATION_HASHES
    )


def test_existing_runtime_principal_with_exact_object_identity_is_accepted() -> None:
    record = PrincipalRecord(
        role_name=RUNTIME_PRINCIPAL_NAME,
        principal_type="service",
        object_id=RUNTIME_OBJECT_ID,
        is_admin=False,
    )

    validate_runtime_principal(record, RUNTIME_OBJECT_ID)


def test_runtime_principal_with_wrong_object_id_fails_closed() -> None:
    record = PrincipalRecord(
        role_name=RUNTIME_PRINCIPAL_NAME,
        principal_type="service",
        object_id=str(uuid4()),
        is_admin=False,
    )

    with pytest.raises(IdentityMismatch):
        validate_runtime_principal(record, RUNTIME_OBJECT_ID)


@pytest.mark.parametrize(
    "unsafe",
    [
        {"is_admin": True},
        {"is_superuser": True},
        {"can_create_role": True},
        {"can_create_database": True},
        {"azure_pg_admin_member": True},
    ],
)
def test_runtime_principal_with_admin_authority_fails_closed(
    unsafe: dict[str, bool],
) -> None:
    record = PrincipalRecord(
        role_name=RUNTIME_PRINCIPAL_NAME,
        principal_type="service",
        object_id=RUNTIME_OBJECT_ID,
        is_admin=False,
    )

    with pytest.raises(IdentityMismatch):
        validate_runtime_principal(replace(record, **unsafe), RUNTIME_OBJECT_ID)


def _safe_privileges() -> PrivilegeSnapshot:
    return PrivilegeSnapshot(
        database_connect=True,
        database_create=False,
        schema_usage=True,
        schema_create=False,
        missing_required=(),
        prohibited_present=(),
        owned_object_count=0,
    )


def test_exact_minimum_privilege_snapshot_is_accepted() -> None:
    validate_runtime_privileges(_safe_privileges())


@pytest.mark.parametrize(
    "unsafe",
    [
        {"database_create": True},
        {"schema_create": True},
        {"missing_required": ("trading.worker_leases:SELECT",)},
        {"prohibited_present": ("trading.worker_leases:UPDATE",)},
        {"owned_object_count": 1},
    ],
)
def test_missing_or_prohibited_privilege_fails_closed(unsafe: dict[str, object]) -> None:
    with pytest.raises(PrivilegeMismatch):
        validate_runtime_privileges(replace(_safe_privileges(), **unsafe))


def test_bootstrap_identity_is_required_and_distinct() -> None:
    config = JobConfig.from_environment("bootstrap-admin", _environment("bootstrap-admin"))

    assert config.job_identity_name == BOOTSTRAP_PRINCIPAL_NAME
    assert config.runtime_identity_object_id == RUNTIME_OBJECT_ID
    assert config.job_identity_object_id != config.runtime_identity_object_id


def test_runtime_identity_cannot_run_bootstrap_admin() -> None:
    environment = _environment("runtime-probe")
    environment["RUNTIME_UAMI_OBJECT_ID"] = RUNTIME_OBJECT_ID

    with pytest.raises(IdentityMismatch):
        JobConfig.from_environment("bootstrap-admin", environment)


def test_bootstrap_identity_cannot_run_runtime_probe() -> None:
    with pytest.raises(IdentityMismatch):
        JobConfig.from_environment("runtime-probe", _environment("bootstrap-admin"))


@pytest.mark.parametrize("name", ["DATABASE_URL", "PGPASSWORD", "POSTGRES_PASSWORD"])
def test_password_and_dsn_configuration_is_rejected(name: str) -> None:
    environment = _environment("runtime-probe")
    environment[name] = "not-accepted"

    with pytest.raises(BootstrapError, match="prohibited"):
        JobConfig.from_environment("runtime-probe", environment)


def test_evidence_writer_rejects_token_shaped_material(tmp_path: Path) -> None:
    token = "eyJ" + ("a" * 24) + "." + ("b" * 24) + "." + ("c" * 24)

    with pytest.raises(BootstrapError, match="token-shaped"):
        write_sanitized_evidence(tmp_path / "evidence.json", {"value": token})

    assert not (tmp_path / "evidence.json").exists()


def test_sanitized_evidence_contains_only_boolean_token_result(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    write_sanitized_evidence(path, {"token_acquired": True, "status": "pass"})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "status": "pass",
        "token_acquired": True,
    }


def test_cloud_bootstrap_source_has_no_sqlite_fallback_or_broker_import() -> None:
    source = (ROOT / "src/ig_trader/db_bootstrap.py").read_text(encoding="utf-8")

    assert "sqlite3" not in source
    assert "sqlite:///" not in source
    assert "ig_trader.session" not in source
    assert "from ig_trader.execution import" not in source
    assert "lightstreamer" not in source.casefold()
    assert 'return self.connect_database("postgres")' in source


def test_bootstrap_image_contains_only_required_project_inputs() -> None:
    dockerfile = (ROOT / "Dockerfile.db-bootstrap").read_text(encoding="utf-8")

    assert "python:3.13.14-slim-bookworm@sha256:" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "ig_trader.db_bootstrap"]' in dockerfile
    assert "001_execution_state.sql" in dockerfile
    assert "002_execution_lease_fencing.sql" in dockerfile
    assert "COPY src ./src" not in dockerfile
    assert ".env" not in dockerfile


def test_job_iac_owns_only_the_temporary_bootstrap_stage() -> None:
    bicep = (ROOT / "infra/azure/dev-shadow-db-bootstrap.bicep").read_text(encoding="utf-8")

    assert bicep.count("resource bootstrapIdentity ") == 1
    assert bicep.count("resource bootstrapAcrPull ") == 1
    assert bicep.count("resource postgresBootstrapAdministrator ") == 1
    assert bicep.count("'Microsoft.App/jobs@2025-01-01' =") == 2
    assert bicep.count(" existing =") == 4
    assert "Microsoft.App/containerApps@" not in bicep
    assert "Microsoft.Network/" not in bicep
    assert "firewall" not in bicep.casefold()
    assert "publicNetworkAccess" not in bicep
    assert "secrets: []" in bicep
    assert "RUNTIME_UAMI_OBJECT_ID" in bicep
    assert "runtimeIdentity.properties.principalId" in bicep
    assert "@sha256" in (
        ROOT / "infra/azure/dev-shadow-db-bootstrap.parameters.bicepparam"
    ).read_text(encoding="utf-8")


def test_ci_has_dedicated_bootstrap_postgresql_image_and_evidence_gates() -> None:
    workflow = (ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")
    evidence = (ROOT / "tools/g4a_ci_evidence.py").read_text(encoding="utf-8")

    assert "tests-g4b-db-bootstrap.xml" in workflow
    assert "tests-g4b-db-bootstrap-postgres.xml" in workflow
    assert "Dockerfile.db-bootstrap" in workflow
    assert "tools/g4b_db_bootstrap_image_inspect.py" in workflow
    assert "--network none" in workflow
    assert "dev-shadow-db-bootstrap.bicep" in workflow
    assert "dev-shadow-db-bootstrap.parameters.bicepparam" in workflow
    assert '"g4b_db_bootstrap": _junit' in evidence
    assert '"g4b_db_bootstrap_postgres": _junit' in evidence
    assert "db-bootstrap-image-inspection.json" in evidence


def _required_local_postgres_dsn() -> str:
    if os.environ.get(POSTGRES_INTEGRATION_ENV) != "1":
        pytest.skip("real PostgreSQL runs only in the dedicated bounded CI gate")
    dsn = os.environ.get(POSTGRES_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip("remote CI provides the ephemeral PostgreSQL 16 service")
    from psycopg.conninfo import conninfo_to_dict

    values = conninfo_to_dict(dsn)
    if values.get("host") not in {"127.0.0.1", "localhost"}:
        pytest.fail("bootstrap integration test refuses non-loopback PostgreSQL")
    if values.get("dbname") != "postgres" or values.get("user") != "postgres":
        pytest.fail("bootstrap integration test requires the disposable CI database")
    return dsn


def test_real_postgresql_blank_bootstrap_applies_and_verifies_both_migrations() -> None:
    dsn = _required_local_postgres_dsn()
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("DROP SCHEMA IF EXISTS trading CASCADE")
    with psycopg.connect(dsn) as connection:
        sources = load_migration_sources(MIGRATION_ROOT)
        applied = apply_required_migrations(connection, sources, "ephemeral-ci-bootstrap")
        assert applied == (MIGRATION_001.version, MIGRATION_002.version)
        assert plan_migrations(inspect_schema(connection)) == ()
