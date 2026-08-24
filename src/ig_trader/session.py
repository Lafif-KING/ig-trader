"""IG API session manager with automatic token refresh."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.ig_trader.config import settings
from src.ig_trader.http_client import HTTPClient

logger = structlog.get_logger(__name__)


class SessionManager:
    """Manages IG API sessions: login, logout, token refresh."""

    def __init__(
        self,
        *,
        request_observer: Callable[[str, str], None] | None = None,
        response_error_observer: Callable[[Any], None] | None = None,
    ) -> None:
        """Initialize session manager."""
        self.cst: str | None = None
        self.x_security_token: str | None = None
        self.account_id: str | None = None
        self.lightstreamer_endpoint: str | None = None
        self.token_expiry: datetime | None = None
        self._response_error_observer = response_error_observer
        self.http_client = HTTPClient(
            base_url=settings.ig_base_url,
            api_key=settings.ig_api_key,
            request_observer=request_observer,
        )

    def login(self) -> bool:
        """Login to IG API and obtain session tokens."""
        if not settings.ig_identifier or not settings.ig_password:
            logger.error(
                "login_failed_missing_credentials",
                reason="IG_IDENTIFIER or IG_PASSWORD not set",
            )
            raise ValueError("IG_IDENTIFIER and IG_PASSWORD must be set in .env")

        logger.info("login_start")

        response = self.http_client.post(
            "session",
            json={
                "identifier": settings.ig_identifier,
                "password": settings.ig_password,
            },
            headers={"VERSION": "2"},
        )

        if response.status_code == 200:
            self.cst = response.headers.get("CST")
            self.x_security_token = response.headers.get("X-SECURITY-TOKEN")
            data = response.json()
            self.account_id = data.get("currentAccountId")
            endpoint = data.get("lightstreamerEndpoint")
            self.lightstreamer_endpoint = (
                endpoint if isinstance(endpoint, str) and endpoint else None
            )
            self.token_expiry = datetime.now(UTC) + timedelta(
                seconds=settings.session_timeout_seconds
            )

            logger.info(
                "login_success",
                token_expiry=self.token_expiry.isoformat(),
            )
            return True
        else:
            if response.status_code == 403 and self._response_error_observer:
                self._response_error_observer(response)
            logger.error(
                "login_failed",
                status_code=response.status_code,
                reason="broker rejected authentication",
            )
            return False

    def logout(self) -> bool:
        """Logout from IG API."""
        if not self.is_authenticated():
            logger.warning("logout_called_but_not_authenticated")
            return False

        logger.info("logout_start")
        response = self.http_client.delete(
            "session",
            headers=self._get_auth_headers(),
        )

        if response.status_code in (200, 204):
            self.cst = None
            self.x_security_token = None
            self.account_id = None
            self.lightstreamer_endpoint = None
            self.token_expiry = None
            logger.info("logout_success")
            return True
        return False

    def is_authenticated(self) -> bool:
        """Check if session is currently authenticated and not expired."""
        if not (self.cst and self.x_security_token and self.token_expiry):
            return False
        return datetime.now(UTC) < self.token_expiry

    def authorized_request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        """Make a request with authentication headers added automatically."""
        if not self.is_authenticated() and not self.login():
            raise RuntimeError("Authentication failed during authorized_request")

        headers = kwargs.pop("headers", {})
        headers.update(self._get_auth_headers())

        return self.http_client._request(method, endpoint.lstrip("/"), headers=headers, **kwargs)

    def _get_auth_headers(self) -> dict:
        """Get authorization headers for authenticated requests."""
        return {
            "CST": self.cst or "",
            "X-SECURITY-TOKEN": self.x_security_token or "",
        }
