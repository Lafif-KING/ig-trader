"""PostgreSQL execution lease, fencing, and managed-identity connection boundary."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)

EXECUTION_LEASE_NAME = "execution-worker"
POSTGRES_ENTRA_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
POSTGRES_PRINCIPAL_NAME = "igtrdevfrc-execution-identity"

_INSTANCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_POSTGRES_HOST_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\.postgres\.database\.azure\.com\Z"
)
_PASSWORD_ENVIRONMENT_NAMES = {
    "DATABASE_URL",
    "PGPASSWORD",
    "POSTGRES_PASSWORD",
    "POSTGRESQL_PASSWORD",
}


class RuntimeRole(StrEnum):
    """Observable role of one cloud runtime process."""

    LEADER = "LEADER"
    STANDBY = "STANDBY"
    NO_EXECUTION = "NO_EXECUTION"


class LeaseState(StrEnum):
    """Fail-closed lease lifecycle exposed in structured telemetry."""

    DISABLED = "DISABLED"
    ACQUIRED = "ACQUIRED"
    STANDBY = "STANDBY"
    LOST = "LOST"
    RELEASED = "RELEASED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"


class LeaseHeartbeatState(StrEnum):
    """Result of the latest lease heartbeat action."""

    DISABLED = "DISABLED"
    NOT_STARTED = "NOT_STARTED"
    HEALTHY = "HEALTHY"
    FAILED = "FAILED"


class FencedOperation(StrEnum):
    """State-changing scopes that require the current lease fence."""

    CYCLE_OWNERSHIP = "cycle_ownership"
    TRADE_INTENT = "trade_intent"
    BROKER_SUBMISSION = "broker_submission"
    RECONCILIATION = "reconciliation"


class LeaseError(RuntimeError):
    """Base class for sanitized execution-lease failures."""


class LeaseDatabaseError(LeaseError):
    """PostgreSQL state is unavailable or ambiguous, so work must stop."""


class FencingRejected(LeaseError):
    """The supplied lease fence is stale, expired, or cannot be proven current."""


class StatefulWorkProhibited(LeaseError):
    """The process has no current stateful-work authority."""


class UnsafePostgresConfiguration(LeaseError):
    """Managed-identity PostgreSQL configuration is absent or unsafe."""


@dataclass(frozen=True)
class LeaseRecord:
    """One proven lease generation returned by PostgreSQL."""

    lease_name: str
    holder_instance_id: str
    fencing_token: int
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class LeaseStatus:
    """Secret-free structured lease and replica-role evidence."""

    replica_instance_id: str
    runtime_role: RuntimeRole
    lease_name: str
    lease_holder: bool
    lease_state: LeaseState
    fencing_token: int | None
    lease_heartbeat_state: LeaseHeartbeatState
    authorized: bool

    def document(self) -> dict[str, str | int | bool | None]:
        return {
            "authorized": self.authorized,
            "fencing_token": self.fencing_token,
            "lease_heartbeat_state": self.lease_heartbeat_state.value,
            "lease_holder": self.lease_holder,
            "lease_name": self.lease_name,
            "lease_state": self.lease_state.value,
            "replica_instance_id": self.replica_instance_id,
            "runtime_role": self.runtime_role.value,
        }


_T = TypeVar("_T")


class ExecutionLeaseStore(Protocol):
    """Atomic PostgreSQL lease operations used by the coordinator."""

    def acquire(
        self,
        lease_name: str,
        holder_instance_id: str,
        ttl_seconds: float,
    ) -> LeaseRecord | None: ...

    def renew(self, lease: LeaseRecord, ttl_seconds: float) -> LeaseRecord | None: ...

    def release(self, lease: LeaseRecord) -> bool: ...

    def run_fenced(
        self,
        lease: LeaseRecord,
        operation: FencedOperation,
        callback: Callable[[Any], _T],
    ) -> _T: ...


class PostgresExecutionLeaseStore:
    """Use a single PostgreSQL row as the atomic execution lease authority."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    def acquire(
        self,
        lease_name: str,
        holder_instance_id: str,
        ttl_seconds: float,
    ) -> LeaseRecord | None:
        _validate_lease_input(lease_name, holder_instance_id, ttl_seconds)
        statement = """
            SELECT lease_name, owner_instance, fencing_token,
                   acquired_at, heartbeat_at, lease_until
            FROM trading.acquire_execution_lease(%s, %s, %s)
        """
        try:
            with (
                self._connection_factory() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    statement,
                    (lease_name, holder_instance_id, ttl_seconds),
                )
                row = cursor.fetchone()
            return _lease_record(row)
        except Exception:
            raise LeaseDatabaseError("PostgreSQL lease acquisition failed closed") from None

    def renew(self, lease: LeaseRecord, ttl_seconds: float) -> LeaseRecord | None:
        _validate_lease_input(lease.lease_name, lease.holder_instance_id, ttl_seconds)
        statement = """
            SELECT lease_name, owner_instance, fencing_token,
                   acquired_at, heartbeat_at, lease_until
            FROM trading.renew_execution_lease(%s, %s, %s, %s)
        """
        try:
            with (
                self._connection_factory() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    statement,
                    (
                        lease.lease_name,
                        lease.holder_instance_id,
                        lease.fencing_token,
                        ttl_seconds,
                    ),
                )
                row = cursor.fetchone()
            return _lease_record(row)
        except Exception:
            raise LeaseDatabaseError("PostgreSQL lease renewal failed closed") from None

    def release(self, lease: LeaseRecord) -> bool:
        try:
            with (
                self._connection_factory() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    "SELECT trading.release_execution_lease(%s, %s, %s)",
                    (
                        lease.lease_name,
                        lease.holder_instance_id,
                        lease.fencing_token,
                    ),
                )
                row = cursor.fetchone()
                released = bool(row and row[0])
            return released
        except Exception:
            raise LeaseDatabaseError("PostgreSQL lease release failed closed") from None

    def run_fenced(
        self,
        lease: LeaseRecord,
        operation: FencedOperation,
        callback: Callable[[Any], _T],
    ) -> _T:
        """Validate and hold the lease-row lock through one state-changing transaction."""

        try:
            with (
                self._connection_factory() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    "SELECT trading.assert_execution_fence(%s, %s, %s, %s)",
                    (
                        lease.lease_name,
                        lease.holder_instance_id,
                        lease.fencing_token,
                        operation.value,
                    ),
                )
                return callback(cursor)
        except Exception:
            raise FencingRejected("execution fencing validation failed closed") from None


