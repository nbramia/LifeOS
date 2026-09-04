"""API tests for CRM request hygiene (#874):

- `GZipMiddleware` is scoped to `/api/crm/*` and `/api/people/*` only, so
  large list/timeline payloads shrink over the wire while streaming (SSE)
  endpoints elsewhere in the API are never touched by the gzip encoder.
- The CRM page routes (`/crm`, `/me`, `/family`, `/birthdays`,
  `/relationship`, and their sub-paths) send a short revalidation
  `Cache-Control` and honor `If-None-Match` with a bodyless 304.

These import `api.main` (`TestClient(app)`), which is a heavy, one-time app
initialization — following the precedent in `test_chat_api.py`, this module
is marked `slow` rather than `unit` so it doesn't run in the fast pre-push
gate; it still runs under `./scripts/test.sh all`/`slow`.
"""
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.slow

_CACHE_CONTROL = "max-age=60, must-revalidate"


@pytest.fixture(scope="module")
def client():
    from api.main import app
    return TestClient(app)


class TestGzipScoping:
    """GZipMiddleware is scoped to /api/crm/* and /api/people/*."""

    def test_crm_page_is_not_gzipped(self, client):
        """The static CRM page sits outside /api/crm and /api/people, so it
        must never be gzip-encoded even though it comfortably exceeds the
        1KB threshold (~750KB per the issue)."""
        response = client.get("/crm", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers.get("content-encoding") is None
        assert len(response.content) > 1024

    def test_small_crm_response_is_not_gzipped(self, client):
        """Below the 1KB minimum_size, a /api/crm/* response stays
        uncompressed even though the path is in scope."""
        response = client.get("/api/crm/statistics", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        if len(response.content) >= 1024:
            pytest.skip("statistics response unexpectedly exceeds the gzip threshold")
        assert response.headers.get("content-encoding") is None

    @pytest.mark.integration
    def test_crm_people_list_is_gzipped_when_large_enough(self, client):
        """A real people list response — ~216KB for 300 people per the
        issue — must be gzip-encoded. Skips cleanly on a fresh clone with
        no/small data/crm.db, per the standing brief's guidance for
        data-dependent assertions."""
        response = client.get("/api/crm/people?limit=300", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        if len(response.content) < 1024:
            pytest.skip("data/crm.db has too few people to exceed the gzip threshold")
        assert response.headers.get("content-encoding") == "gzip"

    @pytest.mark.integration
    def test_people_search_endpoint_is_gzipped_when_large_enough(self, client):
        """/api/people/search is also in scope."""
        response = client.get("/api/people/search?q=a&limit=50",
                               headers={"Accept-Encoding": "gzip"})
        if response.status_code != 200:
            pytest.skip(f"/api/people/search unavailable in this environment ({response.status_code})")
        if len(response.content) < 1024:
            pytest.skip("search response too small in this data set to exceed the gzip threshold")
        assert response.headers.get("content-encoding") == "gzip"

    def test_chat_stream_route_is_never_gzipped(self, client):
        """The SSE chat endpoint sits outside /api/crm and /api/people, so
        the scoped gzip middleware must never touch it — protecting the
        token-by-token streaming the chat surface depends on (see
        docs/specs/technical/client-surfaces.md)."""
        with patch('api.routes.chat.VectorStore') as mock_vs:
            mock_vs.return_value.search.return_value = []
            with patch('api.routes.chat.get_synthesizer') as mock_synth:
                async def mock_stream(*args, **kwargs):
                    yield "x" * 4000  # comfortably over the 1KB threshold
                mock_synth.return_value.stream_response = mock_stream

                response = client.post(
                    "/api/ask/stream",
                    json={"question": "test question"},
                    headers={"Accept-Encoding": "gzip"},
                )

        assert response.headers.get("content-type", "").startswith("text/event-stream")
        assert response.headers.get("content-encoding") is None


class TestCrmPageCacheControl:
    """Page routes serving crm.html send a short revalidation Cache-Control
    and honor If-None-Match with a 304 (#874)."""

    @pytest.mark.parametrize("path", [
        "/crm", "/me", "/family", "/birthdays", "/relationship",
        "/crm/some-person-id", "/me/timeline", "/family/some-tab",
        "/relationship/some-tab", "/birthdays/some-tab",
    ])
    def test_cache_control_header_present(self, client, path):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers.get("cache-control") == _CACHE_CONTROL
        assert response.headers.get("etag")

    def test_matching_if_none_match_returns_304_with_empty_body(self, client):
        first = client.get("/me")
        etag = first.headers["etag"]

        second = client.get("/me", headers={"If-None-Match": etag})

        assert second.status_code == 304
        assert second.headers.get("cache-control") == _CACHE_CONTROL
        assert second.headers.get("etag") == etag
        assert second.content == b""

    def test_stale_if_none_match_returns_full_page(self, client):
        response = client.get("/me", headers={"If-None-Match": '"not-a-real-etag"'})
        assert response.status_code == 200
        assert len(response.content) > 1024
        assert response.headers.get("cache-control") == _CACHE_CONTROL
