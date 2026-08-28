"""
Tests for the Monarch transactions endpoint's optional ascending/descending
sort order (#779).

The underlying monarchmoney client always fetches with offset=0 and a fixed
server-side ordering with no direction control, so naively sorting a
limit-truncated fetch client-side would just reorder whatever arbitrary
subset the server handed back — not the true oldest/newest transactions in
the full requested range. MonarchClient.get_transactions works around this
by probing the total match count, fetching everything, sorting, then
applying the caller's limit — only when sort_order is explicitly requested.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from api.main import app
from api.services.monarch import MonarchClient

pytestmark = pytest.mark.unit


def _txn(id_, date_str, amount=-10.0):
    return {
        "id": id_,
        "date": date_str,
        "amount": amount,
        "merchant": {"name": f"Merchant {id_}"},
        "category": {"name": "General"},
        "account": {"displayName": "Checking"},
        "tags": [],
        "pending": False,
        "isRecurring": False,
        "isSplitTransaction": False,
        "notes": "",
    }


@pytest.fixture
def client():
    """MonarchClient with its inner monarchmoney client stubbed out.

    Setting `_mm` directly short-circuits `_get_client()`'s auth/session
    logic, same as an already-authenticated instance.
    """
    c = MonarchClient()
    c._mm = AsyncMock()
    return c


class TestDefaultOrderUnchanged:
    """No sort_order supplied -> existing behavior, byte-for-byte."""

    async def test_no_sort_param_makes_a_single_call_and_preserves_order(self, client):
        txns = [_txn("3", "2026-01-03"), _txn("1", "2026-01-01"), _txn("2", "2026-01-02")]
        client._mm.get_transactions.return_value = {
            "allTransactions": {"totalCount": 3, "results": txns}
        }

        result = await client.get_transactions(limit=100)

        assert client._mm.get_transactions.call_count == 1
        assert [t["id"] for t in result] == ["3", "1", "2"]


class TestAscendingSort:
    """sort_order="asc" must surface the true oldest transactions in the
    full matching range, not just the first `limit` the server happened to
    return before sorting."""

    async def test_ascending_returns_oldest_first_across_full_range(self, client):
        all_txns = [
            _txn("5", "2026-01-05"),
            _txn("3", "2026-01-03"),
            _txn("1", "2026-01-01"),
            _txn("4", "2026-01-04"),
            _txn("2", "2026-01-02"),
        ]
        probe_response = {"allTransactions": {"totalCount": 5, "results": all_txns[:1]}}
        full_response = {"allTransactions": {"totalCount": 5, "results": all_txns}}
        client._mm.get_transactions.side_effect = [probe_response, full_response]

        # Only 2 requested, but the 2 true oldest must come back even though
        # they weren't first in the server's raw order.
        result = await client.get_transactions(limit=2, sort_order="asc")

        assert [t["id"] for t in result] == ["1", "2"]
        assert client._mm.get_transactions.call_count == 2
        probe_kwargs = client._mm.get_transactions.call_args_list[0].kwargs
        full_kwargs = client._mm.get_transactions.call_args_list[1].kwargs
        assert probe_kwargs["limit"] == 1
        assert full_kwargs["limit"] == 5


class TestDescendingSort:
    async def test_descending_returns_newest_first_across_full_range(self, client):
        all_txns = [
            _txn("5", "2026-01-05"),
            _txn("1", "2026-01-01"),
            _txn("3", "2026-01-03"),
        ]
        probe_response = {"allTransactions": {"totalCount": 3, "results": all_txns[:1]}}
        full_response = {"allTransactions": {"totalCount": 3, "results": all_txns}}
        client._mm.get_transactions.side_effect = [probe_response, full_response]

        result = await client.get_transactions(limit=2, sort_order="desc")

        assert [t["id"] for t in result] == ["5", "3"]


class TestLimitCapUnaffectedBySort:
    """The caller's requested output count still applies after sorting,
    regardless of sort direction."""

    async def test_ascending_respects_the_output_limit(self, client):
        all_txns = [_txn(str(i), f"2026-01-{i:02d}") for i in range(1, 6)]
        probe_response = {"allTransactions": {"totalCount": 5, "results": all_txns[:1]}}
        full_response = {"allTransactions": {"totalCount": 5, "results": all_txns}}
        client._mm.get_transactions.side_effect = [probe_response, full_response]

        result = await client.get_transactions(limit=3, sort_order="asc")

        assert len(result) == 3
        assert [t["id"] for t in result] == ["1", "2", "3"]


# These tests exercise the real FastAPI route (query-param validation
# happens at the request layer, not in a bare function call), so they use
# TestClient like the existing calendar route tests.
route_client = TestClient(app)


class TestTransactionsRouteSortParam:
    def test_invalid_sort_value_is_a_validation_error(self):
        # This app's global RequestValidationError handler (api/main.py)
        # normalizes all query-validation failures to 400, not raw 422 —
        # the point here is that it's rejected outright, not silently
        # defaulted to the current order.
        response = route_client.get("/api/monarch/transactions?sort=bogus")
        assert response.status_code == 400
        body = response.json()
        assert any("sort" in str(err.get("loc", [])) for err in body["detail"])

    def test_valid_sort_value_threads_through_to_the_client(self):
        mock_client = MagicMock()
        mock_client.get_transactions = AsyncMock(return_value=[])
        with patch("api.routes.monarch.get_monarch_client", return_value=mock_client):
            response = route_client.get("/api/monarch/transactions?sort=asc")

        assert response.status_code == 200
        mock_client.get_transactions.assert_called_once()
        assert mock_client.get_transactions.call_args.kwargs["sort_order"] == "asc"

    def test_omitted_sort_defaults_to_none(self):
        mock_client = MagicMock()
        mock_client.get_transactions = AsyncMock(return_value=[])
        with patch("api.routes.monarch.get_monarch_client", return_value=mock_client):
            response = route_client.get("/api/monarch/transactions")

        assert response.status_code == 200
        assert mock_client.get_transactions.call_args.kwargs["sort_order"] is None
