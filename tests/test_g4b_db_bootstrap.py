"""G4B-02B2A database bootstrap, probe, image, and IaC contracts."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.ig_trader.db_bootstrap import (
    BOOTSTRAP_PRINCIPAL_NAME,
    DURABLE_OWNER_NAME,
    EXPECTED_MIGRATION_HASHES,
    GRANT_REPAIR_PRINCIPAL_NAME,
    MIGRATION_001,
    MIGRATION_002,
    MIGRATION_003,
    REMEDIATION_PRINCIPAL_NAME,
    RUNTIME_PRINCIPAL_NAME,
    BootstrapError,
    DatabaseSchemaDrift,
    IdentityMismatch,
    JobConfig,
    MigrationState,
    OwnershipTransferFailure,
    PrincipalRecord,
    PrivilegeMismatch,
    PrivilegeSnapshot,
    RoleRecord,
    SchemaClassification,
    SchemaSnapshot,
    apply_required_migrations,
    apply_runtime_grants,
    classify_schema,
    emit_sanitized_evidence,
    inspect_ownership,
    inspect_schema,
    load_migration_sources,
    plan_migrations,
    read_exact_runtime_privileges,
    read_function_provenance,
    read_reject_function_provenance,
    schema_inspection_evidence,
    validate_durable_owner,
    validate_execution_nonce,
    validate_runtime_principal,
    validate_runtime_privileges,
    write_sanitized_evidence,
)
from src.ig_trader.execution_lease import (
    EXECUTION_LEASE_NAME,
    FencedOperation,
    FencingRejected,
    PostgresExecutionLeaseStore,
)
from src.ig_trader.db_bootstrap import (
    main as db_bootstrap_main,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_ROOT = ROOT / "migrations" / "postgresql"
POSTGRES_INTEGRATION_ENV = "RUN_POSTGRES_INTEGRATION"
POSTGRES_DSN_ENV = "TEST_POSTGRES_DSN"
RUNTIME_OBJECT_ID = "00000000-0000-0000-0000-000000000101"
BOOTSTRAP_OBJECT_ID = "00000000-0000-0000-0000-000000000202"
CLIENT_ID = "00000000-0000-0000-0000-000000000303"
REMEDIATION_OBJECT_ID = "00000000-0000-0000-0000-000000000404"


def _markers(*definitions: object) -> frozenset[str]:
    values: set[str] = set()
    for definition in definitions:
        values.update(definition.required_markers)  # type: ignore[attr-defined]
    return frozenset(values)


def _ledger(*versions: str) -> dict[str, str]:
    return {version: EXPECTED_MIGRATION_HASHES[version] for version in versions}


def _environment(mode: str) -> dict[str, str]:
    bootstrap = mode in {"bootstrap-admin", "schema-inspect"}
    remediation = mode in {"ownership-inspect", "ownership-remediate"}
    grant_repair = mode in {
        "privilege-audit",
        "privilege-repair",
        "privilege-drift-repair",
    }
    values = {
        "AZURE_CLIENT_ID": CLIENT_ID,
        "JOB_IDENTITY_NAME": (
            BOOTSTRAP_PRINCIPAL_NAME
            if bootstrap
            else REMEDIATION_PRINCIPAL_NAME
            if remediation
            else GRANT_REPAIR_PRINCIPAL_NAME
            if grant_repair
            else RUNTIME_PRINCIPAL_NAME
        ),
        "JOB_UAMI_OBJECT_ID": (
            BOOTSTRAP_OBJECT_ID
            if bootstrap
            else REMEDIATION_OBJECT_ID
            if remediation
            else REMEDIATION_OBJECT_ID
            if grant_repair
            else RUNTIME_OBJECT_ID
        ),
        "POSTGRES_DATABASE": "ig_trader",
        "POSTGRES_HOST": "example.postgres.database.azure.com",
    }
    if bootstrap or remediation or grant_repair:
        values["RUNTIME_UAMI_OBJECT_ID"] = RUNTIME_OBJECT_ID
    if remediation:
        values["ORPHAN_UAMI_OBJECT_ID"] = BOOTSTRAP_OBJECT_ID
    return values


def test_blank_database_plans_001_then_003() -> None:
    snapshot = SchemaSnapshot(markers=frozenset(), ledger={})

    assert plan_migrations(snapshot) == (MIGRATION_001, MIGRATION_002, MIGRATION_003)
    assert snapshot.state(MIGRATION_001) is MigrationState.ABSENT
    assert classify_schema(snapshot) is SchemaClassification.BLANK


def test_001_present_and_002_absent_plans_002_then_003() -> None:
    snapshot = SchemaSnapshot(
        markers=_markers(MIGRATION_001),
        ledger=_ledger(MIGRATION_001.version),
    )

    assert plan_migrations(snapshot) == (MIGRATION_002, MIGRATION_003)
    assert classify_schema(snapshot) is SchemaClassification.MIGRATION_001_COMPLETE_ONLY


def test_001_and_002_complete_marks_003_absent() -> None:
    snapshot = SchemaSnapshot(
        markers=_markers(MIGRATION_001, MIGRATION_002),
        ledger=_ledger(MIGRATION_001.version, MIGRATION_002.version),
    )

    assert plan_migrations(snapshot) == (MIGRATION_003,)
    assert (
        classify_schema(snapshot)
        is SchemaClassification.MIGRATIONS_001_AND_002_COMPLETE_003_ABSENT
    )


def test_all_migrations_present_are_verified_without_reapplication() -> None:
    snapshot = SchemaSnapshot(
        markers=_markers(MIGRATION_001, MIGRATION_002, MIGRATION_003),
        ledger=_ledger(MIGRATION_001.version, MIGRATION_002.version, MIGRATION_003.version),
    )

    assert plan_migrations(snapshot) == ()
    assert classify_schema(snapshot) is SchemaClassification.MIGRATIONS_001_TO_003_COMPLETE


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


def test_partial_003_fails_closed() -> None:
    snapshot = SchemaSnapshot(
        markers=_markers(MIGRATION_001, MIGRATION_002) | {"relation:shadow_position_state"},
        ledger=_ledger(MIGRATION_001.version, MIGRATION_002.version),
    )

    with pytest.raises(DatabaseSchemaDrift):
        plan_migrations(snapshot)


def test_unknown_constraint_footprint_fails_closed() -> None:
    snapshot = SchemaSnapshot(
        markers=_markers(MIGRATION_001) | {"constraint-count:trade_intents:c:7"},
        ledger=_ledger(MIGRATION_001.version),
    )

    with pytest.raises(DatabaseSchemaDrift):
        classify_schema(snapshot)


def test_complete_schema_without_reviewed_ledger_hash_fails_closed() -> None:
    snapshot = SchemaSnapshot(markers=_markers(MIGRATION_001), ledger={})

    with pytest.raises(DatabaseSchemaDrift):
        plan_migrations(snapshot)


def test_migration_hash_mismatch_fails_before_database_access(tmp_path: Path) -> None:
    for name in (MIGRATION_001.filename, MIGRATION_002.filename, MIGRATION_003.filename):
        (tmp_path / name).write_bytes((MIGRATION_ROOT / name).read_bytes())
    (tmp_path / MIGRATION_003.filename).write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")

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


def test_schema_inspection_uses_bootstrap_identity_without_runtime_authority() -> None:
    config = JobConfig.from_environment("schema-inspect", _environment("schema-inspect"))

    assert config.database_user == BOOTSTRAP_PRINCIPAL_NAME
    assert config.job_identity_object_id == BOOTSTRAP_OBJECT_ID
    assert config.runtime_identity_object_id is None


def test_ownership_repair_requires_distinct_finite_remediation_identity() -> None:
    config = JobConfig.from_environment("ownership-remediate", _environment("ownership-remediate"))

    assert config.database_user == REMEDIATION_PRINCIPAL_NAME
    assert config.job_identity_object_id == REMEDIATION_OBJECT_ID
    assert config.runtime_identity_object_id == RUNTIME_OBJECT_ID
    assert config.orphan_identity_object_id == BOOTSTRAP_OBJECT_ID


def test_privilege_repair_requires_distinct_finite_admin_identity() -> None:
    config = JobConfig.from_environment("privilege-repair", _environment("privilege-repair"))

    assert config.database_user == GRANT_REPAIR_PRINCIPAL_NAME
    assert config.runtime_identity_object_id == RUNTIME_OBJECT_ID
    assert config.orphan_identity_object_id is None


def test_privilege_drift_repair_requires_distinct_finite_admin_identity() -> None:
    config = JobConfig.from_environment(
        "privilege-drift-repair", _environment("privilege-drift-repair")
    )

    assert config.database_user == GRANT_REPAIR_PRINCIPAL_NAME
    assert config.runtime_identity_object_id == RUNTIME_OBJECT_ID
    assert config.orphan_identity_object_id is None


def test_durable_owner_must_be_nologin_and_non_admin() -> None:
    safe = RoleRecord(
        role_name=DURABLE_OWNER_NAME,
        can_login=False,
        is_superuser=False,
        can_create_role=False,
        can_create_database=False,
        can_replicate=False,
        bypasses_rls=False,
        azure_pg_admin_member=False,
    )

    validate_durable_owner(safe)
    with pytest.raises(OwnershipTransferFailure, match="durable owner"):
        validate_durable_owner(replace(safe, can_login=True))


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


def test_observability_canary_emits_one_nonce_linked_line_without_db_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_path = tmp_path / "canary.json"

    def reject_database_factory(_config: object) -> None:
        pytest.fail("observability canary must not construct database credentials")

    monkeypatch.setattr(
        "src.ig_trader.db_bootstrap.ManagedIdentityConnectionFactory",
        reject_database_factory,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "db-bootstrap",
            "observability-canary",
            "--execution-nonce",
            "nonce-1234",
            "--evidence",
            str(evidence_path),
        ],
    )

    assert db_bootstrap_main() == 0
    assert capsys.readouterr().out.splitlines() == [
        'G4B_EVIDENCE={"classification":"CANARY_PASS",'
        '"event":"observability_canary","nonce":"nonce-1234"}'
    ]
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == {
        "classification": "CANARY_PASS",
        "event": "observability_canary",
        "nonce": "nonce-1234",
    }


@pytest.mark.parametrize("nonce", ["", "short", "contains space", "bad.dot.value"])
def test_execution_nonce_rejects_unlinkable_values(nonce: str) -> None:
    with pytest.raises(BootstrapError, match="nonce"):
        validate_execution_nonce(nonce)


def test_schema_inspection_evidence_has_required_read_only_contract() -> None:
    sources = load_migration_sources(MIGRATION_ROOT)
    evidence = schema_inspection_evidence(
        SchemaSnapshot(markers=frozenset(), ledger={}),
        sources,
        SchemaClassification.BLANK,
        (MIGRATION_001, MIGRATION_002, MIGRATION_003),
        connection_tls=True,
    )

    assert evidence == {
        "classification": "PASS_SCHEMA_INSPECTION",
        "connection_tls": True,
        "database": "ig_trader",
        "drift_detected": False,
        "event": "db_schema_inspection",
        "migration_001": "ABSENT",
        "migration_001_expected_hash": EXPECTED_MIGRATION_HASHES["001_execution_state"],
        "migration_002": "ABSENT",
        "migration_002_expected_hash": EXPECTED_MIGRATION_HASHES["002_execution_lease_fencing"],
        "migration_003": "ABSENT",
        "migration_003_expected_hash": EXPECTED_MIGRATION_HASHES["003_shadow_position_state"],
        "migration_hashes": EXPECTED_MIGRATION_HASHES,
        "migration_state": {
            "001_execution_state": "ABSENT",
            "002_execution_lease_fencing": "ABSENT",
            "003_shadow_position_state": "ABSENT",
        },
        "mode": "read_only",
        "planned_migrations": [
            "001_execution_state",
            "002_execution_lease_fencing",
            "003_shadow_position_state",
        ],
        "read_only": True,
        "schema_classification": "BLANK",
        "schema_constraints_verified": True,
        "token_acquired": True,
        "token_memory_only": True,
        "tls_required": True,
        "unknown_object_count": 0,
    }


def test_console_evidence_rejects_token_shaped_material() -> None:
    token = "eyJ" + ("a" * 24) + "." + ("b" * 24) + "." + ("c" * 24)

    with pytest.raises(BootstrapError, match="token-shaped"):
        emit_sanitized_evidence({"value": token})


def test_cloud_bootstrap_source_has_no_sqlite_fallback_or_broker_import() -> None:
    source = (ROOT / "src/ig_trader/db_bootstrap.py").read_text(encoding="utf-8")

    assert "sqlite3" not in source
    assert "sqlite:///" not in source
    assert "ig_trader.session" not in source
    assert "from ig_trader.execution import" not in source
    assert "lightstreamer" not in source.casefold()
    assert 'return self.connect_database("postgres")' in source
    assert '"initial_fencing_token": lease.fencing_token' in source
    assert '"successor_fencing_token": successor.fencing_token' in source
    assert '"token_memory_only": True' in source
    assert '"runtime_privileges": _privilege_evidence(privileges)' in source
    assert '"observability-canary"' in source
    assert '"ownership-inspect"' in source
    assert '"ownership-remediate"' in source
    assert '"privilege-audit"' in source
    assert '"privilege-repair"' in source
    assert "has_table_privilege(%s, %s::oid, %s)" in source
    assert "REASSIGN OWNED BY" in source
    assert 'REVOKE "{RUNTIME_PRINCIPAL_NAME}"' in source
    assert "DROP OWNED" not in source
    assert '"event": "db_schema_inspection"' in source
    assert '"execution_nonce"' in source
    assert "print(_EVIDENCE_PREFIX + serialized" in source


def test_bootstrap_image_contains_only_required_project_inputs() -> None:
    dockerfile = (ROOT / "Dockerfile.db-bootstrap").read_text(encoding="utf-8")
    runtime_dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "python:3.13.14-slim-bookworm@sha256:" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "ig_trader.db_bootstrap"]' in dockerfile
    assert "001_execution_state.sql" in dockerfile
    assert "002_execution_lease_fencing.sql" in dockerfile
    assert "003_shadow_position_state.sql" in dockerfile
    assert "COPY src ./src" not in dockerfile
    assert ".env" not in dockerfile
    assert "RUN rm ./src/ig_trader/db_bootstrap.py" in runtime_dockerfile
    assert "normal runtime contains database administration code" in (
        ROOT / "tools/g4a_image_inspect.py"
    ).read_text(encoding="utf-8")


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
    assert "'schema-inspect'" in bicep
    assert "project: 'ig-trader'" in bicep
    assert "purpose: 'db-bootstrap-temporary'" in bicep
    assert "'execution-authority': 'none'" in bicep
    assert "@sha256" in (
        ROOT / "infra/azure/dev-shadow-db-bootstrap.parameters.bicepparam"
    ).read_text(encoding="utf-8")


def test_ci_has_dedicated_bootstrap_postgresql_image_and_evidence_gates() -> None:
    workflow = (ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")
    evidence = (ROOT / "tools/g4a_ci_evidence.py").read_text(encoding="utf-8")

    assert "tests-g4b-db-bootstrap.xml" in workflow
    assert "tests-g4b-db-bootstrap-postgres.xml" in workflow
    assert "poetry run pip check" in workflow
    assert "Dockerfile.db-bootstrap" in workflow
    assert "tools/g4b_db_bootstrap_image_inspect.py" in workflow
    assert "--network none" in workflow
    assert "dev-shadow-db-bootstrap.bicep" in workflow
    assert "dev-shadow-db-bootstrap.parameters.bicepparam" in workflow
    assert '"g4b_db_bootstrap": _junit' in evidence
    assert '"g4b_db_bootstrap_postgres": _junit' in evidence
    assert "db-bootstrap-image-inspection.json" in evidence


def test_oidc_publisher_supports_both_reviewed_immutable_images() -> None:
    workflow = (ROOT / ".github/workflows/dev-shadow-image-publish.yaml").read_text(
        encoding="utf-8"
    )

    assert "image_kind:" in workflow
    assert "ig-trader-db-bootstrap" in workflow
    assert "Dockerfile.db-bootstrap" in workflow
    assert "tools/g4b_db_bootstrap_image_inspect.py" in workflow
    assert "IMAGE_TAG=$GITHUB_SHA" in workflow
    assert "inputs.image_kind == 'db_bootstrap'" in workflow
    assert "tests-g4b-db-bootstrap.xml" in workflow
    assert "latest" not in workflow.casefold()


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


def test_real_postgresql_blank_bootstrap_applies_and_verifies_all_migrations() -> None:
    admin_dsn = _required_local_postgres_dsn()
    import psycopg
    from psycopg.conninfo import make_conninfo

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS ig_trader")
        admin.execute("CREATE DATABASE ig_trader")
        admin.execute(
            "DO $$ BEGIN CREATE ROLE azure_pg_admin NOLOGIN; "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        admin.execute(
            f'DO $$ BEGIN CREATE ROLE "{RUNTIME_PRINCIPAL_NAME}" LOGIN; '
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    app_dsn = make_conninfo(admin_dsn, dbname="ig_trader")
    with psycopg.connect(app_dsn) as connection:
        connection.execute("DROP SCHEMA IF EXISTS trading CASCADE")
        sources = load_migration_sources(MIGRATION_ROOT)
        applied = apply_required_migrations(connection, sources, "ephemeral-ci-bootstrap")
        assert applied == (MIGRATION_001.version, MIGRATION_002.version, MIGRATION_003.version)
        assert plan_migrations(inspect_schema(connection)) == ()
        ownership = inspect_ownership(connection)
        assert ownership.schema_owners == (("trading", "postgres"),)
        assert len(ownership.relation_owners) == 12
        assert len(ownership.function_owners) == 6
        assert dict(ownership.migration_ledger) == EXPECTED_MIGRATION_HASHES
        assert connection.execute(
            """SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = 'trading' AND table_name = 'position_state'
              AND column_name = 'deal_id'"""
        ).fetchone()[0] == "NO"
        shadow_columns = {
            row[0]
            for row in connection.execute(
                """SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'trading' AND table_name = 'shadow_position_state'"""
            ).fetchall()
        }
        assert {"deal_id", "order_id", "working_order_id"}.isdisjoint(shadow_columns)
        assert "intent_id" in shadow_columns
        audit = read_exact_runtime_privileges(connection)
        assert "trading:USAGE" in audit.missing_required
        assert "trading.worker_leases:SELECT" in audit.missing_required
        assert any("reject_append_only_mutation" in item for item in audit.prohibited_present)
        provenance = read_reject_function_provenance(connection)
        assert provenance.classification == "PUBLIC_GRANT"
        assert provenance.public_execute is True
        assert provenance.direct_runtime_execute is False
        assert provenance.role_execute_sources == ()
        assert provenance.runtime_is_owner is False
        assert connection.execute(
            "SELECT proacl IS NULL FROM pg_proc WHERE oid = "
            "to_regprocedure('trading.reject_append_only_mutation()')"
        ).fetchone()[0]

        approved_signatures = (
            "trading.acquire_execution_lease(text,text,double precision)",
            "trading.renew_execution_lease(text,text,bigint,double precision)",
            "trading.release_execution_lease(text,text,bigint)",
            "trading.assert_execution_fence(text,text,bigint,text)",
            "trading.require_current_execution_fence()",
        )
        for signature in approved_signatures:
            approved = read_function_provenance(connection, signature)
            assert approved.classification == "NONE"
            assert approved.public_execute is False
            assert approved.direct_runtime_execute is False

        connection.execute(
            "REVOKE EXECUTE ON FUNCTION trading.reject_append_only_mutation() FROM PUBLIC"
        )
        revoked = read_reject_function_provenance(connection)
        assert revoked.classification == "NONE"
        assert revoked.public_execute is False
        assert revoked.direct_runtime_execute is False

        connection.execute(
            "GRANT EXECUTE ON FUNCTION trading.reject_append_only_mutation() TO "
            f'"{RUNTIME_PRINCIPAL_NAME}"'
        )
        direct = read_reject_function_provenance(connection)
        assert direct.classification == "DIRECT_GRANT"
        assert direct.direct_runtime_execute is True
        assert direct.public_execute is False

        connection.execute(
            "REVOKE EXECUTE ON FUNCTION trading.reject_append_only_mutation() FROM "
            f'"{RUNTIME_PRINCIPAL_NAME}"'
        )
        connection.execute(
            "GRANT EXECUTE ON FUNCTION trading.reject_append_only_mutation() TO PUBLIC"
        )
        public = read_reject_function_provenance(connection)
        assert public.classification == "PUBLIC_GRANT"
        assert public.public_execute is True
        assert public.direct_runtime_execute is False
        apply_runtime_grants(connection)
        exact = read_exact_runtime_privileges(connection)
        assert exact.missing_required == ()
        assert exact.prohibited_present == ()
        assert dict(exact.table_privileges)["shadow_position_state"] == (
            "SELECT",
            "INSERT",
            "UPDATE",
        )

        lease_store = PostgresExecutionLeaseStore(lambda: psycopg.connect(app_dsn))
        leader = lease_store.acquire(EXECUTION_LEASE_NAME, "shadow-ci-leader", 30)
        assert leader is not None
        intent_id, position_id = uuid4(), uuid4()
        now = datetime.now(UTC)

        def write_shadow(cursor: object) -> None:
            cursor.execute(
                """INSERT INTO trading.trade_intents (
                    intent_id, idempotency_key, strategy_name, epic, execution_mode,
                    lifecycle_state, intent_payload, input_fingerprint_sha256,
                    created_at, updated_at
                ) VALUES (%s, %s, 'S0', 'CS.D.EURGBP.MINI.IP', 'SHADOW_DEMO',
                    'SHADOW_INTENT_CREATED', '{}'::jsonb, %s, %s, %s)""",
                (intent_id, str(intent_id), "a" * 64, now, now),
            )
            cursor.execute(
                """INSERT INTO trading.shadow_position_state (
                    shadow_position_id, intent_id, strategy_id, instrument, direction,
                    entry_price, stop_price, target_price, opened_at, status,
                    fencing_token, created_at, updated_at
                ) VALUES (%s, %s, 'S0', 'EURGBP', 'BUY', 0.8500, 0.8490, 0.8510,
                    %s, 'OPEN', %s, %s, %s)""",
                (position_id, intent_id, now, leader.fencing_token, now, now),
            )

        lease_store.run_fenced(leader, FencedOperation.TRADE_INTENT, write_shadow)
        assert lease_store.release(leader)
        successor = lease_store.acquire(EXECUTION_LEASE_NAME, "shadow-ci-successor", 30)
        assert successor is not None and successor.fencing_token > leader.fencing_token
        with pytest.raises(FencingRejected):
            lease_store.run_fenced(
                leader,
                FencedOperation.RECONCILIATION,
                lambda cursor: cursor.execute(
                    "UPDATE trading.shadow_position_state SET status = 'CLOSED' "
                    "WHERE intent_id = %s",
                    (intent_id,),
                ),
            )
