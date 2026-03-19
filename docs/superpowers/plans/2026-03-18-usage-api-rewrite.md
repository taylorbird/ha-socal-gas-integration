# Usage API Rewrite Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken Green Button ZIP download with the SoCal Gas usage API endpoints (monthly + hourly per billing cycle).

**Architecture:** Auth via Browserless (unchanged) → fetch billing cycles via monthly endpoint → fetch hourly readings per cycle → convert to GreenButtonReading → import to HA statistics (unchanged pipeline).

**Tech Stack:** Python 3.9+, aiohttp, Home Assistant custom component APIs, zoneinfo for timezone handling.

**Spec:** `docs/superpowers/specs/2026-03-18-usage-api-rewrite-design.md`

---

## File Structure

| File | Role | Action |
|------|------|--------|
| `custom_components/socalgas/api.py` | API client — auth, account discovery, data fetching | Modify: remove Green Button, add usage API methods |
| `custom_components/socalgas/coordinator.py` | HA update coordinator — orchestrates fetch/import | Modify: replace chunk-based download with billing-cycle fetch |
| `custom_components/socalgas/usage_parser.py` | Convert hourly API response → GreenButtonReading | Create: new file for data conversion |
| `custom_components/socalgas/config_flow.py` | Config/options UI | Modify: simplify redownload step |
| `custom_components/socalgas/const.py` | Constants | Modify: add CONF_INITIAL_IMPORT_DONE |
| `custom_components/socalgas/manifest.json` | Integration metadata | Modify: bump version |
| `tests/test_usage_parser.py` | Tests for hourly→reading conversion | Create |
| `tests/test_api_usage.py` | Tests for new API methods | Create |

---

## Chunk 1: API Layer

### Task 1: Update AccountInfo dataclass and GNN mapping

**Files:**
- Modify: `custom_components/socalgas/api.py:35-43` (AccountInfo)
- Modify: `custom_components/socalgas/api.py:207-232` (_get_gnn_mapping)
- Test: `tests/test_api.py` (update existing)

- [ ] **Step 1: Update AccountInfo dataclass**

Change `gnn_id` from `str` to `int`, rename `service_location_id` to `service_location`:

```python
@dataclass
class AccountInfo:
    """Account and meter information discovered during login."""
    account_number: str  # 10-digit account number
    meter_number: str
    gnn_id: int
    service_location: str
```

- [ ] **Step 2: Update _get_gnn_mapping() to preserve GnnId as int and derive service_location**

In `_get_gnn_mapping()` (around line 207), replace the GnnId/ServiceLocationId parsing:

```python
                _LOGGER.warning("GNN mapping keys: %s", list(mapping.keys()))

                gnn_id_raw = mapping.get("GnnId") or mapping.get("gnnId")
                if gnn_id_raw is None:
                    raise SoCalGasConnectionError("GNN mapping missing GnnId")
                try:
                    gnn_id = int(gnn_id_raw)
                except (ValueError, TypeError) as err:
                    raise SoCalGasConnectionError(
                        f"GNN mapping GnnId is not a valid integer: {gnn_id_raw}"
                    ) from err

                meter_number = str(
                    mapping.get("MeterNumber", "")
                    or mapping.get("meterNumber", "")
                )

                # Prefer ServiceLocationId from API; fall back to derivation
                service_location = str(
                    mapping.get("ServiceLocationId", "")
                    or mapping.get("serviceLocationId", "")
                    or mapping.get("ServicePointId", "")
                    or mapping.get("servicePointId", "")
                )
                if not service_location:
                    # Derive: account number with last digit → 0
                    service_location = account_number[:-1] + "0"

                _LOGGER.warning(
                    "GNN mapping result: gnn=%s, slid=%s, meter=%s",
                    gnn_id, service_location, meter_number,
                )
                return AccountInfo(
                    account_number=account_number,
                    meter_number=meter_number,
                    gnn_id=gnn_id,
                    service_location=service_location,
                )
```

- [ ] **Step 3: Fix all references to old field names**

Search for `service_location_id` and `gnn_id` string usage across the codebase and update. The main reference is in `download_green_button()` which will be removed in Task 2, but check for any others.

- [ ] **Step 4: Run existing tests**

Run: `pytest tests/test_api.py -v`
Fix any failures from the type/name changes.

- [ ] **Step 5: Commit**

