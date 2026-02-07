"""
Tests for API Cost Tracking (P6.2).

Tests cost calculation and usage storage.
"""
import pytest
import tempfile
import os
from datetime import datetime, timedelta

# All tests in this file are fast unit tests
pytestmark = pytest.mark.unit


class TestCostCalculation:
    """Test cost calculation from token counts."""

    def test_haiku_cost_calculation(self):
        """Haiku cost should be calculated correctly."""
        from api.services.cost_tracker import calculate_cost

        # Haiku: $0.25/1M input, $1.25/1M output
        cost = calculate_cost(
            model="haiku",
            input_tokens=1000,
            output_tokens=500
        )

        # 1000 * 0.25/1M + 500 * 1.25/1M = 0.00025 + 0.000625 = 0.000875
        assert abs(cost - 0.000875) < 0.0001

    def test_sonnet_cost_calculation(self):
        """Sonnet cost should be calculated correctly."""
        from api.services.cost_tracker import calculate_cost

        # Sonnet: $3/1M input, $15/1M output
        cost = calculate_cost(
            model="sonnet",
            input_tokens=1000,
            output_tokens=500
        )

        # 1000 * 3/1M + 500 * 15/1M = 0.003 + 0.0075 = 0.0105
        assert abs(cost - 0.0105) < 0.0001

    def test_opus_cost_calculation(self):
        """Opus cost should be calculated correctly."""
        from api.services.cost_tracker import calculate_cost

        # Opus: $15/1M input, $75/1M output
        cost = calculate_cost(
            model="opus",
            input_tokens=1000,
            output_tokens=500
        )

        # 1000 * 15/1M + 500 * 75/1M = 0.015 + 0.0375 = 0.0525
        assert abs(cost - 0.0525) < 0.0001

    def test_full_model_name_works(self):
        """Should handle full model names like 'claude-sonnet-4-20250514'."""
        from api.services.cost_tracker import calculate_cost

        cost = calculate_cost(
            model="claude-sonnet-4-20250514",
            input_tokens=1000,
            output_tokens=500
        )

        # Should use sonnet pricing
        assert abs(cost - 0.0105) < 0.0001

    def test_unknown_model_defaults_to_sonnet(self):
        """Unknown model should use sonnet pricing."""
        from api.services.cost_tracker import calculate_cost

        cost = calculate_cost(
            model="unknown-model",
            input_tokens=1000,
            output_tokens=500
        )

        # Should use sonnet pricing
        assert abs(cost - 0.0105) < 0.0001

    def test_zero_tokens_returns_zero_cost(self):
        """Zero tokens should return zero cost."""
        from api.services.cost_tracker import calculate_cost

        cost = calculate_cost(
            model="sonnet",
            input_tokens=0,
            output_tokens=0
        )

        assert cost == 0.0


class TestUsageRecord:
    """Test UsageRecord dataclass."""

    def test_creates_usage_record(self):
        """Should create usage record with all fields."""
        from api.services.cost_tracker import UsageRecord

        record = UsageRecord(
            id="usage-123",
            conversation_id="conv-456",
            message_id="msg-789",
            model="sonnet",
            input_tokens=1523,
            output_tokens=487,
            cost_usd=0.0089,
            created_at=datetime.now()
        )

        assert record.id == "usage-123"
        assert record.input_tokens == 1523
        assert record.cost_usd == 0.0089

    def test_usage_record_to_dict(self):
        """UsageRecord should convert to dict."""
        from api.services.cost_tracker import UsageRecord

        record = UsageRecord(
            id="usage-123",
            conversation_id="conv-456",
            message_id="msg-789",
            model="sonnet",
            input_tokens=1523,
            output_tokens=487,
            cost_usd=0.0089,
            created_at=datetime(2026, 1, 7, 12, 0, 0)
        )

        d = record.to_dict()
        assert d["id"] == "usage-123"
        assert d["input_tokens"] == 1523
        assert d["cost_usd"] == 0.0089


