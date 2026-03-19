"""Tests for the hourly usage parser."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add custom_components to path for testing
sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock homeassistant before importing the package
sys.modules.setdefault("homeassistant", MagicMock())
sys.modules.setdefault("homeassistant.config_entries", MagicMock())
sys.modules.setdefault("homeassistant.core", MagicMock())

from custom_components.socalgas.usage_parser import hourly_to_readings


class TestHourlyToReadings:
    """Tests for hourly_to_readings function."""

    def test_basic_conversion_pst(self):
        """8 AM Pacific on Jan 15 (PST, UTC-8) should become 16:00 UTC."""
        data = [
            {"ForDate": "2026-01-15T08:00:00", "Usage": 0.6582, "Cost": 1.2245},
        ]
        readings = hourly_to_readings(data)
        assert len(readings) == 1
        r = readings[0]
        assert r.start == datetime(2026, 1, 15, 16, 0, 0, tzinfo=timezone.utc)
        assert r.duration_seconds == 3600
        assert r.therms == pytest.approx(0.6582)
        assert r.cost_dollars == pytest.approx(1.2245)

    def test_dst_spring_forward(self):
        """March 8, 2026: clocks spring forward at 2 AM.

        1 AM PST (UTC-8) -> 9 AM UTC
        3 AM PDT (UTC-7) -> 10 AM UTC
        """
        data = [
            {"ForDate": "2026-03-08T01:00:00", "Usage": 0.5, "Cost": 0.9},
            {"ForDate": "2026-03-08T03:00:00", "Usage": 0.4, "Cost": 0.8},
        ]
        readings = hourly_to_readings(data)
        assert len(readings) == 2
        assert readings[0].start == datetime(2026, 3, 8, 9, 0, 0, tzinfo=timezone.utc)
        assert readings[1].start == datetime(2026, 3, 8, 10, 0, 0, tzinfo=timezone.utc)

    def test_multiple_readings_sorted(self):
        """Multiple readings should be returned sorted by start time."""
        data = [
            {"ForDate": "2026-01-15T10:00:00", "Usage": 0.3, "Cost": 0.5},
            {"ForDate": "2026-01-15T08:00:00", "Usage": 0.6, "Cost": 1.0},
            {"ForDate": "2026-01-15T09:00:00", "Usage": 0.4, "Cost": 0.7},
        ]
        readings = hourly_to_readings(data)
        assert len(readings) == 3
        assert readings[0].start < readings[1].start < readings[2].start
        # First should be 08:00 Pacific = 16:00 UTC
        assert readings[0].start == datetime(2026, 1, 15, 16, 0, 0, tzinfo=timezone.utc)
        assert readings[1].start == datetime(2026, 1, 15, 17, 0, 0, tzinfo=timezone.utc)
        assert readings[2].start == datetime(2026, 1, 15, 18, 0, 0, tzinfo=timezone.utc)

    def test_empty_input(self):
        """Empty list should return empty list."""
        assert hourly_to_readings([]) == []

    def test_skips_missing_fordate(self):
        """Entries without ForDate should be skipped."""
        data = [
            {"ForDate": "2026-01-15T08:00:00", "Usage": 0.6, "Cost": 1.0},
            {"Usage": 0.3, "Cost": 0.5},  # missing ForDate
            {"ForDate": None, "Usage": 0.2, "Cost": 0.4},  # None ForDate
            {"ForDate": "2026-01-15T09:00:00", "Usage": 0.4, "Cost": 0.7},
        ]
        readings = hourly_to_readings(data)
        assert len(readings) == 2

    def test_zero_usage_kept(self):
        """Zero usage and cost values should be kept, not skipped."""
        data = [
            {"ForDate": "2026-01-15T08:00:00", "Usage": 0.0, "Cost": 0.0},
        ]
        readings = hourly_to_readings(data)
        assert len(readings) == 1
        assert readings[0].therms == 0.0
        assert readings[0].cost_dollars == 0.0
