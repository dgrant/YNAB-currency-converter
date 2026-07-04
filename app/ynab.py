import httpx


class YNABError(Exception):
    pass


class YNABClient:
    """Minimal client for the official YNAB API (https://api.ynab.com/v1)."""

    def __init__(self, token: str, base_url: str = "https://api.ynab.com/v1") -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = self._client.get(path, params=params)
        self._raise_for_status(response)
        return response.json()["data"]

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            detail = response.json()["error"]["detail"]
        except Exception:
            detail = response.text
        raise YNABError(f"YNAB API error {response.status_code}: {detail}")

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
        response = self._client.patch(
            f"/budgets/{budget_id}/transactions",
            json={"transactions": transactions},
        )
        self._raise_for_status(response)
        return response.json()["data"]["transactions"]