class ExecutionLeaseCoordinator:
    """Convert lease outcomes into exactly one LEADER or fail-closed STANDBY."""

    def __init__(
        self,
        *,
        store: ExecutionLeaseStore,
        replica_instance_id: str,
        execution_enabled: bool,
        ttl_seconds: float = 15.0,
        lease_name: str = EXECUTION_LEASE_NAME,
    ) -> None:
        _validate_lease_input(lease_name, replica_instance_id, ttl_seconds)
        self.store = store
        self.replica_instance_id = replica_instance_id
        self.execution_enabled = execution_enabled
        self.ttl_seconds = ttl_seconds
        self.lease_name = lease_name
        self.role = RuntimeRole.STANDBY if execution_enabled else RuntimeRole.NO_EXECUTION
        self.lease_state = LeaseState.STANDBY if execution_enabled else LeaseState.DISABLED
        self.heartbeat_state = (
            LeaseHeartbeatState.NOT_STARTED if execution_enabled else LeaseHeartbeatState.DISABLED
        )
        self.lease: LeaseRecord | None = None

    @property
    def authorized(self) -> bool:
        return self.execution_enabled and self.role is RuntimeRole.LEADER and self.lease is not None

    def status(self) -> LeaseStatus:
        return LeaseStatus(
            replica_instance_id=self.replica_instance_id,
            runtime_role=self.role,
            lease_name=self.lease_name,
            lease_holder=self.authorized,
            lease_state=self.lease_state,
            fencing_token=self.lease.fencing_token if self.lease else None,
            lease_heartbeat_state=self.heartbeat_state,
            authorized=self.authorized,
        )

    def try_acquire(self) -> bool:
        if not self.execution_enabled:
            return False
        try:
            lease = self.store.acquire(
                self.lease_name,
                self.replica_instance_id,
                self.ttl_seconds,
            )
        except LeaseDatabaseError:
            self._demote(LeaseState.DATABASE_UNAVAILABLE)
            return False
        if lease is None:
            self._demote(LeaseState.STANDBY)
            return False
        self.lease = lease
        self.role = RuntimeRole.LEADER
        self.lease_state = LeaseState.ACQUIRED
        self.heartbeat_state = LeaseHeartbeatState.HEALTHY
        logger.info("execution_lease_acquired", **self.status().document())
        return True

    def renew(self) -> bool:
        if not self.authorized or self.lease is None:
            self._demote(LeaseState.LOST)
            return False
        try:
            renewed = self.store.renew(self.lease, self.ttl_seconds)
        except LeaseDatabaseError:
            self._demote(LeaseState.DATABASE_UNAVAILABLE)
            return False
        if renewed is None:
            self._demote(LeaseState.LOST)
            return False
        self.lease = renewed
        self.heartbeat_state = LeaseHeartbeatState.HEALTHY
        logger.info("execution_lease_renewed", **self.status().document())
        return True

    def release(self) -> bool:
        lease = self.lease
        if lease is None:
            self._demote(LeaseState.RELEASED)
            return False
        try:
            released = self.store.release(lease)
        except LeaseDatabaseError:
            self._demote(LeaseState.DATABASE_UNAVAILABLE)
            return False
        self._demote(LeaseState.RELEASED)
        return released

    def run_state_change(
        self,
        operation: FencedOperation,
        callback: Callable[[Any], _T],
    ) -> _T:
        lease = self.lease
        if not self.authorized or lease is None:
            raise StatefulWorkProhibited("stateful work requires the current execution lease")
        try:
            return self.store.run_fenced(lease, operation, callback)
        except (FencingRejected, LeaseDatabaseError):
            logger.error(
                "stale_fencing_token_rejected",
                operation_scope=operation.value,
                **self.status().document(),
            )
            self._demote(LeaseState.LOST)
            raise StatefulWorkProhibited(
                "execution lease was lost; stateful work stopped"
            ) from None

    def _demote(self, state: LeaseState) -> None:
        self.lease = None
        self.role = RuntimeRole.STANDBY if self.execution_enabled else RuntimeRole.NO_EXECUTION
        self.lease_state = state if self.execution_enabled else LeaseState.DISABLED
        if not self.execution_enabled:
            self.heartbeat_state = LeaseHeartbeatState.DISABLED
        elif state in {LeaseState.LOST, LeaseState.DATABASE_UNAVAILABLE}:
            self.heartbeat_state = LeaseHeartbeatState.FAILED
        else:
            self.heartbeat_state = LeaseHeartbeatState.NOT_STARTED
        if state is LeaseState.STANDBY:
            logger.info("execution_lease_standby", **self.status().document())
        elif state is LeaseState.RELEASED:
            logger.info("execution_lease_released", **self.status().document())
        else:
            logger.warning("execution_lease_fail_closed", **self.status().document())


