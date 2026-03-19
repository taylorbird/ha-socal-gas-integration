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
   - Not used in this design — hourly supersedes it for our use case

3. **Hourly** — `POST /connectorsso/api/usage/hourly`
   - Same body format as daily
   - Returns: Hourly usage/cost for one billing cycle (~157KB)

4. **Verify** — `GET /connectorsso/api/module/verify?accountId=<account>`
   - Called once per session before hourly requests (as observed in browser)
   - Returns account details (name, address, balance). Response is informational — we call it for side effects (session validation) and ignore the body.
   - Idempotent; safe to call multiple times.

### Key API Differences from Old Green Button

- Daily/hourly use `AccountNumber` (not `AccountId`) in the request body
- `ServiceLocation`: prefer value from GNN mapping response (`ServiceLocationId`); fall back to derivation (account number with last digit → `0`) if empty
- `GnnId` must be **integer**, not string
- `BillCycle` = full billing cycle object from the monthly response. Minimum required fields: `FromDate`, `ToDate`, `BillDays`, `CCF`, `MAId`, `RateClass`, `TotalServiceAmount`, `Monthly`, `Hourly`, `Title`
- Usage values are in CCF (hundred cubic feet). 1 CCF ≈ 1 therm (the monthly response includes a `CCF` factor field, e.g. 0.968, for precise conversion if needed). For this implementation, we treat CCF as therms (≈2-3% approximation).

## Design

### API Layer (`api.py`)

Remove `download_green_button()`. Add three new methods:

- **`fetch_monthly()`** — calls monthly endpoint, returns list of billing cycle dicts
- **`fetch_hourly(billing_cycle)`** — calls hourly endpoint for one billing cycle, returns list of hourly reading dicts
- **`verify_account()`** — `GET /connectorsso/api/module/verify?accountId=X`. Called once per session before hourly fetches. Fire-and-forget: log the response status, do not fail if it errors.

`AccountInfo` dataclass changes:
- Rename `service_location_id` to `service_location` (derived: prefer GNN mapping `ServiceLocationId` value; if empty, use account_number with last digit → `0`)
- `gnn_id` type changes from `str` to `int`. The `_get_gnn_mapping()` parser will keep the native integer from the JSON response instead of calling `str()`. Error handling: if `GnnId` is missing or not a valid int, raise `SoCalGasConnectionError`.

### Coordinator (`coordinator.py`)

Replace `_download_range()` with billing-cycle-based fetching.

**First run** (initial_import_done = False):
1. Authenticate
2. `verify_account()`
3. `fetch_monthly()` → get all billing cycles
4. Filter to cycles with `TotalServiceAmount > 0` (skip open/empty cycles)
5. For each cycle: `fetch_hourly(cycle)` → convert to `GreenButtonReading` list
6. Sleep 5s between billing cycle fetches (rate-limit protection, carried forward from old design)
7. Merge all readings, deduplicate by hour, import to HA statistics
8. Set `initial_import_done = True`

**Subsequent runs:**
1. Authenticate
2. `verify_account()`
3. `fetch_monthly()` → get all billing cycles
4. Identify the **current open cycle** (ToDate is in the future or TotalServiceAmount == 0) and the **most recent completed cycle** (highest FromDate with TotalServiceAmount > 0)
5. Fetch hourly for those 1-2 cycles only
6. Merge with existing statistics

**Error handling:**
- If a billing cycle fetch fails, log the error with the cycle's date range, save what we have so far, and continue with remaining cycles. Surface the partial failure count in the completion notification.
- If auth fails, raise as before (ConfigEntryAuthFailed or UpdateFailed).

**Removed:** `_download_range()`, `_extract_xml_from_zip()`, `CHUNK_DAYS`, 30-day chunk loop, ZIP handling.

**Kept:** dedup by hour, merge with existing, progress notifications ("billing cycle X of Y"), download lock, "Import Complete" notification.

**`lookback_days` config:** No longer used for date-range calculation. On initial import, all billing cycles with data are fetched (typically ~12 months). The config field is kept for backward compatibility but ignored.

### Data Conversion

A helper function `_hourly_to_readings(hourly_data, billing_cycle)` maps the hourly API response to `GreenButtonReading` objects:

```
API entry:  {"Cost": 1.2245, "Usage": 0.6582, "ForDate": "2026-02-13T08:00:00"}

GreenButtonReading:
  start = ForDate parsed as America/Los_Angeles, converted to UTC
  duration_seconds = 3600
  therms = Usage  (CCF ≈ therms)
  cost_dollars = Cost
```

**Timezone handling:** `ForDate` is local Pacific time. Use `zoneinfo.ZoneInfo("America/Los_Angeles")` (not a fixed offset) to correctly handle DST transitions. During spring-forward, the missing 2 AM hour will have no reading (expected). During fall-back, the ambiguous 1 AM hour should use `fold=0` (first occurrence).

`statistics.py` pipeline unchanged: converts therms→ft³ (* 100), computes running sums.

### Unchanged Files

- **browser.py** — auth flow working correctly
- **green_button_parser.py** — kept for file-upload path in config_flow
- **sensor.py** — reads from statistics, independent of data source
- **statistics.py** — conversion and import logic unchanged
- **config_flow.py** — credentials flow unchanged; redownload simplified to reset `initial_import_done = False` and trigger a full refresh (date range picker removed)

### File Change Summary

| File | Change |
|------|--------|
| `api.py` | Remove `download_green_button()`. Add `fetch_monthly()`, `fetch_hourly()`, `verify_account()`. GnnId as int. Rename/update service_location. |
| `coordinator.py` | Replace chunk-based download with billing-cycle fetch. Remove ZIP logic. Add partial failure handling. |
| `config_flow.py` | Simplify redownload to reset initial_import_done and refresh |
| `manifest.json` | Bump to 0.4.0 |
| `__init__.py` | No changes |
| `browser.py` | No changes |
| `sensor.py` | No changes |
| `statistics.py` | No changes |
| `green_button_parser.py` | No changes |

### Testing

- Update `test_usage_api.py` for end-to-end local validation (auth → monthly → hourly → parse to readings)
- Existing unit tests for parser and statistics unchanged
- Manual: install via HACS, verify sensors populate, check Developer Tools → Statistics
