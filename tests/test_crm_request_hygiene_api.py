"""API tests for CRM request hygiene (#874):

- `GZipMiddleware` is scoped to `/api/crm/*`, `/api/people/*`, and the CRM
  page routes (`/crm`, `/me`, `/family`, `/birthdays`, `/relationship`, and
  their sub-paths) — the handful of routes with payloads large enough to be
  worth compressing. Streaming (SSE) endpoints elsewhere in the API sit
  outside this scope by construction (Starlette's `GZipMiddleware` also
  already refuses to compress `text/event-stream` on its own, independent
  of this scoping — see `api/main.py`).
- Those same CRM page routes send a short revalidation `Cache-Control` and
  honor `If-None-Match` with a bodyless 304, including the weak-validator
  (`W/"..."`) and wildcard (`*`) forms.

Most of this module imports `api.main` (`TestClient(app)`), which is a
heavy, one-time app initialization — following the precedent in
`test_chat_api.py`, marked `slow` rather than `unit` so it doesn't run in
the fast pre-push gate; it still runs under `./scripts/test.sh all`/`slow`.
`TestGzipUnitSynthetic` below is the deliberate exception: it uses the same
heavy fixture but is marked plain `unit`, because it's the only assertion
in this file that the gzip feature is ever actually applied without
depending on `data/crm.db` being present and large — everything else that
proves compression happens (`TestGzipScoping`'s two `integration`-marked
tests) skips cleanly on a fresh clone, which would otherwise leave nothing
in the default `unit`/pre-push scope asserting the headline behavior at all.
"""
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from api.services.person_entity import PersonEntity

_CACHE_CONTROL = "max-age=60, must-revalidate"


@pytest.fixture(scope="module")
def client():
    from api.main import app
    return TestClient(app)


def _make_synthetic_people(count: int) -> list[PersonEntity]:
    """Obviously-synthetic PersonEntity fixtures with long enough names/
    emails that `count` of them comfortably exceed the gzip 1KB threshold
    once serialized, regardless of what real data (if any) exists."""
    people = []
    for i in range(count):
        p = PersonEntity(
            id=f"synthetic-gzip-test-person-{i:04d}",
            canonical_name=f"Synthetic Gzip Test Person Number {i:04d}",
            emails=[f"synthetic-gzip-test-{i:04d}@example.test"],
            company="Example Synthetic Test Company",
        )
        p.relationship_strength = float(count - i)
        people.append(p)
    return people


class TestGzipUnitSynthetic:
    """Data-independent positive gzip assertion (runs in the `unit` scope,
    not `slow` — see module docstring)."""

    pytestmark = pytest.mark.unit

    def test_synthetic_large_people_list_is_gzipped(self, client):
        """Patches the CRM people store with enough obviously-synthetic
        people that GET /api/crm/people deterministically exceeds the gzip
        threshold, so this assertion holds on a completely fresh clone with
        no `data/` directory at all."""
        people = _make_synthetic_people(60)
        mock_person_store = MagicMock()
        mock_person_store.get_all.return_value = people
        mock_source_store = MagicMock()
        mock_source_store.get_for_people_batch.return_value = {}

        with patch("api.routes.crm.get_person_entity_store", return_value=mock_person_store), \
             patch("api.routes.crm.get_source_entity_store", return_value=mock_source_store):
            response = client.get("/api/crm/people?limit=100", headers={"Accept-Encoding": "gzip"})

        assert response.status_code == 200
        assert len(response.content) > 1024, (
            "fixture must exceed the gzip threshold deterministically — "
            f"got {len(response.content)} bytes for {len(people)} synthetic people"
        )
        assert response.headers.get("content-encoding") == "gzip"


class TestGzipScoping:
    """GZipMiddleware is scoped to /api/crm/*, /api/people/*, and the CRM
    page routes."""

    pytestmark = pytest.mark.slow

    def test_crm_page_is_gzipped(self, client):
        """The CRM page (~750KB per the issue, the largest single payload
        this scope covers) is compressed."""
        response = client.get("/crm", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers.get("content-encoding") == "gzip"

    def test_crm_page_subpath_is_gzipped(self, client):
        """A client-side-routed sub-path of a page route (e.g. a person's
        timeline URL) is in scope too, not just the bare route."""
        response = client.get("/me/timeline", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers.get("content-encoding") == "gzip"

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
        data-dependent assertions. (TestGzipUnitSynthetic above is the
        data-independent version of this same assertion.)"""
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
        """The SSE chat endpoint sits outside every gzip-scoped prefix, so
        this middleware never touches it — belt-and-suspenders alongside
        Starlette's own refusal to compress `text/event-stream` responses
        at all (`DEFAULT_EXCLUDED_CONTENT_TYPES` in
        `starlette.middleware.gzip`, independent of this app's scoping)."""
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

    pytestmark = pytest.mark.slow

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

    def test_weak_if_none_match_returns_304(self, client):
        """An intermediary may weaken a strong ETag to `W/"..."` in transit
        (RFC 9110 §8.8.3) — that still means the client's copy is current
        for a static file that only ever changes by full replacement."""
        first = client.get("/me")
        etag = first.headers["etag"]

        second = client.get("/me", headers={"If-None-Match": f"W/{etag}"})

        assert second.status_code == 304
        assert second.content == b""

    def test_wildcard_if_none_match_returns_304(self, client):
        """`If-None-Match: *` matches any current representation."""
        response = client.get("/me", headers={"If-None-Match": "*"})
        assert response.status_code == 304
        assert response.content == b""

    def test_stale_if_none_match_returns_full_page(self, client):
        response = client.get("/me", headers={"If-None-Match": '"not-a-real-etag"'})
        assert response.status_code == 200
        assert len(response.content) > 1024
        assert response.headers.get("cache-control") == _CACHE_CONTROL