```bash
git add custom_components/socalgas/api.py tests/test_api.py
git commit -m "refactor: AccountInfo gnn_id to int, service_location_id to service_location"
```

---

### Task 2: Add new API methods and remove Green Button download

**Files:**
- Modify: `custom_components/socalgas/api.py` (remove download_green_button, add 3 methods)
- Create: `tests/test_api_usage.py`

- [ ] **Step 1: Remove download_green_button() method**

Delete the `download_green_button()` method (lines ~238-308 in api.py). Also remove the `GREEN_BUTTON_URL` constant at the top of the file (line 16-18).

- [ ] **Step 2: Add verify_account() method**

```python
    async def verify_account(self) -> None:
        """Call the verify endpoint to validate the session.

        The SoCal Gas website calls this before daily/hourly requests.
        Fire-and-forget: log status but do not fail on error.
        """
        if not self._access_token or not self._account_info:
            return

        session = await self._ensure_session()
        info = self._account_info
        url = f"{SMARTCMOBILE_BASE}/connectorsso/api/module/verify?accountId={info.account_number}"

        try:
            async with session.get(
                url,
                headers={
                    **SMARTCMOBILE_HEADERS,
                    "AccessToken": self._access_token,
                },
            ) as resp:
                _LOGGER.debug("Verify account status: %s", resp.status)
        except Exception as err:
            _LOGGER.debug("Verify account call failed (non-fatal): %s", err)
```

- [ ] **Step 3: Add fetch_monthly() method**

```python
    async def fetch_monthly(self) -> list[dict]:
        """Fetch monthly billing cycle data.

        Returns list of billing cycle dicts from the API response.
        """
        if not self._access_token or not self._account_info:
            raise SoCalGasAuthError("Must authenticate before fetching data")

        session = await self._ensure_session()
        info = self._account_info

        request_body = {
            "MeterNumber": info.meter_number,
            "GnnId": info.gnn_id,
            "AccountId": info.account_number,
        }

        try:
            async with session.post(
                f"{SMARTCMOBILE_BASE}/connectorsso/api/usage/monthly",
                json=request_body,
                headers={
                    **SMARTCMOBILE_HEADERS,
                    "AccessToken": self._access_token,
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status == 401:
                    raise SoCalGasAuthError("AccessToken expired")
                if resp.status != 200:
                    text = await resp.text()
                    raise SoCalGasConnectionError(
                        f"Monthly usage request failed ({resp.status}): {text[:200]}"
                    )
                data = await resp.json()

        except aiohttp.ClientError as err:
            raise SoCalGasConnectionError(
                f"Monthly usage request error: {err}"
            ) from err

        if isinstance(data, dict) and "Code" in data and data["Code"] != 200:
            raise SoCalGasConnectionError(
                f"Monthly usage API error ({data['Code']}): {data.get('Message', '')}"
            )

        cycles = data.get("Billing", {}).get("BillingCycles", [])
        _LOGGER.info("Fetched %d billing cycles", len(cycles))
        return cycles
```

- [ ] **Step 4: Add fetch_hourly() method**

```python
    async def fetch_hourly(self, billing_cycle: dict) -> list[dict]:
        """Fetch hourly usage data for a single billing cycle.

        Args:
            billing_cycle: A billing cycle dict from fetch_monthly().

        Returns list of hourly reading dicts.
        """
        if not self._access_token or not self._account_info:
            raise SoCalGasAuthError("Must authenticate before fetching data")

        session = await self._ensure_session()
        info = self._account_info

        request_body = {
            "MeterNumber": info.meter_number,
            "GnnId": info.gnn_id,
            "AccountNumber": info.account_number,
            "ServiceLocation": info.service_location,
            "BillCycle": billing_cycle,
        }

        _LOGGER.info(
            "Fetching hourly data for cycle %s",
            billing_cycle.get("Title", "unknown"),
        )

        try:
            async with session.post(
                f"{SMARTCMOBILE_BASE}/connectorsso/api/usage/hourly",
                json=request_body,
                headers={
                    **SMARTCMOBILE_HEADERS,
                    "AccessToken": self._access_token,
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status == 401:
                    raise SoCalGasAuthError("AccessToken expired")
                if resp.status != 200:
                    text = await resp.text()
                    raise SoCalGasConnectionError(
                        f"Hourly usage request failed ({resp.status}): {text[:200]}"
                    )
                data = await resp.json()

        except aiohttp.ClientError as err:
            raise SoCalGasConnectionError(
                f"Hourly usage request error: {err}"
            ) from err

        if isinstance(data, dict) and "Code" in data and data.get("Code") not in (None, 200):
            raise SoCalGasConnectionError(
                f"Hourly usage API error ({data['Code']}): {data.get('Message', '')}"
            )

        # The hourly response has HourlyUsage as a flat list across all days
        # Each entry has: ForDate, Usage, Cost, etc.
        hourly_list = []
        if isinstance(data, dict):
            for key in ("HourlyUsage", "hourlyUsage"):
                if key in data:
                    hourly_list = data[key]
                    break

        _LOGGER.info(
            "Cycle %s: %d hourly readings",
            billing_cycle.get("Title", "unknown"), len(hourly_list),
        )
        return hourly_list
```

