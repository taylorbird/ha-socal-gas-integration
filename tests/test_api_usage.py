"""Tests for the SoCal Gas API usage methods (fetch_monthly, fetch_hourly, verify_account)."""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock homeassistant modules before importing api.py
sys.modules.setdefault("homeassistant", MagicMock())
sys.modules.setdefault("homeassistant.config_entries", MagicMock())
sys.modules.setdefault("homeassistant.core", MagicMock())

from custom_components.socalgas.api import (  # noqa: E402
    AccountInfo,
    SoCalGasAPI,
    SoCalGasAuthError,
    SoCalGasConnectionError,
)


def _make_authenticated_api() -> SoCalGasAPI:
    """Create an authenticated API instance with mock session."""
    api = SoCalGasAPI(
        "test@email.com", "testpassword",
        browserless_url="http://browserless:3000",
    )
    api._access_token = "fake-token"
    api._account_info = AccountInfo(
        account_number="1408090780",
        meter_number="03894524",
        gnn_id=1408090700,
        service_location="1408090700",
    )
    return api


class _MockResponse:
    """Minimal mock for aiohttp response used as async context manager."""

    def __init__(self, status: int, json_data=None, text_data: str = ""):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestFetchMonthly:
    """Tests for the fetch_monthly method."""

    def test_fetch_monthly_success(self):
        """Test successful monthly billing cycle fetch."""
        api = _make_authenticated_api()
        billing_cycles = [
            {"StartDate": "01/01/2025", "EndDate": "02/01/2025", "Usage": 45.2},
            {"StartDate": "02/01/2025", "EndDate": "03/01/2025", "Usage": 38.7},
        ]
        mock_resp = _MockResponse(200, json_data={"Code": 200, "BillingCycles": billing_cycles})
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        api._session = mock_session

        result = asyncio.get_event_loop().run_until_complete(api.fetch_monthly())
        assert result == billing_cycles
        # Verify the POST was called with correct URL
        call_args = mock_session.post.call_args
        assert "usage/monthly" in call_args[0][0]
        # Verify GnnId is sent as int
        assert call_args[1]["json"]["GnnId"] == 1408090700

    def test_fetch_monthly_auth_error(self):
        """Test 401 response raises SoCalGasAuthError."""
        api = _make_authenticated_api()
        mock_resp = _MockResponse(401)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        api._session = mock_session

        with pytest.raises(SoCalGasAuthError, match="AccessToken expired"):
            asyncio.get_event_loop().run_until_complete(api.fetch_monthly())

    def test_fetch_monthly_api_error_code(self):
        """Test API error code (Code != 200) raises SoCalGasConnectionError."""
        api = _make_authenticated_api()
        mock_resp = _MockResponse(
            200,
            json_data={"Code": 701, "Message": "Session expired"},
        )
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        api._session = mock_session

        with pytest.raises(SoCalGasConnectionError, match="Code 701"):
            asyncio.get_event_loop().run_until_complete(api.fetch_monthly())

    def test_fetch_monthly_non_200_status(self):
        """Test non-200 HTTP status raises SoCalGasConnectionError."""
        api = _make_authenticated_api()
        mock_resp = _MockResponse(500, text_data="Internal Server Error")
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        api._session = mock_session

        with pytest.raises(SoCalGasConnectionError, match="500"):
            asyncio.get_event_loop().run_until_complete(api.fetch_monthly())


class TestFetchHourly:
    """Tests for the fetch_hourly method."""

    def test_fetch_hourly_success(self):
        """Test successful hourly usage fetch."""
        api = _make_authenticated_api()
        hourly_data = [
            {"Hour": "00:00", "Usage": 0.1},
            {"Hour": "01:00", "Usage": 0.2},
            {"Hour": "02:00", "Usage": 0.15},
        ]
        mock_resp = _MockResponse(200, json_data={"Code": 200, "HourlyUsage": hourly_data})
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        api._session = mock_session

        billing_cycle = {"StartDate": "01/01/2025", "EndDate": "02/01/2025"}
        result = asyncio.get_event_loop().run_until_complete(
            api.fetch_hourly(billing_cycle)
        )
        assert result == hourly_data
        # Verify the POST was called with correct URL and body
        call_args = mock_session.post.call_args
        assert "usage/hourly" in call_args[0][0]
        body = call_args[1]["json"]
        assert body["GnnId"] == 1408090700
        assert body["ServiceLocation"] == "1408090700"
        assert body["BillCycle"] == billing_cycle

    def test_fetch_hourly_not_authenticated(self):
        """Test fetch_hourly raises when not authenticated."""
        api = SoCalGasAPI(
            "test@email.com", "testpassword",
            browserless_url="http://browserless:3000",
        )
        # No token set
        with pytest.raises(SoCalGasAuthError, match="Must authenticate"):
            asyncio.get_event_loop().run_until_complete(
                api.fetch_hourly({"StartDate": "01/01/2025"})
            )

    def test_fetch_hourly_auth_error(self):
        """Test 401 response raises SoCalGasAuthError."""
        api = _make_authenticated_api()
        mock_resp = _MockResponse(401)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        api._session = mock_session

        with pytest.raises(SoCalGasAuthError, match="AccessToken expired"):
            asyncio.get_event_loop().run_until_complete(
                api.fetch_hourly({"StartDate": "01/01/2025"})
            )


class TestVerifyAccount:
    """Tests for the verify_account method."""

    def test_verify_account_does_not_raise_on_500(self):
        """Test verify_account swallows errors (fire-and-forget)."""
        api = _make_authenticated_api()
        mock_resp = _MockResponse(500)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        api._session = mock_session

        # Should not raise
        asyncio.get_event_loop().run_until_complete(api.verify_account())

    def test_verify_account_does_not_raise_when_not_authenticated(self):
        """Test verify_account silently returns when not authenticated."""
        api = SoCalGasAPI(
            "test@email.com", "testpassword",
            browserless_url="http://browserless:3000",
        )
        # No token, no account info -- should just return without error
        asyncio.get_event_loop().run_until_complete(api.verify_account())

    def test_verify_account_success(self):
        """Test verify_account completes without error on 200."""
        api = _make_authenticated_api()
        mock_resp = _MockResponse(200)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        api._session = mock_session

        asyncio.get_event_loop().run_until_complete(api.verify_account())
        # Verify the GET was called with the correct URL
        call_args = mock_session.get.call_args
        assert "module/verify" in call_args[0][0]
        assert "accountId=1408090780" in call_args[0][0]