@dataclass(frozen=True)
class ManagedIdentityPostgresConfig:
    """Passwordless Azure PostgreSQL connection fields; no DSN is accepted."""

    host: str
    database: str
    user: str
    client_id: str
    port: int = 5432
    connect_timeout_seconds: int = 5

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
    ) -> ManagedIdentityPostgresConfig:
        if any(environment.get(name, "").strip() for name in _PASSWORD_ENVIRONMENT_NAMES):
            raise UnsafePostgresConfiguration("passwords and database URLs are prohibited")
        host = environment.get("POSTGRES_HOST", "").strip().casefold()
        database = environment.get("POSTGRES_DATABASE", "ig_trader").strip()
        user = environment.get("POSTGRES_USER", POSTGRES_PRINCIPAL_NAME).strip()
        client_id = environment.get("AZURE_CLIENT_ID", "").strip()
        if not _POSTGRES_HOST_PATTERN.fullmatch(host):
            raise UnsafePostgresConfiguration("POSTGRES_HOST must be an Azure PostgreSQL FQDN")
        if database != "ig_trader":
            raise UnsafePostgresConfiguration("only the ig_trader database is accepted")
        if user != POSTGRES_PRINCIPAL_NAME:
            raise UnsafePostgresConfiguration(
                "the execution managed-identity principal is required"
            )
        try:
            UUID(client_id)
        except ValueError:
            raise UnsafePostgresConfiguration(
                "the user-assigned identity client ID is required"
            ) from None
        return cls(host=host, database=database, user=user, client_id=client_id)


