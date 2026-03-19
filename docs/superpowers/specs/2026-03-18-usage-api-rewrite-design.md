# SoCal Gas Integration: Usage API Rewrite

**Date:** 2026-03-18
**Status:** Approved

## Problem

The Green Button ZIP download endpoint (`greenbuttonservices/api/greenbutton/zipfile`) returns HTTP 500 with empty body for all parameter variations. The `connectorsso/api/usage/greenbutton` endpoint returns Code 701 "The Analyze Usage tool is not available for this account." This is a SoCal Gas backend change — the endpoints are broken/deprecated.

## Discovery

Through browser network capture and direct API testing, we found the actual endpoints the SoCal Gas website uses to render the Analyze Usage page:

### Working Endpoints

1. **Monthly** — `POST /connectorsso/api/usage/monthly`
   - Body: `{"MeterNumber": "<meter>", "GnnId": <int>, "AccountId": "<account>"}`
   - Returns: Array of billing cycles with dates, costs, CCF values (~8KB, ~12 months)

2. **Daily** — `POST /connectorsso/api/usage/daily`
   - Body: `{"MeterNumber": "<meter>", "GnnId": <int>, "AccountNumber": "<account>", "ServiceLocation": "<slid>", "BillCycle": {<full billing cycle object>}}`
   - Returns: Daily usage/cost for one billing cycle (~165KB)
   - City/Zip optional (only affects weather data)

3. **Hourly** — `POST /connectorsso/api/usage/hourly`
   - Same body format as daily
   - Returns: Hourly usage/cost for one billing cycle (~157KB)

### Key API Differences

- Daily/hourly use `AccountNumber` (not `AccountId`)
- `ServiceLocation` = account number with last digit changed to `0`
- `GnnId` must be **integer**, not string
- `BillCycle` = full billing cycle object from the monthly response
- A `verify?accountId=X` GET call is made by the browser before daily/hourly requests
- Usage values are in CCF (≈ therms)

## Design

### API Layer (`api.py`)

Remove `download_green_button()`. Add three new methods:

- **`fetch_monthly()`** — calls monthly endpoint, returns list of billing cycle dicts
- **`fetch_hourly(billing_cycle)`** — calls hourly endpoint for one billing cycle, returns hourly readings
- **`verify_account()`** — calls verify endpoint once before hourly requests

`AccountInfo` dataclass changes:
- Add `service_location: str` (derived: account_number with last digit → `0`)
- `gnn_id` stored as `int` instead of `str`

### Coordinator (`coordinator.py`)

Replace `_download_range()` with billing-cycle-based fetching:

**First run** (initial_import_done = False):
1. Authenticate
2. `fetch_monthly()` → get all billing cycles
3. Filter to cycles with `TotalServiceAmount > 0`
4. For each cycle: `fetch_hourly(cycle)` → convert to `GreenButtonReading` list
5. Merge, deduplicate by hour, import to HA statistics
6. Set `initial_import_done = True`

**Subsequent runs:**
1. Authenticate
2. `fetch_monthly()` → get all billing cycles
3. Fetch hourly for only the most recent completed cycle + the current open cycle
4. Merge with existing statistics

Removed: `_download_range()`, `_extract_xml_from_zip()`, `CHUNK_DAYS`, 30-day chunk loop.

Kept: dedup by hour, merge with existing, progress notifications, download lock, "Import Complete" notification.

### Data Conversion

Hourly API response maps to existing `GreenButtonReading`:

```
API:  {"Cost": 1.2245, "Usage": 0.6582, "ForDate": "2026-02-13T08:00:00"}
  →  GreenButtonReading(start=<ForDate as local→UTC>, duration_seconds=3600, therms=Usage, cost_dollars=Cost)
```

`ForDate` timestamps are local Pacific time — convert to UTC for HA statistics.

`statistics.py` pipeline unchanged: converts therms→ft³ (* 100), computes running sums.

### Unchanged Files

- **browser.py** — auth flow working correctly
- **green_button_parser.py** — kept for file-upload path in config_flow
- **sensor.py** — reads from statistics, independent of data source
- **statistics.py** — conversion and import logic unchanged
- **config_flow.py** — credentials flow unchanged; redownload triggers full re-import instead of date range picking

### File Change Summary

| File | Change |
|------|--------|
| `api.py` | Remove `download_green_button()`. Add `fetch_monthly()`, `fetch_hourly()`, `verify_account()`. GnnId as int. Add `service_location`. |
| `coordinator.py` | Replace chunk-based download with billing-cycle fetch. Remove ZIP logic. |
| `config_flow.py` | Redownload triggers full re-import |
| `manifest.json` | Bump to 0.4.0 |
| `__init__.py` | No changes |
| `browser.py` | No changes |
| `sensor.py` | No changes |
| `statistics.py` | No changes |
| `green_button_parser.py` | No changes |

### Testing

- Update `test_usage_api.py` for end-to-end local validation
- Existing unit tests for parser and statistics unchanged
- Manual: install via HACS, verify sensors populate, check Developer Tools → Statistics