- [ ] **Step 5: Write tests for new API methods**

Create `tests/test_api_usage.py`:

```python
"""Tests for the new usage API methods."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import ClientSession

from custom_components.socalgas.api import (
    SoCalGasAPI,
    SoCalGasAuthError,
    SoCalGasConnectionError,
    AccountInfo,
)


@pytest.fixture
def api():
    """Create an API instance with pre-set auth state."""
    a = SoCalGasAPI("user@test.com", "password", browserless_url="http://browserless:3000")
    a._access_token = "test-token"
    a._account_info = AccountInfo(
        account_number="0388795601",
        meter_number="15874691",
        gnn_id=388795600,
        service_location="0388795600",
    )
    return a


@pytest.mark.asyncio
async def test_fetch_monthly_success(api):
    """Test successful monthly fetch."""
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "Billing": {
            "BillingCycles": [
                {"BillDays": "30", "TotalServiceAmount": 50.0, "FromDate": "2026-01-01T00:00:00", "ToDate": "2026-01-31T00:00:00", "Title": "01/01/26 - 01/31/26"},
            ]
        }
    })
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock()

    mock_session = AsyncMock(spec=ClientSession)
    mock_session.post = MagicMock(return_value=mock_response)
    api._session = mock_session

    cycles = await api.fetch_monthly()
    assert len(cycles) == 1
    assert cycles[0]["TotalServiceAmount"] == 50.0


@pytest.mark.asyncio
async def test_fetch_monthly_auth_error(api):
    """Test monthly fetch with expired token."""
    mock_response = AsyncMock()
    mock_response.status = 401
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock()

    mock_session = AsyncMock(spec=ClientSession)
    mock_session.post = MagicMock(return_value=mock_response)
    api._session = mock_session

    with pytest.raises(SoCalGasAuthError):
        await api.fetch_monthly()


@pytest.mark.asyncio
async def test_fetch_monthly_api_error_code(api):
    """Test monthly fetch with API error code."""
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"Code": 701, "Message": "Not available"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock()

    mock_session = AsyncMock(spec=ClientSession)
    mock_session.post = MagicMock(return_value=mock_response)
    api._session = mock_session

    with pytest.raises(SoCalGasConnectionError, match="701"):
        await api.fetch_monthly()


@pytest.mark.asyncio
async def test_fetch_hourly_success(api):
    """Test successful hourly fetch."""
    billing_cycle = {"Title": "01/01/26 - 01/31/26", "FromDate": "2026-01-01T00:00:00", "ToDate": "2026-01-31T00:00:00"}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "HourlyUsage": [
            {"ForDate": "2026-01-01T00:00:00", "Usage": 0.5, "Cost": 1.0},
            {"ForDate": "2026-01-01T01:00:00", "Usage": 0.3, "Cost": 0.6},
        ]
    })
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock()

    mock_session = AsyncMock(spec=ClientSession)
    mock_session.post = MagicMock(return_value=mock_response)
    api._session = mock_session

    readings = await api.fetch_hourly(billing_cycle)
    assert len(readings) == 2
    assert readings[0]["Usage"] == 0.5


@pytest.mark.asyncio
async def test_fetch_hourly_not_authenticated(api):
    """Test hourly fetch without auth raises."""
    api._access_token = None
    with pytest.raises(SoCalGasAuthError):
        await api.fetch_hourly({"Title": "test"})


@pytest.mark.asyncio
async def test_verify_account_does_not_raise(api):
    """Test verify_account is fire-and-forget."""
    mock_response = AsyncMock()
    mock_response.status = 500  # Even errors should not raise
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock()

    mock_session = AsyncMock(spec=ClientSession)
    mock_session.get = MagicMock(return_value=mock_response)
    api._session = mock_session

    # Should not raise
    await api.verify_account()
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_api_usage.py -v`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add custom_components/socalgas/api.py tests/test_api_usage.py
git commit -m "feat: replace Green Button download with usage API methods (monthly, hourly, verify)"
```

---

## Chunk 2: Usage Parser and Coordinator

### Task 3: Create usage_parser.py for hourly→GreenButtonReading conversion

**Files:**
- Create: `custom_components/socalgas/usage_parser.py`
- Create: `tests/test_usage_parser.py`

- [ ] **Step 1: Write failing tests for hourly_to_readings()**

Create `tests/test_usage_parser.py`:

```python
"""Tests for usage API response parser."""
import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from custom_components.socalgas.usage_parser import hourly_to_readings


