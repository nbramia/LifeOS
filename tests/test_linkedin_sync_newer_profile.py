"""sync_linkedin_to_v2 keeps stored company and position for connections whose
scraped profile is newer than the export file."""
import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from api.services.people_aggregator import sync_linkedin_to_v2

pytestmark = pytest.mark.unit


class TestLinkedInSyncPrefersNewerProfile:
    """A profile scraped after the export keeps the stored company and position."""

    EXPORT_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _sync(self, tmp_path, profiles):
        csv_path = tmp_path / "LinkedInConnections.csv"
        csv_path.write_text(
            "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
            "Jane,Example,https://www.linkedin.com/in/jane-example,,Old Co,Old Role,01 Jan 2020\n"
        )
        os.utime(csv_path, (self.EXPORT_AT.timestamp(),) * 2)
        if profiles is not None:
            (tmp_path / "linkedin_extracted.json").write_text(json.dumps({"profiles": profiles}))
        resolver = MagicMock()
        resolver.resolve_from_linkedin.return_value = None
        with patch("api.services.people_aggregator.get_source_entity_store"):
            sync_linkedin_to_v2(str(csv_path), entity_resolver=resolver)
        return resolver.resolve_from_linkedin.call_args.kwargs

    @pytest.mark.parametrize(
        "scraped_at", ["2026-06-01T12:00:00+00:00", "2026-06-01T12:00:00", "2026-06-01T12:00:00Z"]
    )
    def test_newer_profile_keeps_stored_job(self, tmp_path, scraped_at):
        profile = {"linkedin_url": "HTTPS://www.linkedin.com/in/Jane-Example/?x=1", "scraped_at": scraped_at}
        kwargs = self._sync(tmp_path, [profile])
        assert kwargs["company"] is None
        assert kwargs["position"] is None
        assert kwargs["linkedin_url"] == "https://www.linkedin.com/in/jane-example"

    def test_older_profile_lets_export_win(self, tmp_path):
        profile = {"linkedin_url": "https://www.linkedin.com/in/jane-example", "scraped_at": "2025-06-01T12:00:00"}
        kwargs = self._sync(tmp_path, [profile])
        assert kwargs["company"] == "Old Co"
        assert kwargs["position"] == "Old Role"

    def test_connection_without_profile_is_unchanged(self, tmp_path):
        profile = {"linkedin_url": "https://www.linkedin.com/in/someone-else", "scraped_at": "2026-06-01T12:00:00"}
        kwargs = self._sync(tmp_path, [profile])
        assert kwargs["company"] == "Old Co"
        assert kwargs["position"] == "Old Role"

    def test_missing_profiles_file_is_unchanged(self, tmp_path):
        kwargs = self._sync(tmp_path, None)
        assert kwargs["company"] == "Old Co"
        assert kwargs["position"] == "Old Role"

    def test_unreadable_profiles_file_is_unchanged(self, tmp_path):
        (tmp_path / "linkedin_extracted.json").write_text("{not json")
        kwargs = self._sync(tmp_path, None)
        assert kwargs["company"] == "Old Co"
