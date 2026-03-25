# SoCal Gas HA Integration

## Project Status (2026-03-24)

### What's Working
- **Auth:** Browser login via Browserless, token capture, GNN mapping
- **Data import:** Billing cycle fetch, hourly data download, statistics import
- **All 5 sensors:** status (ok), cost_this_month ($29.35), usage_this_month (1566.29 ft³), cost_today (0.0), usage_today (0.0)
- **Statistics:** 4 entries in Developer Tools → Statistics, all "No issue"
- **Energy dashboard:** Gas tab recognizes the integration, shows data through March 13
- **Clean startup:** No errors or task exceptions

### Known Issues / Next Steps

1. **Data gap after March 13** — The Energy dashboard shows data only through March 13 (end of last completed billing cycle). The current open cycle (03/16 - 04/14) has daily data on socalgas.com through ~March 24, but our integration may not be fetching it.
   - **Root cause investigation needed:** Added `logger:` config to show INFO messages but haven't yet confirmed whether the open cycle's hourly API returns data
   - **Fix pushed:** Initial import now includes open cycles (TotalServiceAmount == 0), not just completed ones
   - **Next:** Check HA logs after restart for INFO messages showing which cycles were fetched and how many readings each returned. If open cycle returns 0 readings, need to investigate the hourly API endpoint for open cycles
   - Logger config for debugging (add to configuration.yaml root level):
     ```yaml
     logger:
       default: warning
       logs:
         custom_components.socalgas: info
     ```

2. **Duplicate statistics** — 4 statistic entries (should be 2). Two pairs with slightly different slugs. Likely from multiple config entries or account name changes between installs. Check Settings → Devices & Services for duplicate SoCal Gas entries.

3. **Translation error** — `component.socalgas.options.step.redownload.description` missing `earliest_date` variable. Minor, cosmetic only.

4. **GNN mapping warnings** — 3 warnings on every startup are informational, should be downgraded to debug level.

5. **Dashboard setup** — statistics-graph card or Energy dashboard gas source configuration (point cost at `socalgas:gas_cost_home`)

### Bugs Fixed This Session (2026-03-24)
- **Status sensor "Error adding entity"** — `AttributeError: 'SoCalGasCoordinator' has no attribute 'last_update_success_time'`. Used `getattr` fallback. Root cause: attribute doesn't exist on this HA version's DataUpdateCoordinator.
- **"Task exception was never retrieved" (5x per restart)** — `_update_from_statistics` had imports and `async_write_ha_state()` outside try/except. Wrapped entire method body.
- **Open billing cycles skipped in initial import** — `TotalServiceAmount == 0` filter excluded current billing period. Now includes open cycles.

### Key API Details (discovered through testing)
- Monthly: `POST /connectorsso/api/usage/monthly` body: `{MeterNumber, GnnId (int!), AccountId}`
- Hourly: `POST /connectorsso/api/usage/hourly` body: `{MeterNumber, GnnId (int!), AccountNumber, ServiceLocation, BillCycle (full object from monthly)}`
- Verify: `GET /connectorsso/api/module/verify?accountId=X` (fire-and-forget, called before hourly)
- ServiceLocation = account number with last digit → "0" (fallback when API doesn't provide it)
- Usage values are CCF ≈ therms
- ForDate timestamps are Pacific time (converted to UTC)
- Current billing period: 03/16/26 - 04/14/26
- SoCal Gas website shows daily data with ~1-2 day delay

### Account Info
- Account: 0388795601, Meter: 15874691, GnnId: 388795600
- ServiceLocation: 0388795600
- Browserless at: browserless.thebirds.casa (external) / browserless.thebirds.com (internal k8s)
- SoCal Gas rate-limits login — avoid rapid repeated auth attempts
- Entity IDs use `socal_gas` (underscore), not `socalgas`

### Test Scripts (repo root, not committed to git)
- `test_local.py` — Tests auth + Green Button download (old, for debugging)
- `test_capture_api.py` — Browser capture of all smartcmobile.com API calls
- `test_usage_api.py` — Tests new monthly/daily/hourly endpoints directly

### Docs
- Design spec: `docs/superpowers/specs/2026-03-18-usage-api-rewrite-design.md`
- Implementation plan: `docs/superpowers/plans/2026-03-18-usage-api-rewrite.md`
- Explanation for users: `docs/explain.md`