LA_TZ = ZoneInfo("America/Los_Angeles")


def test_basic_conversion():
    """Test basic hourly entry to GreenButtonReading."""
    hourly_data = [
        {"ForDate": "2026-01-15T08:00:00", "Usage": 0.5, "Cost": 1.23},
    ]
    readings = hourly_to_readings(hourly_data)
    assert len(readings) == 1
    r = readings[0]
    assert r.therms == 0.5
    assert r.cost_dollars == 1.23
    assert r.duration_seconds == 3600
    # 8 AM Pacific on Jan 15 = 4 PM UTC (PST = UTC-8)
    expected_utc = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)
    assert r.start == expected_utc


def test_dst_transition_spring_forward():
    """Test spring forward: 2 AM doesn't exist, 1 AM → 3 AM."""
    # March 8, 2026 is spring forward in US
    hourly_data = [
        {"ForDate": "2026-03-08T01:00:00", "Usage": 0.1, "Cost": 0.2},
        # 2 AM doesn't exist — API may skip it or not
        {"ForDate": "2026-03-08T03:00:00", "Usage": 0.1, "Cost": 0.2},
    ]
    readings = hourly_to_readings(hourly_data)
    assert len(readings) == 2
    # 1 AM PST = 9 AM UTC
    assert readings[0].start == datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)
    # 3 AM PDT = 10 AM UTC
    assert readings[1].start == datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc)


def test_multiple_readings():
    """Test conversion of multiple hourly entries."""
    hourly_data = [
        {"ForDate": "2026-01-15T00:00:00", "Usage": 0.1, "Cost": 0.2},
        {"ForDate": "2026-01-15T01:00:00", "Usage": 0.2, "Cost": 0.4},
        {"ForDate": "2026-01-15T02:00:00", "Usage": 0.3, "Cost": 0.6},
    ]
    readings = hourly_to_readings(hourly_data)
    assert len(readings) == 3
    assert readings[0].therms == 0.1
    assert readings[2].therms == 0.3


def test_empty_input():
    """Test empty list returns empty."""
    assert hourly_to_readings([]) == []


def test_skips_entries_without_fordate():
    """Test entries missing ForDate are skipped."""
    hourly_data = [
        {"ForDate": "2026-01-15T08:00:00", "Usage": 0.5, "Cost": 1.0},
        {"Usage": 0.3, "Cost": 0.6},  # missing ForDate
    ]
    readings = hourly_to_readings(hourly_data)
    assert len(readings) == 1


def test_zero_usage():
    """Test zero usage values are kept."""
    hourly_data = [
        {"ForDate": "2026-01-15T08:00:00", "Usage": 0.0, "Cost": 0.0},
    ]
    readings = hourly_to_readings(hourly_data)
    assert len(readings) == 1
    assert readings[0].therms == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_usage_parser.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement usage_parser.py**

Create `custom_components/socalgas/usage_parser.py`:

