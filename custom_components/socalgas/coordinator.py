"""DataUpdateCoordinator for SoCal Gas integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.persistent_notification import (
    async_create,
    async_dismiss,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import SoCalGasAPI, SoCalGasAuthError, SoCalGasConnectionError
from .const import (
    CONF_BROWSERLESS_URL,
    CONF_INITIAL_IMPORT_DONE,
    CONF_PASSWORD,
    CONF_REFRESH_INTERVAL_HOURS,
    CONF_USERNAME,
    DEFAULT_REFRESH_INTERVAL_HOURS,
    DOMAIN,
)
from .statistics import (
    async_get_existing_states,
    async_get_prior_sums,
    async_import_to_ha,
    merge_readings_with_existing,
    readings_to_hourly_statistics,
)
from .usage_parser import hourly_to_readings

_LOGGER = logging.getLogger(__name__)


class SoCalGasCoordinator(DataUpdateCoordinator):
    """Coordinator that fetches SoCal Gas data daily."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        interval_hours = entry.options.get(
            CONF_REFRESH_INTERVAL_HOURS, DEFAULT_REFRESH_INTERVAL_HOURS
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=interval_hours),
        )
        self.entry = entry
        self._api: SoCalGasAPI | None = None
        self._download_lock = asyncio.Lock()

    async def _async_update_data(self) -> dict:
        """Fetch new data from SoCal Gas.

        On first run after setup, imports all billing cycles with data.
        On subsequent runs, fetches the current open cycle plus the most
        recent completed cycle.
        """
        username = self.entry.data.get(CONF_USERNAME)
        password = self.entry.data.get(CONF_PASSWORD)
        if not username or not password:
            raise UpdateFailed("No credentials configured")

        browserless_url = self.entry.data.get(CONF_BROWSERLESS_URL)

        async with self._download_lock:
            return await self._do_update(username, password, browserless_url)

    async def _do_update(self, username: str, password: str, browserless_url: str | None = None) -> dict:
        """Run the actual update (must be called under _download_lock)."""
        _LOGGER.warning("Starting SoCal Gas data update")
        # Reuse the already-authenticated API from the config flow if
        # available, to avoid a second login that triggers rate limiting.
        pending_api = self.hass.data.get(DOMAIN, {}).pop("pending_api", None)
        if pending_api:
            _LOGGER.warning("Reusing authenticated API from config flow")
            api = pending_api
        else:
            _LOGGER.warning("Creating new API session, will authenticate")
            api = SoCalGasAPI(username, password, browserless_url=browserless_url)
        try:
            if not pending_api:
                _LOGGER.warning("Authenticating with SoCal Gas...")
                try:
                    account_info = await api.authenticate()
                    _LOGGER.warning("Authentication successful")
                except SoCalGasAuthError as err:
                    _LOGGER.error("Authentication failed: %s", err)
                    err_msg = str(err).lower()
                    if "interstitial" in err_msg or "confirm account" in err_msg:
                        async_create(
                            self.hass,
                            "SoCal Gas requires you to confirm account "
                            "information before data can be downloaded. "
                            "Please log in to socalgas.com in a browser, "
                            "address the popup, then restart the integration.",
                            title="SoCal Gas: Action Required",
                            notification_id="socalgas_interstitial",
                        )
                    if "invalid" in err_msg and "password" in err_msg:
                        raise ConfigEntryAuthFailed(str(err)) from err
                    raise UpdateFailed(str(err)) from err
                except SoCalGasConnectionError as err:
                    _LOGGER.error("Connection error during auth: %s", err)
                    raise UpdateFailed(str(err)) from err

            end_date = datetime.now(tz=timezone.utc)
            initial_import_done = self.entry.data.get(
                CONF_INITIAL_IMPORT_DONE, False
            )

            # Always fetch billing cycles first
            _LOGGER.warning("Fetching monthly billing cycles...")
            cycles = await api.fetch_monthly()
            _LOGGER.warning("Got %d billing cycles", len(cycles))
            await api.verify_account()

            if not initial_import_done:
                # Import all completed cycles with data + open cycles
                # (current billing period, not yet billed but has hourly data)
                completed = [
                    c for c in cycles if c.get("TotalServiceAmount", 0) > 0
                ]
                open_cycles = [
                    c for c in cycles if c.get("TotalServiceAmount", 0) == 0
                ]
                cycles_to_fetch = completed + open_cycles
                _LOGGER.warning(
                    "Initial import: %d completed + %d open billing cycles",
                    len(completed), len(open_cycles),
                )
            else:
                # Refresh: current open cycle + most recent completed
                cycles_to_fetch = self._pick_refresh_cycles(cycles)
                _LOGGER.warning(
                    "Refresh: fetching %d billing cycles",
                    len(cycles_to_fetch),
                )

            total_readings = await self._fetch_billing_cycles(
                api, cycles_to_fetch
            )

            # Persist the initial import flag
            if not initial_import_done:
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={
                        **self.entry.data,
                        CONF_INITIAL_IMPORT_DONE: True,
                    },
                )

            return {
                "last_update": end_date.isoformat(),
                "readings_count": total_readings,
            }
        finally:
            await api.close()

    def _name_slug(self) -> str:
        """Return the name slug for this entry."""
        return (
            self.entry.data.get("account_name", "home")
            .lower()
            .replace(" ", "_")
        )

    async def _get_latest_statistic_time(self) -> datetime | None:
        """Query HA recorder for the latest statistic timestamp."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            get_last_statistics,
        )

        statistic_id = f"{DOMAIN}:gas_consumption_{self._name_slug()}"

        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, False, {"start"}
        )
        if result and statistic_id in result and result[statistic_id]:
            row = result[statistic_id][0]
            ts = row["start"]
            if isinstance(ts, (int, float)):
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            return ts
        return None

    @staticmethod
    def _pick_refresh_cycles(cycles: list[dict]) -> list[dict]:
        """Pick billing cycles to fetch during a refresh.

        Returns all open cycles (no charge yet) plus the most recent
        completed cycle so we can catch late-arriving hourly data.
        """
        to_fetch = []
        completed = []
        for c in cycles:
            if c.get("TotalServiceAmount", 0) == 0:
                to_fetch.append(c)
            else:
                completed.append(c)
        if completed:
            completed.sort(key=lambda c: c.get("FromDate", ""), reverse=True)
            to_fetch.append(completed[0])
        return to_fetch

    async def async_redownload_all(self) -> None:
        """Re-download all billing cycles with data on demand."""
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
                cycles_to_fetch = [
                    c for c in cycles if c.get("TotalServiceAmount", 0) > 0
                ]
                await self._fetch_billing_cycles(
                    api, cycles_to_fetch, label="Re-download"
                )
            except (SoCalGasAuthError, SoCalGasConnectionError) as err:
                _LOGGER.error("Redownload failed: %s", err)
            finally:
                await api.close()

    async def _fetch_billing_cycles(
        self,
        api: SoCalGasAPI,
        cycles: list[dict],
        label: str = "Import",
    ) -> int:
        """Fetch hourly data for billing cycles and import. Returns total new readings.

        Downloads hourly data for each billing cycle, deduplicates readings
        by hour, then computes cumulative sums once over the complete dataset.
        This prevents sum discontinuities caused by overlapping data
        between adjacent billing cycles.
        """
        name_slug = self._name_slug()
        total_cycles = len(cycles)
        label_slug = label.lower().replace(" ", "_")
        notification_id = f"{DOMAIN}_{label_slug}_{name_slug}"

        # Phase 1: Download all cycles, collecting raw readings
        all_readings = []
        failed_count = 0

        for cycle_num, cycle in enumerate(cycles, start=1):
            from_date = cycle.get("FromDate", "?")
            to_date = cycle.get("ToDate", "?")

            _LOGGER.info(
                "Fetching billing cycle %d/%d: %s to %s",
                cycle_num, total_cycles, from_date, to_date,
            )

            async_create(
                self.hass,
                f"Fetching billing cycle {cycle_num} of {total_cycles}\n"
                f"({from_date} – {to_date})",
                title=f"SoCal Gas {label}",
                notification_id=notification_id,
            )

            try:
                hourly_data = await api.fetch_hourly(cycle)
                readings = hourly_to_readings(hourly_data)
            except SoCalGasAuthError as err:
                async_dismiss(self.hass, notification_id)
                raise ConfigEntryAuthFailed(str(err)) from err
            except SoCalGasConnectionError as err:
                _LOGGER.error(
                    "Connection error fetching cycle %d/%d (%s to %s): %s",
                    cycle_num, total_cycles, from_date, to_date, err,
                )
                failed_count += 1
                continue
            except Exception as err:
                _LOGGER.error(
                    "Error parsing cycle %d/%d (%s to %s): %s",
                    cycle_num, total_cycles, from_date, to_date, err,
                )
                failed_count += 1
                continue

            if readings:
                _LOGGER.warning(
                    "Cycle %d/%d: %d readings (%s to %s)",
                    cycle_num, total_cycles, len(readings),
                    readings[0].start.date(), readings[-1].start.date(),
                )
                all_readings.extend(readings)
            else:
                _LOGGER.warning(
                    "Cycle %d/%d: no data returned for %s to %s",
                    cycle_num, total_cycles, from_date, to_date,
                )

            # Rate-limit protection: pause between cycles (not after last)
            if cycle_num < total_cycles:
                _LOGGER.info("Sleeping 5s between cycles (rate-limit protection)")
                await asyncio.sleep(5)

        # Phase 2: Deduplicate downloaded readings by hour
        if not all_readings:
            async_dismiss(self.hass, notification_id)
            _LOGGER.warning("No readings in downloaded data")
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

        # Phase 3: Merge with existing data.
        # Query all existing statistics from the earliest new reading
        # onward. New readings take priority; existing hours that are
        # NOT in the download are kept so their sums stay consistent.
        earliest = unique_readings[0].start
        existing = await async_get_existing_states(
            self.hass, name_slug, earliest
        )
        merged = merge_readings_with_existing(unique_readings, existing)

        # Phase 4: Compute cumulative sums and import
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

        failure_info = ""
        if failed_count > 0:
            failure_info = f" ({failed_count} cycle(s) failed)"

        summary_msg = (
            f"{len(unique_readings)} new readings imported "
            f"({merged[0].start.strftime('%b %d, %Y')} – "
            f"{merged[-1].start.strftime('%b %d, %Y')})"
            f"{failure_info}"
        )
        _LOGGER.warning(
            "%s complete: %d new readings + %d existing merged (%s to %s)%s",
            label, len(unique_readings),
            len(merged) - len(unique_readings),
            merged[0].start.date(),
            merged[-1].start.date(),
            failure_info,
        )
        async_create(
            self.hass,
            summary_msg,
            title=f"SoCal Gas {label} Complete",
            notification_id=f"{notification_id}_done",
        )

        return len(unique_readings)
