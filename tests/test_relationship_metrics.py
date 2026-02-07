"""Tests for relationship metrics calculation."""
import pytest
from datetime import datetime, timedelta, timezone

from api.services.relationship_metrics import (
    compute_recency_score,
    compute_frequency_score,
    compute_diversity_score,
    compute_relationship_strength,
    RECENCY_WINDOW_DAYS,
    FREQUENCY_TARGET,
)


class TestRecencyScore:
    """Tests for recency score calculation."""

    def test_none_last_seen(self):
        """Test with no last_seen date."""
        score = compute_recency_score(None)
        assert score == 0.0

    def test_today(self):
        """Test with last seen today."""
        now = datetime.now(timezone.utc)
        score = compute_recency_score(now)
        assert score == 1.0

    def test_yesterday(self):
        """Test with last seen yesterday."""
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        score = compute_recency_score(yesterday)
        # Should be close to 1 - 1/90
        assert 0.98 < score < 1.0

    def test_half_window(self):
        """Test at half the recency window."""
        half_window = datetime.now(timezone.utc) - timedelta(days=RECENCY_WINDOW_DAYS / 2)
        score = compute_recency_score(half_window)
        assert 0.45 < score < 0.55  # Close to 0.5

    def test_at_window_boundary(self):
        """Test at the window boundary."""
        boundary = datetime.now(timezone.utc) - timedelta(days=RECENCY_WINDOW_DAYS)
        score = compute_recency_score(boundary)
        assert score == 0.0

    def test_beyond_window(self):
        """Test beyond the window."""
        old = datetime.now(timezone.utc) - timedelta(days=RECENCY_WINDOW_DAYS + 30)
        score = compute_recency_score(old)
        assert score == 0.0

    def test_naive_datetime(self):
        """Test with naive (no timezone) datetime."""
        naive = datetime.now() - timedelta(days=1)
        score = compute_recency_score(naive)
        assert 0.98 < score < 1.0


class TestFrequencyScore:
    """Tests for frequency score calculation."""

    def test_zero_interactions(self):
        """Test with no interactions."""
        score = compute_frequency_score(0)
        assert score == 0.0

    def test_negative_interactions(self):
        """Test with negative (invalid) count."""
        score = compute_frequency_score(-5)
        assert score == 0.0

    def test_one_interaction(self):
        """Test with single interaction."""
        score = compute_frequency_score(1)
        # With logarithmic scaling, 1 interaction gives a small but non-zero score
        # log(2) / log(FREQUENCY_TARGET + 1) ≈ 0.125 for target=250
        assert 0.05 < score < 0.20

    def test_half_target(self):
        """Test at half the target."""
        score = compute_frequency_score(FREQUENCY_TARGET // 2)
        # With logarithmic scaling, half target gives ~0.87 (log compresses high values)
        assert 0.80 < score < 0.95

    def test_at_target(self):
        """Test at exactly the target."""
        score = compute_frequency_score(FREQUENCY_TARGET)
        assert score == 1.0

    def test_above_target(self):
        """Test above the target (capped at 1.0)."""
        score = compute_frequency_score(FREQUENCY_TARGET * 2)
        assert score == 1.0


class TestDiversityScore:
    """Tests for diversity score calculation."""

    def test_no_sources(self):
        """Test with no sources."""
        score = compute_diversity_score([])
        assert score == 0.0

    def test_single_source(self):
        """Test with single source."""
        score = compute_diversity_score(["gmail"])
        # Should be small (1/total_sources)
        assert 0.0 < score < 0.2

    def test_multiple_sources(self):
        """Test with multiple sources."""
        score = compute_diversity_score(["gmail", "calendar", "slack"])
        assert score > 0.2

    def test_duplicate_sources(self):
        """Test that duplicates are counted once."""
        score_with_dups = compute_diversity_score(["gmail", "gmail", "gmail"])
        score_single = compute_diversity_score(["gmail"])
        assert score_with_dups == score_single


class TestRelationshipStrength:
    """Tests for overall relationship strength calculation."""

    def test_zero_everything(self):
        """Test with all zeros."""
        strength = compute_relationship_strength(
            last_seen=None,
            interaction_count=0,
            sources=[],
        )
        assert strength == 0.0

    def test_recent_active_diverse(self):
        """Test with high scores in all components."""
        now = datetime.now(timezone.utc)
        strength = compute_relationship_strength(
            last_seen=now,
            interaction_count=FREQUENCY_TARGET,
            sources=["gmail", "calendar", "slack", "imessage", "vault"],
        )
        # Should be close to 100 (scale is 0-100)
        assert strength > 80

    def test_old_but_active(self):
        """Test with old last_seen but many interactions."""
        old = datetime.now(timezone.utc) - timedelta(days=RECENCY_WINDOW_DAYS + 10)
        strength = compute_relationship_strength(
            last_seen=old,
            interaction_count=FREQUENCY_TARGET,
            sources=["gmail", "calendar"],
        )
        # Recency is 0, but frequency and diversity contribute (scale is 0-100)
        # With logarithmic frequency scaling, the score is slightly higher
        assert 30 < strength < 70

    def test_recent_but_inactive(self):
        """Test with recent last_seen but few interactions."""
        now = datetime.now(timezone.utc)
        strength = compute_relationship_strength(
            last_seen=now,
            interaction_count=1,
            sources=["gmail"],
        )
        # Recency is high, but frequency and diversity are low (scale is 0-100)
        assert 30 < strength < 50

    def test_weights_sum_to_one(self):
        """Verify that maximum possible score is 100."""
        now = datetime.now(timezone.utc)
        # All components at max
        strength = compute_relationship_strength(
            last_seen=now,
            interaction_count=FREQUENCY_TARGET * 2,
            sources=list(range(20)),  # More than total_sources
        )
        assert strength <= 100.0

    def test_rounding(self):
        """Test that strength is rounded to 1 decimal place."""
        now = datetime.now(timezone.utc) - timedelta(days=7)
        strength = compute_relationship_strength(
            last_seen=now,
            interaction_count=5,
            sources=["gmail", "calendar"],
        )
        # Check it's a reasonable precision (1 decimal for 0-100 scale)
        assert len(str(strength).split(".")[-1]) <= 1
