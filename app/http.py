import time

import httpx

# Transient upstream failures worth one retry on idempotent GETs.
RETRYABLE_STATUSES = {502, 503, 504}
RETRY_DELAY_SECONDS = 0.5


def get_with_retry(client: httpx.Client, path: str, params: dict | None = None) -> httpx.Response:
    """GET with a single retry on connection errors and 502/503/504."""
    try:
        response = client.get(path, params=params)
        if response.status_code not in RETRYABLE_STATUSES:
            return response
    except httpx.TransportError:
        pass
    time.sleep(RETRY_DELAY_SECONDS)
    return client.get(path, params=params)


def get_or_error(
    client: httpx.Client,
    path: str,
    params: dict | None,
    error_cls: type[Exception],
    service: str,
) -> httpx.Response:
    """get_with_retry, mapping connection failures to the caller's domain error."""
    try:
        return get_with_retry(client, path, params)
    except httpx.TransportError as exc:
        raise error_cls(f"Could not reach {service}: {exc}") from exc