class ManagedIdentityPostgresTokenProvider:
    """Reuse Azure Identity token caching and return tokens only in process memory."""

    def __init__(self, client_id: str) -> None:
        from azure.identity import ManagedIdentityCredential

        self._credential = ManagedIdentityCredential(client_id=client_id)

    def get_token(self) -> str:
        try:
            token = self._credential.get_token(POSTGRES_ENTRA_SCOPE).token
        except Exception:
            raise LeaseDatabaseError("managed-identity token acquisition failed closed") from None
        if not token:
            raise LeaseDatabaseError("managed-identity token acquisition failed closed")
        return token


class ManagedIdentityPostgresConnectionFactory:
    """Open TLS PostgreSQL sessions with an in-memory Entra token as the password."""

    def __init__(
        self,
        config: ManagedIdentityPostgresConfig,
        token_provider: ManagedIdentityPostgresTokenProvider | None = None,
    ) -> None:
        self.config = config
        self._token_provider = token_provider or ManagedIdentityPostgresTokenProvider(
            config.client_id
        )

    def __call__(self) -> Any:
        try:
            import psycopg

            token = self._token_provider.get_token()
            return psycopg.connect(
                host=self.config.host,
                port=self.config.port,
                dbname=self.config.database,
                user=self.config.user,
                password=token,
                sslmode="require",
                connect_timeout=self.config.connect_timeout_seconds,
                application_name="ig-trader-execution-lease",
            )
        except Exception:
            raise LeaseDatabaseError(
                "managed-identity PostgreSQL connection failed closed"
            ) from None


def no_execution_lease_status(replica_instance_id: str) -> LeaseStatus:
    """Return explicit structured proof that NO_EXECUTION has no lease authority."""

    _validate_instance_id(replica_instance_id)
    return LeaseStatus(
        replica_instance_id=replica_instance_id,
        runtime_role=RuntimeRole.NO_EXECUTION,
        lease_name=EXECUTION_LEASE_NAME,
        lease_holder=False,
        lease_state=LeaseState.DISABLED,
        fencing_token=None,
        lease_heartbeat_state=LeaseHeartbeatState.DISABLED,
        authorized=False,
    )


def _lease_record(row: Any) -> LeaseRecord | None:
    if row is None:
        return None
    return LeaseRecord(
        lease_name=str(row[0]),
        holder_instance_id=str(row[1]),
        fencing_token=int(row[2]),
        acquired_at=row[3],
        heartbeat_at=row[4],
        expires_at=row[5],
    )


def _validate_lease_input(
    lease_name: str,
    holder_instance_id: str,
    ttl_seconds: float,
) -> None:
    if lease_name != EXECUTION_LEASE_NAME:
        raise ValueError("only the accepted execution-worker lease is supported")
    _validate_instance_id(holder_instance_id)
    if not 1 <= ttl_seconds <= 300:
        raise ValueError("lease TTL must be within 1..300 seconds")


def _validate_instance_id(instance_id: str) -> None:
    if not _INSTANCE_PATTERN.fullmatch(instance_id):
        raise ValueError("replica instance identity is invalid")