class TestCostTrackerStorage:
    """Test CostTracker SQLite storage."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database file."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            yield f.name
        os.unlink(f.name)

    def test_tracker_initialization(self, temp_db):
        """CostTracker should initialize and create table."""
        from api.services.cost_tracker import CostTracker

        tracker = CostTracker(db_path=temp_db)

        # Verify table exists
        import sqlite3
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='api_usage'"
        )
        result = cursor.fetchone()
        conn.close()

        assert result is not None

    def test_record_usage(self, temp_db):
        """Should record usage to database."""
        from api.services.cost_tracker import CostTracker

        tracker = CostTracker(db_path=temp_db)
        record = tracker.record_usage(
            conversation_id="conv-123",
            message_id="msg-456",
            model="sonnet",
            input_tokens=1000,
            output_tokens=500
        )

        assert record is not None
        assert record.input_tokens == 1000
        assert record.output_tokens == 500
        assert record.cost_usd > 0

    def test_get_conversation_total(self, temp_db):
        """Should calculate conversation total cost."""
        from api.services.cost_tracker import CostTracker

        tracker = CostTracker(db_path=temp_db)

        # Record multiple usages in same conversation
        tracker.record_usage("conv-1", "msg-1", "sonnet", 1000, 500)
        tracker.record_usage("conv-1", "msg-2", "sonnet", 2000, 1000)

        total = tracker.get_conversation_total("conv-1")
        assert total > 0

    def test_get_session_total(self, temp_db):
        """Should calculate session total cost."""
        from api.services.cost_tracker import CostTracker

        tracker = CostTracker(db_path=temp_db)

        # Record usages across conversations
        tracker.record_usage("conv-1", "msg-1", "sonnet", 1000, 500)
        tracker.record_usage("conv-2", "msg-2", "haiku", 500, 200)

        total = tracker.get_session_total()
        assert total > 0

    def test_get_usage_by_conversation(self, temp_db):
        """Should retrieve usage records for a conversation."""
        from api.services.cost_tracker import CostTracker

        tracker = CostTracker(db_path=temp_db)

        tracker.record_usage("conv-1", "msg-1", "sonnet", 1000, 500)
        tracker.record_usage("conv-1", "msg-2", "sonnet", 2000, 1000)
        tracker.record_usage("conv-2", "msg-3", "sonnet", 500, 200)

        records = tracker.get_usage_by_conversation("conv-1")
        assert len(records) == 2

    def test_get_usage_by_date_range(self, temp_db):
        """Should retrieve usage records within date range."""
        from api.services.cost_tracker import CostTracker

        tracker = CostTracker(db_path=temp_db)

        # Record usage
        tracker.record_usage("conv-1", "msg-1", "sonnet", 1000, 500)

        # Query by date range
        now = datetime.now()
        yesterday = now - timedelta(days=1)
        tomorrow = now + timedelta(days=1)

        records = tracker.get_usage_by_date_range(yesterday, tomorrow)
        assert len(records) >= 1


class TestCostTrackerService:
    """Test the CostTracker service singleton."""

    def test_get_cost_tracker(self):
        """Should return singleton instance."""
        from api.services.cost_tracker import get_cost_tracker

        tracker1 = get_cost_tracker()
        tracker2 = get_cost_tracker()

        assert tracker1 is tracker2


class TestUsageEventFormat:
    """Test usage event format for SSE."""

    def test_format_usage_event(self):
        """Should format usage record for SSE."""
        from api.services.cost_tracker import format_usage_event, UsageRecord

        record = UsageRecord(
            id="usage-123",
            conversation_id="conv-456",
            message_id="msg-789",
            model="sonnet",
            input_tokens=1523,
            output_tokens=487,
            cost_usd=0.0089,
            created_at=datetime.now()
        )

        event = format_usage_event(record)

        assert event["type"] == "usage"
        assert event["input_tokens"] == 1523
        assert event["output_tokens"] == 487
        assert event["cost_usd"] == 0.0089
        assert event["model"] == "sonnet"

    def test_format_usage_event_with_totals(self):
        """Should include totals in usage event."""
        from api.services.cost_tracker import format_usage_event, UsageRecord

        record = UsageRecord(
            id="usage-123",
            conversation_id="conv-456",
            message_id="msg-789",
            model="sonnet",
            input_tokens=1523,
            output_tokens=487,
            cost_usd=0.0089,
            created_at=datetime.now()
        )

        event = format_usage_event(
            record,
            conversation_total=0.047,
            session_total=0.23
        )

        assert event["conversation_total"] == 0.047
        assert event["session_total"] == 0.23
