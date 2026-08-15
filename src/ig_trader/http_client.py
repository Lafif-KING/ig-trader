"""HTTP client with retry logic and structured logging."""

import ssl
from typing import Any

import httpx
import structlog
import truststore

logger = structlog.get_logger(__name__)


def build_system_ssl_context() -> ssl.SSLContext:
    """Return a hostname-checking TLS client context backed by system trust."""

    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class HTTPClient:
    """HTTP client with retry logic, logging, and error handling."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        """
        Initialize HTTP client.

        Args:
            base_url: Base URL for API (e.g. https://demo-api.ig.com)
            api_key: IG API key for X-IG-API-KEY header
            timeout: Request timeout in seconds
            max_retries: Maximum number of retries on failure
        """
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"X-IG-API-KEY": api_key},
        )

    def get(self, endpoint: str, **kwargs: Any) -> httpx.Response:
        """GET request with retry logic."""
        return self._request("GET", endpoint, **kwargs)

    def post(self, endpoint: str, **kwargs: Any) -> httpx.Response:
        """POST request with retry logic."""
        return self._request("POST", endpoint, **kwargs)

    def put(self, endpoint: str, **kwargs: Any) -> httpx.Response:
        """PUT request with retry logic."""
        return self._request("PUT", endpoint, **kwargs)

    def delete(self, endpoint: str, **kwargs: Any) -> httpx.Response:
        """DELETE request with retry logic."""
        return self._request("DELETE", endpoint, **kwargs)

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
        """
        Make HTTP request with exponential backoff retry logic.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (e.g. /session)
            **kwargs: Additional args for httpx.request

        Returns:
            httpx.Response object

        Raises:
            httpx.HTTPError: If all retries fail
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(
                    "http_request_start",
                    method=method,
                    endpoint=endpoint,
                    attempt=attempt,
                    max_retries=self.max_retries,
                )

                response = self.client.request(method, endpoint, **kwargs)

                logger.info(
                    "http_response",
                    method=method,
                    endpoint=endpoint,
                    status_code=response.status_code,
                    attempt=attempt,
                )

                return response

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                logger.warning(
                    "http_request_failed",
                    method=method,
                    endpoint=endpoint,
                    attempt=attempt,
                    error=str(e),
                )
                if attempt == self.max_retries:
                    logger.error(
                        "http_request_exhausted_retries",
                        method=method,
                        endpoint=endpoint,
                        error=str(e),
                    )
                    raise

        raise RuntimeError("Unexpected: request loop completed without return or raise")
