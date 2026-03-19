"""Parser to convert hourly API response entries into GreenButtonReading objects."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .green_button_parser import GreenButtonReading

PACIFIC = ZoneInfo("America/Los_Angeles")


def hourly_to_readings(hourly_data: list[dict]) -> list[GreenButtonReading]:
    """Convert hourly API entries to a sorted list of GreenButtonReading objects.

    Each entry is expected to have ForDate (local Pacific time string),
    Usage (therms), and Cost (dollars). Entries missing ForDate are skipped.
    """
    readings: list[GreenButtonReading] = []
    for entry in hourly_data:
        for_date = entry.get("ForDate")
        if not for_date:
            continue
        local_dt = datetime.fromisoformat(for_date).replace(tzinfo=PACIFIC)
        utc_dt = local_dt.astimezone(timezone.utc)
        readings.append(
            GreenButtonReading(
                start=utc_dt,
                duration_seconds=3600,
                therms=entry.get("Usage", 0.0),
                cost_dollars=entry.get("Cost", 0.0),
            )
        )
    readings.sort(key=lambda r: r.start)
    return readings
