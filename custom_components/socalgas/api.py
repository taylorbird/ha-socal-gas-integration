"""SoCal Gas API client for automated data fetching."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import aiohttp

_LOGGER = logging.getLogger(__name__)

SMARTCMOBILE_BASE = "https://socal.smartcmobile.com"

ACCOUNT_LIST_URL = f"{SMARTCMOBILE_BASE}/connectorsso/api/account/list"
GNN_MAPPING_URL = f"{SMARTCMOBILE_BASE}/connectorsso/api/usage/gnnmapping"

SMARTCMOBILE_HEADERS = {
    "PortalType": "R",
    "Module": "",
    "X-SEW-CallerType": "socal",
}


class SoCalGasAuthError(Exception):
    """Raised when authentication fails."""


class SoCalGasConnectionError(Exception):
    """Raised when a connection error occurs."""


@dataclass
class AccountInfo:
    """Account and meter information discovered during login."""

    account_number: str  # 10-digit account number
    meter_number: str
    gnn_id: int
    service_location: str


class SoCalGasAPI:
    """Client for the SoCal Gas API.

    Authentication is handled by Browserless Chrome (browser
    automation via /function API) which captures the AccessToken.
    Data downloads use plain HTTP with the captured token.
    """

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
        browserless_url: str | None = None,
    ) -> None:
        """Initialize the API client."""
        self._username = username
        self._password = password
        self._external_session = session is not None
        self._session = session
        self._browserless_url = browserless_url
        self._access_token: str | None = None
        self._account_info: AccountInfo | None = None

    @property
    def account_info(self) -> AccountInfo | None:
        """Return discovered account info."""
        return self._account_info

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session with cookie jar."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar()
            )
        return self._session

    async def close(self) -> None:
        """Close the session if we own it."""
        if not self._external_session and self._session and not self._session.closed:
            await self._session.close()

    async def authenticate(self) -> AccountInfo:
        """Perform full authentication flow and return account info.

        Uses Browserless Chrome to handle the socalgas.com login
        (which requires client-side JavaScript).
        """
        from .browser import browser_authenticate

        if not self._browserless_url:
            raise SoCalGasAuthError(
                "Browserless Chrome is not configured. "
                "Set the Browserless URL in the integration config "
                "(e.g. http://browserless:3000) and ensure the Browserless "
                "container or add-on is running."
            )

        access_token, account_number = await browser_authenticate(
            self._browserless_url, self._username, self._password
        )
        self._access_token = access_token

        # If browser didn't capture account number, get it via SmartCMobile API
        if not account_number:
            session = await self._ensure_session()
            account_number = await self._get_account_number_smartcmobile(
                session
            )

        # Use the captured token for GNN mapping via plain HTTP
        session = await self._ensure_session()
        account_info = await self._get_gnn_mapping(session, account_number)
        self._account_info = account_info
        return account_info

    async def _get_account_number_smartcmobile(
        self, session: aiohttp.ClientSession
    ) -> str:
        """Get the account number via SmartCMobile using the AccessToken."""
        try:
            async with session.post(
                ACCOUNT_LIST_URL,
                headers={
                    **SMARTCMOBILE_HEADERS,
                    "AccessToken": self._access_token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            ) as resp:
                _LOGGER.debug(
                    "SmartCMobile account list status: %s", resp.status
                )
                if resp.status == 200:
                    data = await resp.json()
                    _LOGGER.debug("SmartCMobile account list response: %s", data)
                    # Response is a list of account objects
                    accounts = data if isinstance(data, list) else data.get(
                        "billAccounts", data.get("accounts", [])
                    ) if isinstance(data, dict) else []
                    if accounts:
                        acct = accounts[0]
                        if isinstance(acct, dict):
                            number = str(
                                acct.get("Id", "")
                                or acct.get("BillAccount", "")
                                or acct.get("billAccountNumber", "")
                                or acct.get("accountNumber", "")
                            )
                            if len(number) == 11:
                                number = number[:10]
                            if number:
                                return number
        except (aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("Could not get SmartCMobile account list: %s", err)

        raise SoCalGasAuthError(
            "Could not determine account number. "
            "Please provide it manually or use file upload."
        )

    async def _get_gnn_mapping(
        self, session: aiohttp.ClientSession, account_number: str
    ) -> AccountInfo:
        """Get GNN mapping to find meter number and GNN ID."""
        try:
            async with session.post(
                GNN_MAPPING_URL,
                json={"BillAccount": account_number},
                headers={
                    **SMARTCMOBILE_HEADERS,
                    "AccessToken": self._access_token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            ) as resp:
                if resp.status != 200:
                    raise SoCalGasConnectionError(
                        f"GNN mapping failed with status {resp.status}"
                    )
                data = await resp.json()
                _LOGGER.debug("GNN mapping response: %s", data)

                # Parse the response - structure may vary
                if isinstance(data, dict) and "GnnMeterMap" in data:
                    # Wrapped format: {"GnnMeterMap": [{...}]}
                    meter_list = data["GnnMeterMap"]
                    if isinstance(meter_list, list) and meter_list:
                        mapping = meter_list[0]
                    else:
                        raise SoCalGasConnectionError(
                            f"Empty GnnMeterMap in response: {data}"
                        )
                elif isinstance(data, list) and data:
                    mapping = data[0]
                elif isinstance(data, dict):
                    mapping = data
                else:
                    raise SoCalGasConnectionError(
                        f"Unexpected GNN mapping response: {data}"
                    )

                _LOGGER.warning("GNN mapping keys: %s", list(mapping.keys()))

                # GnnId must be a valid integer
                gnn_id_raw = mapping.get("GnnId") or mapping.get("gnnId")
                if gnn_id_raw is None:
                    raise SoCalGasConnectionError(
                        f"GNN mapping missing GnnId: {mapping}"
                    )
                try:
                    gnn_id = int(gnn_id_raw)
                except (TypeError, ValueError) as err:
                    raise SoCalGasConnectionError(
                        f"GNN mapping GnnId is not a valid integer: {gnn_id_raw}"
                    ) from err

                # Service location: prefer API value, fall back to derivation
                service_location = str(
                    mapping.get("ServiceLocationId", "")
                    or mapping.get("serviceLocationId", "")
                    or mapping.get("ServicePointId", "")
                    or mapping.get("servicePointId", "")
                )
                if not service_location:
                    # Derive from account number: replace last digit with 0
                    service_location = account_number[:-1] + "0"

                meter_number = str(
                    mapping.get("MeterNumber", "")
                    or mapping.get("meterNumber", "")
                )

                _LOGGER.warning(
                    "GNN mapping result: gnn=%s, sl=%s, meter=%s",
                    gnn_id, service_location, meter_number,
                )
                return AccountInfo(
                    account_number=account_number,
                    meter_number=meter_number,
                    gnn_id=gnn_id,
                    service_location=service_location,
                )
        except aiohttp.ClientError as err:
            raise SoCalGasConnectionError(
                f"GNN mapping error: {err}"
            ) from err

    async def verify_account(self) -> None:
        """Fire-and-forget account verification. Logs status, never raises."""
        try:
            if not self._access_token or not self._account_info:
                _LOGGER.debug("verify_account: not authenticated, skipping")
                return
            session = await self._ensure_session()
            info = self._account_info
            url = (
                f"{SMARTCMOBILE_BASE}/connectorsso/api/module/verify"
                f"?accountId={info.account_number}"
            )
            async with session.get(
                url,
                headers={
                    **SMARTCMOBILE_HEADERS,
                    "AccessToken": self._access_token,
                    "Content-Type": "application/json",
                },
            ) as resp:
                _LOGGER.debug("verify_account status: %s", resp.status)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("verify_account failed", exc_info=True)

    async def fetch_monthly(self) -> list[dict]:
        """Fetch monthly billing cycle data.

        Returns a list of billing cycle dicts.
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
                # Check for API-level error codes
                if isinstance(data, dict) and data.get("Code") and data["Code"] != 200:
                    raise SoCalGasConnectionError(
                        f"Monthly usage API error (Code {data['Code']}): "
                        f"{data.get('Message', '')}"
                    )
                # Return the billing cycles list
                # API returns {"Billing": {"BillingCycles": [...]}}
                if isinstance(data, dict):
                    billing = data.get("Billing", data)
                    if isinstance(billing, dict):
                        return billing.get("BillingCycles", billing.get("billingCycles", []))
                    return data.get("BillingCycles", data.get("billingCycles", []))
                if isinstance(data, list):
                    return data
                return []
        except aiohttp.ClientError as err:
            raise SoCalGasConnectionError(
                f"Monthly usage request error: {err}"
            ) from err

    async def fetch_hourly(self, billing_cycle: dict) -> list[dict]:
        """Fetch hourly usage data for a billing cycle.

        Args:
            billing_cycle: A billing cycle dict (from fetch_monthly).

        Returns:
            List of hourly reading dicts from "HourlyUsage" key.
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
                # Check for API-level error codes
                if isinstance(data, dict) and data.get("Code") and data["Code"] != 200:
                    raise SoCalGasConnectionError(
                        f"Hourly usage API error (Code {data['Code']}): "
                        f"{data.get('Message', '')}"
                    )
                if isinstance(data, dict):
                    return data.get("HourlyUsage", [])
                return []
        except aiohttp.ClientError as err:
            raise SoCalGasConnectionError(
                f"Hourly usage request error: {err}"
            ) from err