```python
"""Parse SoCal Gas usage API responses into GreenButtonReading objects."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .green_button_parser import GreenButtonReading

_LOGGER = logging.getLogger(__name__)

_LA_TZ = ZoneInfo("America/Los_Angeles")


def hourly_to_readings(hourly_data: list[dict]) -> list[GreenButtonReading]:
    """Convert hourly usage API response entries to GreenButtonReading objects.

    Args:
        hourly_data: List of dicts from the hourly API response,
            each with ForDate, Usage, and Cost fields.

    Returns:
        List of GreenButtonReading objects sorted by start time.
    """
    readings = []
    for entry in hourly_data:
        for_date = entry.get("ForDate")
        if not for_date:
            continue

        try:
            # Parse as naive datetime, localize to Pacific, convert to UTC
            naive = datetime.fromisoformat(for_date)
            local_dt = naive.replace(tzinfo=_LA_TZ)
            utc_dt = local_dt.astimezone(timezone.utc)
        except (ValueError, TypeError) as err:
            _LOGGER.debug("Skipping entry with bad ForDate %r: %s", for_date, err)
            continue

        readings.append(GreenButtonReading(
            start=utc_dt,
            duration_seconds=3600,
            therms=float(entry.get("Usage", 0.0)),
            cost_dollars=float(entry.get("Cost", 0.0)),
        ))

    readings.sort(key=lambda r: r.start)
    return readings
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_usage_parser.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/socalgas/usage_parser.py tests/test_usage_parser.py
git commit -m "feat: add usage_parser for hourly API response to GreenButtonReading conversion"
```

---

### Task 4: Rewrite coordinator to use billing-cycle fetching

**Files:**
- Modify: `custom_components/socalgas/coordinator.py`

- [ ] **Step 1: Remove old constants and imports**

Remove `CHUNK_DAYS = 30` (line 44). Remove `import zipfile` and `import io` (lines 6-7). Add import for `usage_parser`:

```python
from .usage_parser import hourly_to_readings
```

- [ ] **Step 2: Replace _async_update_data() logic**

Replace the date-range calculation in `_async_update_data()` (lines 122-159) with billing-cycle logic:

```python
            end_date = datetime.now(tz=timezone.utc)
            initial_import_done = self.entry.data.get(
                CONF_INITIAL_IMPORT_DONE, False
            )

            # Always fetch billing cycles first
            cycles = await api.fetch_monthly()
            await api.verify_account()

            if not initial_import_done:
                # Import all cycles with data
                cycles_to_fetch = [
                    c for c in cycles if c.get("TotalServiceAmount", 0) > 0
                ]
                _LOGGER.info(
                    "Initial import: %d billing cycles with data",
                    len(cycles_to_fetch),
                )
            else:
                # Refresh: current open cycle + most recent completed
                cycles_to_fetch = self._pick_refresh_cycles(cycles)
                _LOGGER.info(
                    "Refresh: fetching %d billing cycles",
                    len(cycles_to_fetch),
                )

            total_readings = await self._fetch_billing_cycles(
                api, cycles_to_fetch
            )
```

- [ ] **Step 3: Add _pick_refresh_cycles() helper**

```python
    @staticmethod
    def _pick_refresh_cycles(cycles: list[dict]) -> list[dict]:
        """Pick which billing cycles to fetch on a refresh run.

        Returns the current open cycle (if any) and the most recent
        completed cycle.
        """
        to_fetch = []
        completed = []

        for c in cycles:
            if c.get("TotalServiceAmount", 0) == 0:
                # Open/empty cycle
                to_fetch.append(c)
            else:
                completed.append(c)

        # Most recent completed cycle by FromDate
        if completed:
            completed.sort(
                key=lambda c: c.get("FromDate", ""), reverse=True
            )
            to_fetch.append(completed[0])

        return to_fetch
```

- [ ] **Step 4: Replace _download_range() with _fetch_billing_cycles()**

Remove the entire `_download_range()` method and `_extract_xml_from_zip()` static method. Replace with:

