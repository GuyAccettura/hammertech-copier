"""
HammerTech Playwright authentication.

Uses the centralised auth server to log in headlessly and extract the
per-instance session cookie, then optionally fetches a dev-API bearer token
for accessing the IssueTypes / ObservationTypes endpoint.
"""

import time
import requests

AUTH_BASE = "https://us-auth.hammertechonline.com"
DEV_API_BASE = "https://us-api.hammertechonline.com"


def get_auth_cookie_playwright(instance: str, email: str, password: str) -> str:
    """
    Launch a headless Chromium browser, log into HammerTech for *instance*,
    and return the HAMMERTECHAUTH1 session cookie as a 'name=value' string.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    cookie_name_upper = f"HAMMERTECHAUTH1{instance.upper()}.HAMMERTECHONLINE.COM"
    login_url = f"{AUTH_BASE}/Login/LoginUser?tenant={instance}&returnUrl="
    instance_host = f"{instance}.hammertechonline.com"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=20_000)

            # Step 1: email
            page.wait_for_selector('[name="email"]', timeout=10_000)
            page.fill('[name="email"]', email)
            page.keyboard.press("Enter")

            # Step 2: password (may appear on a separate page)
            try:
                page.wait_for_selector('[name="password"]', timeout=10_000)
            except PWTimeout:
                raise ValueError(
                    f"Password field not found after submitting email for '{instance}'. "
                    "The login page structure may have changed."
                )
            page.fill('[name="password"]', password)
            page.keyboard.press("Enter")

            # Step 3: wait for redirect back to the tenant instance
            try:
                page.wait_for_url(f"**{instance_host}**", timeout=20_000)
            except PWTimeout:
                raise ValueError(
                    f"Never redirected to {instance_host} after login. "
                    "Check credentials and instance name."
                )

            # Step 4: poll for the auth cookie (SPA may set it asynchronously)
            for _ in range(20):
                for c in context.cookies():
                    if c["name"].upper() == cookie_name_upper:
                        return f"{c['name']}={c['value']}"
                time.sleep(1)

            found = [c["name"] for c in context.cookies()]
            raise ValueError(
                f"Auth cookie '{cookie_name_upper}' not found after login. "
                f"Cookies present: {found}"
            )
        finally:
            browser.close()


def get_bearer_token(instance: str, email: str, password: str) -> str:
    """
    Obtain a short-lived JWT from the HammerTech developer auth API.
    Used to call the /api/v1/IssueTypes endpoint (no Playwright needed).
    """
    r = requests.post(
        f"{AUTH_BASE}/api/login/generatetoken",
        json={"email": email, "password": password, "tenant": instance},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    token = r.json().get("token")
    if not token:
        raise ValueError(
            f"No token in response for '{instance}'. "
            f"Response: {r.text[:300]}"
        )
    return token
