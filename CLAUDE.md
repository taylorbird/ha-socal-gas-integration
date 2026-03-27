# SoCal Gas HA Integration

## Project Status (2026-03-26)

### What's Working
- **Auth:** Browser login via Browserless, token capture, GNN mapping
- **Data import:** Billing cycle fetch (completed + open), hourly data download, statistics import
- **All 5 sensors:** status, cost_this_month, usage_this_month, cost_today, usage_today
- **Statistics:** 2 external entries (gas_consumption_home, gas_cost_home) + 4 recorder entries
- **Energy dashboard:** Gas tab shows data including current open billing cycle
- **Hourly refresh:** Default update interval is 1 hour
- **Open cycle data:** Hourly API returns near real-time data for the current billing period
- **Clean startup:** No errors, warnings, or task exceptions
- **Logo:** Blue gas flame icon

### Known Issues / Next Steps

1. **Logo not loading in HA UI** — icon.png/logo.png files exist in the repo but HA/HACS isn't serving them. May need to submit to home-assistant/brands repo.

2. **SoCal Gas rate-limits login** — Each refresh triggers a full browser auth. With 1-hour refresh this means 24 logins/day. Monitor for rate-limit blocks.

### Key API Details (discovered through testing)
- Monthly: `POST /connectorsso/api/usage/monthly` body: `{MeterNumber, GnnId (int!), AccountId}`
- Hourly: `POST /connectorsso/api/usage/hourly` body: `{MeterNumber, GnnId (int!), AccountNumber, ServiceLocation, BillCycle (full object from monthly)}`
- Verify: `GET /connectorsso/api/module/verify?accountId=X` (fire-and-forget, called before hourly)
- ServiceLocation = account number with last digit → "0" (fallback when API doesn't provide it)
- Usage values are CCF ≈ therms
- ForDate timestamps are Pacific time (converted to UTC)
- SoCal Gas website shows near real-time hourly data
- Hourly API returns data for open (unbilled) cycles

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