```python
    async def _fetch_billing_cycles(
        self,
        api: SoCalGasAPI,
        cycles: list[dict],
        label: str = "Import",
    ) -> int:
        """Fetch hourly data for billing cycles and import to HA.

        Downloads hourly data per cycle, converts to readings,
        deduplicates, merges with existing, and imports.
        Returns total new readings imported.
        """
        name_slug = self._name_slug()
        notification_id = f"{DOMAIN}_{label.lower().replace(' ', '_')}_{name_slug}"
        total_cycles = len(cycles)
        failed_cycles = 0
        all_readings = []

        for i, cycle in enumerate(cycles, 1):
            title = cycle.get("Title", "unknown")
            _LOGGER.info("Fetching cycle %d/%d: %s", i, total_cycles, title)

            async_create(
                self.hass,
                f"Downloading data: billing cycle {i} of {total_cycles}\n({title})",
                title=f"SoCal Gas {label}",
                notification_id=notification_id,
            )

            try:
                hourly_data = await api.fetch_hourly(cycle)
                readings = hourly_to_readings(hourly_data)
                if readings:
                    _LOGGER.info(
                        "Cycle %d/%d: %d readings (%s to %s)",
                        i, total_cycles, len(readings),
                        readings[0].start.date(), readings[-1].start.date(),
                    )
                    all_readings.extend(readings)
                else:
                    _LOGGER.info("Cycle %d/%d: no hourly data returned", i, total_cycles)
            except (SoCalGasAuthError, SoCalGasConnectionError) as err:
                _LOGGER.error("Cycle %d/%d (%s) failed: %s", i, total_cycles, title, err)
                failed_cycles += 1
                if isinstance(err, SoCalGasAuthError):
                    async_dismiss(self.hass, notification_id)
                    raise ConfigEntryAuthFailed(str(err)) from err
                # Connection errors: log and continue with remaining cycles

            # Rate-limit protection between cycles
            if i < total_cycles:
                _LOGGER.info("Sleeping 5s between billing cycles")
                await asyncio.sleep(5)

        # Deduplicate by hour
        if not all_readings:
            async_dismiss(self.hass, notification_id)
            _LOGGER.info("No readings in downloaded data")
            return 0

        hour_map: dict[datetime, object] = {}
        for r in all_readings:
            key = r.start.replace(minute=0, second=0, microsecond=0)
            hour_map[key] = r
        unique_readings = sorted(hour_map.values(), key=lambda r: r.start)

        dupes = len(all_readings) - len(unique_readings)
        if dupes > 0:
            _LOGGER.info(
                "Deduplicated %d overlapping readings (kept %d unique)",
                dupes, len(unique_readings),
            )

        # Merge with existing data
        earliest = unique_readings[0].start
        existing = await async_get_existing_states(self.hass, name_slug, earliest)
        merged = merge_readings_with_existing(unique_readings, existing)

        # Compute cumulative sums and import
        running_usage_sum, running_cost_sum = await async_get_prior_sums(
            self.hass, name_slug, merged[0].start
        )
        _LOGGER.info(
            "Starting sums before %s: usage=%.2f ft³, cost=$%.4f",
            merged[0].start.date(), running_usage_sum, running_cost_sum,
        )

        async_create(
            self.hass,
            f"Importing {len(merged)} readings...",
            title=f"SoCal Gas {label}",
            notification_id=notification_id,
        )

        stats = readings_to_hourly_statistics(
            merged, running_usage_sum, running_cost_sum
        )
        await async_import_to_ha(self.hass, stats, name_slug)

        _LOGGER.info(
            "Final sums: usage=%.2f ft³, cost=$%.4f",
            stats[-1].usage_sum, stats[-1].cost_sum,
        )

        async_dismiss(self.hass, notification_id)

        failed_msg = f" ({failed_cycles} cycles failed)" if failed_cycles else ""
        summary_msg = (
            f"{len(unique_readings)} new readings imported "
            f"({merged[0].start.strftime('%b %d, %Y')} – "
            f"{merged[-1].start.strftime('%b %d, %Y')}){failed_msg}"
        )
        _LOGGER.info(
            "%s complete: %d new readings + %d existing merged (%s to %s)",
            label, len(unique_readings),
            len(merged) - len(unique_readings),
            merged[0].start.date(),
            merged[-1].start.date(),
        )
        async_create(
            self.hass,
            summary_msg,
            title=f"SoCal Gas {label} Complete",
            notification_id=f"{notification_id}_done",
        )

        return len(unique_readings)
```

- [ ] **Step 5: Update async_redownload_range() to work with new approach**

Replace the existing `async_redownload_range()` method:

```python
    async def async_redownload_all(self) -> None:
        """Re-download all billing cycles on demand."""
        username = self.entry.data.get(CONF_USERNAME)
        password = self.entry.data.get(CONF_PASSWORD)
        if not username or not password:
            _LOGGER.error("Cannot redownload: no credentials configured")
            return

        browserless_url = self.entry.data.get(CONF_BROWSERLESS_URL)

        async with self._download_lock:
            api = SoCalGasAPI(username, password, browserless_url=browserless_url)
            try:
                await api.authenticate()
                await api.verify_account()
                cycles = await api.fetch_monthly()
                cycles_with_data = [
                    c for c in cycles if c.get("TotalServiceAmount", 0) > 0
                ]
                await self._fetch_billing_cycles(
                    api, cycles_with_data, label="Re-download"
                )
            except (SoCalGasAuthError, SoCalGasConnectionError) as err:
                _LOGGER.error("Redownload failed: %s", err)
            finally:
                await api.close()
```

