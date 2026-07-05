import httpx

from .http import get_or_error


class YNABError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# One pooled httpx client shared by every user's YNABClient (tokens differ per
# user, so authorization is a per-request header, not a client default).
_pooled_client: httpx.Client | None = None


def pooled_client(base_url: str) -> httpx.Client:
    global _pooled_client
    if _pooled_client is None:
        _pooled_client = httpx.Client(base_url=base_url, timeout=30)
    return _pooled_client


class YNABClient:
    """Minimal client for the official YNAB API (https://api.ynab.com/v1)."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.ynab.com/v1",
        client: httpx.Client | None = None,
    ) -> None:
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client if client is not None else pooled_client(base_url)

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = get_or_error(
            self._client, path, params, YNABError, "YNAB", headers=self._headers
        )
        self._raise_for_status(response)
        return response.json()["data"]

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code == 429:
            raise YNABError(
                "YNAB is rate limiting this token (the API allows about 200 "
                "requests per hour).",
                status_code=429,
            )
        try:
            detail = response.json()["error"]["detail"]
        except Exception:
            detail = response.text
        raise YNABError(
            f"YNAB API error {response.status_code}: {detail}",
            status_code=response.status_code,
        )

    def get_budgets(self) -> list[dict]:
        return self._get("/budgets")["budgets"]

    def get_accounts(self, budget_id: str) -> list[dict]:
        accounts = self._get(f"/budgets/{budget_id}/accounts")["accounts"]
        return [a for a in accounts if not a["deleted"] and not a["closed"]]

    def get_transactions(self, budget_id: str, account_id: str, since_date: str) -> list[dict]:
        data = self._get(
            f"/budgets/{budget_id}/accounts/{account_id}/transactions",
            params={"since_date": since_date},
        )
        return [t for t in data["transactions"] if not t["deleted"]]

    def update_transactions(self, budget_id: str, transactions: list[dict]) -> list[dict]:
        # PATCH is not idempotent, so no retry here — only friendly wrapping.
        try:
            response = self._client.patch(
                f"/budgets/{budget_id}/transactions",
                json={"transactions": transactions},
                headers=self._headers,
            )
        except httpx.TransportError as exc:
            raise YNABError(f"Could not reach YNAB: {exc}") from exc
        self._raise_for_status(response)
        return response.json()["data"]["transactions"]
