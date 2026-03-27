"""Browser authentication via Browserless Chrome /function API.

Sends Puppeteer JavaScript to a Browserless Chrome instance over HTTP.
The JS runs headless Chromium to handle the socalgas.com login flow,
captures the AccessToken from SmartCMobile requests, and returns it.

Once the AccessToken is obtained, all subsequent SmartCMobile API calls
(gnnmapping, Green Button download) work with plain HTTP.
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Puppeteer code executed inside Browserless via /function endpoint.
# Receives {page, context} where context = {username, password}.
# Returns JSON with access_token + account_number, or error + error_type.
_LOGIN_JS = r"""
export default async ({ page, context }) => {
  const { username, password } = context;

  let accessToken = null;
  let accountNumber = null;

  // Capture AccessToken from outgoing SmartCMobile requests
  page.on('request', (request) => {
    if (request.url().includes('smartcmobile.com')) {
      const headers = request.headers();
      if (headers['accesstoken']) {
        accessToken = headers['accesstoken'];
      }
    }
  });

  // Capture account number from API responses
  page.on('response', async (response) => {
    if (response.url().includes('get-bill-account-list') && response.status() === 200) {
      try {
        const data = await response.json();
        if (data && typeof data === 'object') {
          const accounts = data.billAccounts || data.accounts || [];
          if (Array.isArray(accounts) && accounts.length > 0) {
            const acct = accounts[0];
            let num = String(acct.billAccountNumber || acct.accountNumber || '');
            if (num.length === 11) num = num.slice(0, 10);
            if (num) accountNumber = num;
          }
        }
      } catch (e) { /* ignore parse errors */ }
    }
  });

  await page.setUserAgent(
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
  );

  // Step 1: Login page
  await page.goto('https://myaccount.socalgas.com/ui/login', {
    waitUntil: 'networkidle2',
    timeout: 90000,
  });

  // Step 2: Fill credentials using keyboard input
  // Login form uses web components (scg-text-field) with shadow DOM,
  // so regular selectors can't reach the inputs. Wait for shadow DOM
  // inputs to be available before interacting — on slow connections
  // the web components may not be ready immediately after networkidle2.
  const findInputs = () => {
    let emailInput = null, passInput = null;
    const fields = document.querySelectorAll('scg-text-field');
    for (const host of fields) {
      const sr = host.shadowRoot;
      if (!sr) continue;
      const inp = sr.querySelector('input');
      if (!inp) continue;
      if (inp.type === 'email' || inp.id === 'email') emailInput = inp;
      if (inp.type === 'password' || inp.id === 'password') passInput = inp;
    }
    return emailInput && passInput;
  };

  // Poll for up to 30s for the shadow DOM inputs to appear
  try {
    await page.waitForFunction(findInputs, { timeout: 30000 });
  } catch (e) {
    return {
      data: { error: 'Login form did not become ready within 30s — site may be experiencing issues', error_type: 'connection' },
      type: 'application/json',
    };
  }

  // Extra settle time for web component framework
  await new Promise(r => setTimeout(r, 1000));

  const emailHandle = await page.evaluateHandle(() => {
    const fields = document.querySelectorAll('scg-text-field');
    for (const host of fields) {
      const sr = host.shadowRoot;
      if (!sr) continue;
      const inp = sr.querySelector('input');
      if (inp && (inp.type === 'email' || inp.id === 'email')) return inp;
    }
    return null;
  });

  const passHandle = await page.evaluateHandle(() => {
    const fields = document.querySelectorAll('scg-text-field');
    for (const host of fields) {
      const sr = host.shadowRoot;
      if (!sr) continue;
      const inp = sr.querySelector('input');
      if (inp && (inp.type === 'password' || inp.id === 'password')) return inp;
    }
    return null;
  });

  const inputsFound = await page.evaluate(
    (e, p) => !!e && !!p, emailHandle, passHandle
  );
  if (!inputsFound) {
    return {
      data: { error: 'Could not find login form fields on page', error_type: 'connection' },
      type: 'application/json',
    };
  }

  // Focus and type into each field — real keyboard events work with
  // the web component framework unlike programmatic value setting.
  await emailHandle.focus();
  await page.keyboard.type(username, { delay: 30 });
  await passHandle.focus();
  await page.keyboard.type(password, { delay: 30 });

  await new Promise(r => setTimeout(r, 500));

  // Find and click the "Log In" button inside scg-button shadow DOM.
  // There are multiple scg-button elements on the page; target by text.
  const clicked = await page.evaluate(() => {
    const scgBtns = document.querySelectorAll('scg-button');
    for (const host of scgBtns) {
      const sr = host.shadowRoot;
      if (!sr) continue;
      const b = sr.querySelector('button');
      if (b && b.textContent.trim() === 'Log In') {
        b.click();
        return true;
      }
    }
    // Fallback: any submit button
    const btn = document.querySelector('button[type="submit"]');
    if (btn) { btn.click(); return true; }
    return false;
  });

  if (!clicked) {
    return {
      data: { error: 'Could not find submit button on login page', error_type: 'connection' },
      type: 'application/json',
    };
  }

  // Step 3: Wait for redirect away from login
  try {
    await page.waitForFunction(
      () => !window.location.href.includes('/ui/login'),
      { timeout: 90000 }
    );
  } catch (e) {
    // Before blaming credentials, check if there's an error message on the page
    const pageError = await page.evaluate(() => {
      // Check for visible error messages in common patterns
      const errorEls = document.querySelectorAll(
        '.error-message, .alert-danger, [role="alert"], .scg-error, .error-text'
      );
      for (const el of errorEls) {
        const text = el.textContent?.trim();
        if (text) return text;
      }
      // Check shadow DOM of scg-* elements for error states
      const allScg = document.querySelectorAll('[class*="error"], [class*="Error"]');
      for (const el of allScg) {
        const text = el.textContent?.trim();
        if (text) return text;
      }
      return null;
    });

    const detail = pageError
      ? `Login page error: ${pageError}`
      : 'Login did not redirect within 90s. The site may be slow or your credentials may be incorrect.';

    return {
      data: {
        error: detail,
        error_type: pageError ? 'auth' : 'connection',
      },
      type: 'application/json',
    };
  }

  // Let the page settle
  await new Promise(r => setTimeout(r, 3000));

  // Check for error page (rate limiting)
  if (page.url().includes('/ui/error')) {
    return {
      data: {
        error: 'Login redirected to error page — site may be rate-limiting. Try again in a few minutes.',
        error_type: 'connection',
      },
      type: 'application/json',
    };
  }

  // Step 3.5: Detect and attempt to dismiss interstitial popups
  // SoCal Gas periodically shows interstitials for MFA enrollment,
  // contact info confirmation, etc. Many have a "skip" or "remind me
  // later" option we can click to proceed without user intervention.
  if (page.url().includes('/ui/interstitials')) {
    const dismissed = await page.evaluate(() => {
      // Look for dismiss/skip/remind-me-later buttons in both regular
      // DOM and shadow DOM (scg-button web components).
      const dismissPatterns = [
        /remind me later/i,
        /skip/i,
        /not now/i,
        /maybe later/i,
        /no thanks/i,
        /close/i,
        /dismiss/i,
      ];

      // Check scg-button shadow DOM elements
      const scgBtns = document.querySelectorAll('scg-button');
      for (const host of scgBtns) {
        const sr = host.shadowRoot;
        if (!sr) continue;
        const b = sr.querySelector('button');
        if (!b) continue;
        const text = b.textContent.trim();
        if (dismissPatterns.some(p => p.test(text))) {
          b.click();
          return text;
        }
      }

      // Check regular buttons and links
      const clickables = document.querySelectorAll('button, a, [role="button"]');
      for (const el of clickables) {
        const text = el.textContent.trim();
        if (dismissPatterns.some(p => p.test(text))) {
          el.click();
          return text;
        }
      }

      return null;
    });

    if (dismissed) {
      // Wait for navigation after dismissing
      await new Promise(r => setTimeout(r, 3000));
      try {
        await page.waitForFunction(
          () => !window.location.href.includes('/ui/interstitials'),
          { timeout: 15000 }
        );
      } catch (e) {
        // Dismiss clicked but didn't navigate away — still stuck
      }
      await new Promise(r => setTimeout(r, 2000));
    }

    // If still on interstitials after attempted dismiss, give up
    if (page.url().includes('/ui/interstitials')) {
      return {
        data: {
          error: 'SoCal Gas is showing a required prompt (MFA setup, account verification, etc.) that cannot be skipped. Please log in to myaccount.socalgas.com in a browser, complete the prompt, then retry.',
          error_type: 'interstitial',
        },
        type: 'application/json',
      };
    }
  }

  // Step 4: Navigate to usage page to trigger SSO + AccessToken
  await page.goto('https://myaccount.socalgas.com/ui/ways-to-save/analyze-usage', {
    waitUntil: 'networkidle2',
    timeout: 90000,
  });

  // Wait for SmartCMobile requests to fire
  await new Promise(r => setTimeout(r, 3000));

  if (!accessToken) {
    await new Promise(r => setTimeout(r, 5000));
  }

  if (!accessToken) {
    return {
      data: {
        error: 'Could not capture AccessToken from browser session. The usage page may not have loaded the energy widget.',
        error_type: 'auth',
      },
      type: 'application/json',
    };
  }

  return {
    data: { access_token: accessToken, account_number: accountNumber || '' },
    type: 'application/json',
  };
};
"""


def _build_function_url(base_url: str, timeout_ms: int = 180000) -> str:
    """Build the /function endpoint URL from the Browserless base URL.

    Handles base URLs with or without query params (e.g. token).
    Adds a timeout parameter so Browserless doesn't kill the session early.
    http://browserless:3000?token=X -> http://browserless:3000/function?token=X&timeout=120000
    """
    parsed = urlparse(base_url)
    new_path = parsed.path.rstrip("/") + "/function"
    # Merge existing query params with timeout
    params = parse_qs(parsed.query)
    params["timeout"] = [str(timeout_ms)]
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(path=new_path, query=new_query))


async def browser_authenticate(
    browserless_url: str, username: str, password: str
) -> tuple[str, str]:
    """Authenticate via Browserless Chrome /function API.

    Args:
        browserless_url: Base URL of Browserless (e.g. http://browserless:3000).
        username: SoCal Gas username/email.
        password: SoCal Gas password.

    Returns:
        Tuple of (access_token, account_number).

    Raises:
        SoCalGasAuthError: If login credentials are wrong.
        SoCalGasConnectionError: If Browserless is unreachable or browser fails.
    """
    from .api import SoCalGasAuthError, SoCalGasConnectionError

    function_url = _build_function_url(browserless_url)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                function_url,
                json={
                    "code": _LOGIN_JS,
                    "context": {"username": username, "password": password},
                },
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                if resp.status == 401:
                    raise SoCalGasConnectionError(
                        "Browserless authentication failed. "
                        "If your Browserless instance requires a token, "
                        "append ?token=YOUR_TOKEN to the Browserless URL."
                    )
                if resp.status != 200:
                    text = await resp.text()
                    raise SoCalGasConnectionError(
                        f"Browserless error ({resp.status}): {text[:200]}"
                    )
                raw = await resp.json()
                # Browserless wraps the return in {"data": ..., "type": ...}
                data = raw.get("data", raw) if isinstance(raw, dict) else raw

    except SoCalGasConnectionError:
        raise
    except aiohttp.ClientError as err:
        raise SoCalGasConnectionError(
            f"Cannot connect to Browserless at {browserless_url}: {err}"
        ) from err
    except Exception as err:
        raise SoCalGasConnectionError(
            f"Browserless request failed: {err}"
        ) from err

    # Check for error responses from our JS code
    if "error" in data:
        error_msg = data["error"]
        error_type = data.get("error_type", "connection")
        if error_type in ("auth", "interstitial"):
            raise SoCalGasAuthError(error_msg)
        raise SoCalGasConnectionError(error_msg)

    access_token = data.get("access_token")
    if not access_token:
        raise SoCalGasAuthError(
            "Could not capture AccessToken from browser session."
        )

    account_number = data.get("account_number", "")
    if not account_number:
        _LOGGER.debug(
            "Could not capture account number from browser; "
            "it will need to be obtained via API"
        )

    return access_token, account_number
