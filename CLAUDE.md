# SoCal Gas HA Integration

## Project Status (2026-03-18)

### Just Completed: Usage API Rewrite (v0.4.0)
The Green Button ZIP download endpoint broke (HTTP 500). We reverse-engineered the actual SoCal Gas website API endpoints via browser network capture and rewrote the data fetching layer.

**What was done:**
- Replaced broken `greenbuttonservices/api/greenbutton/zipfile` with working `connectorsso/api/usage/monthly` + `hourly` endpoints
- New billing-cycle-based fetching: monthly endpoint returns cycles, hourly fetches per cycle
- Added `usage_parser.py` for hourly API → GreenButtonReading conversion (Pacific→UTC timezone handling)
- Rewrote coordinator: billing-cycle fetch instead of 30-day date-range chunks
- Simplified redownload: resets `initial_import_done` flag instead of date picker
- Added sensor platform: status, usage today, cost today, usage this month, cost this month
- Browser auth improvements: shadow DOM readiness polling, interstitial auto-dismiss, increased timeouts
- 33 tests passing

**Pushed to GitHub, needs HACS redownload + HA restart to test.**

### Next Steps
1. **Test on live HA** — Redownload via HACS, restart, check logs for successful billing cycle import
2. **Verify sensors populate** — Developer Tools → States, search `socal_gas`
3. **Verify statistics** — Developer Tools → Statistics, search `socal_gas`
4. **If issues:** Check logs for "Fetched X billing cycles" and hourly data
5. **Dashboard setup** — statistics-graph card or use the new sensor entities

### Key API Details (discovered through testing)
- Monthly: `POST /connectorsso/api/usage/monthly` body: `{MeterNumber, GnnId (int!), AccountId}`
- Hourly: `POST /connectorsso/api/usage/hourly` body: `{MeterNumber, GnnId (int!), AccountNumber, ServiceLocation, BillCycle (full object from monthly)}`
- Verify: `GET /connectorsso/api/module/verify?accountId=X` (fire-and-forget, called before hourly)
- ServiceLocation = account number with last digit → "0" (fallback when API doesn't provide it)
- Usage values are CCF ≈ therms
- ForDate timestamps are Pacific time (converted to UTC)

### Account Info
- Account: 0388795601, Meter: 15874691, GnnId: 388795600
- ServiceLocation: 0388795600
- Browserless at: browserless.thebirds.casa (external) / browserless.thebirds.com (internal k8s)
- SoCal Gas rate-limits login — avoid rapid repeated auth attempts

### Test Scripts (repo root, not committed to git)
- `test_local.py` — Tests auth + Green Button download (old, for debugging)
- `test_capture_api.py` — Browser capture of all smartcmobile.com API calls
- `test_usage_api.py` — Tests new monthly/daily/hourly endpoints directly

### Docs
- Design spec: `docs/superpowers/specs/2026-03-18-usage-api-rewrite-design.md`
- Implementation plan: `docs/superpowers/plans/2026-03-18-usage-api-rewrite.md`
- Explanation for users: `docs/explain.md`