- [ ] **Step 6: Remove _extract_xml_from_zip()**

Delete the `_extract_xml_from_zip()` static method and remove `import io` and `import zipfile` from the top of the file.

- [ ] **Step 7: Run all tests**

Run: `pytest tests/ -v`
Fix any failures.

- [ ] **Step 8: Commit**

```bash
git add custom_components/socalgas/coordinator.py
git commit -m "feat: replace chunk-based download with billing-cycle fetch"
```

---

## Chunk 3: Config Flow, Constants, and Version

### Task 5: Simplify redownload in config_flow.py

**Files:**
- Modify: `custom_components/socalgas/config_flow.py`

- [ ] **Step 1: Simplify async_step_redownload()**

Replace the date-picker redownload step (lines ~365-419) with a simpler version that resets `initial_import_done` and triggers a refresh:

```python
    async def async_step_redownload(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Handle re-download of all billing cycle data."""
        if user_input is not None:
            # Reset initial import flag so next refresh fetches everything
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={
                    **self._entry.data,
                    "initial_import_done": False,
                },
            )
            # Trigger a refresh
            coordinator = self.hass.data.get(DOMAIN, {}).get(
                self._entry.entry_id
            )
            if coordinator:
                await coordinator.async_request_refresh()
            return self.async_abort(reason="redownload_started")

        return self.async_show_form(
            step_id="redownload",
            description_placeholders={
                "info": "This will re-download all available billing cycle data."
            },
        )
```

- [ ] **Step 2: Remove unused imports**

Remove `timedelta` import if only used by the old date-range redownload, and the `NumberSelector` / date-related selectors if no longer needed for redownload.

- [ ] **Step 3: Commit**

```bash
git add custom_components/socalgas/config_flow.py
git commit -m "simplify: redownload resets initial_import_done instead of date picker"
```

---

### Task 6: Update constants and version

**Files:**
- Modify: `custom_components/socalgas/const.py`
- Modify: `custom_components/socalgas/manifest.json`

- [ ] **Step 1: Add CONF_INITIAL_IMPORT_DONE to const.py**

```python
CONF_INITIAL_IMPORT_DONE = "initial_import_done"
```

- [ ] **Step 2: Update coordinator.py to use the constant**

Replace the hardcoded `CONF_INITIAL_IMPORT_DONE = "initial_import_done"` in coordinator.py (line 46) with an import from const.py.

- [ ] **Step 3: Bump version to 0.4.0 in manifest.json**

Change `"version": "0.3.0"` to `"version": "0.4.0"`.

- [ ] **Step 4: Commit**

```bash
git add custom_components/socalgas/const.py custom_components/socalgas/coordinator.py custom_components/socalgas/manifest.json
git commit -m "chore: add CONF_INITIAL_IMPORT_DONE constant, bump version to 0.4.0"
```

---

### Task 7: End-to-end local test

**Files:**
- Modify: `test_usage_api.py` (repo root)

- [ ] **Step 1: Update test_usage_api.py to test the full pipeline**

Add a section that takes the hourly API response, runs it through `hourly_to_readings()`, and verifies the output matches what `statistics.py` expects. This validates the data flows correctly end-to-end without HA.

- [ ] **Step 2: Run the local test against live API**

```bash
export SOCALGAS_USERNAME='...'
export SOCALGAS_PASSWORD='...'
export BROWSERLESS_URL='...'
python test_usage_api.py
```

Verify: monthly returns billing cycles, hourly returns readings, readings convert correctly.

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -v
```

Expected: All tests pass.

- [ ] **Step 4: Final commit and push**

```bash
git add -A
git commit -m "feat: complete usage API rewrite — replace broken Green Button with billing-cycle endpoints"
git push
```

---

## Summary

| Task | Description | Estimated Steps |
|------|-------------|-----------------|
| 1 | Update AccountInfo and GNN mapping | 5 |
| 2 | Add new API methods, remove Green Button | 7 |
| 3 | Create usage_parser.py with tests | 5 |
| 4 | Rewrite coordinator | 8 |
| 5 | Simplify config_flow redownload | 3 |
| 6 | Constants and version bump | 4 |
| 7 | End-to-end test and push | 4 |
