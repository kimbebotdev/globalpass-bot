import argparse
import asyncio
import os
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from dotenv import load_dotenv

load_dotenv()


LOGIN_URL = "https://signon.ual.com/oamfed/idp/initiatesso?providerid=DPmyidtravel"


async def perform_login(headless: bool, screenshot: str | None) -> None:
    username = os.getenv("UAL_USERNAME")
    password = os.getenv("UAL_PASSWORD")

    if not username or not password:
        raise SystemExit("Set UAL_USERNAME and UAL_PASSWORD in your environment before running.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await page.fill("#username", username)
        await page.fill("#password", password)
        await page.click("input[type=submit][value='Login']")

        try:
            # Wait for redirect away from the login form.
            await page.wait_for_url(lambda url: "login" not in url, timeout=15000)
        except PlaywrightTimeout:
            # If the URL never changed we still try to capture the resulting state.
            pass

        await page.wait_for_load_state("networkidle")

        if screenshot:
            await page.screenshot(path=screenshot, full_page=True)

        await context.storage_state(path="auth_state.json")
        await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automate United sign-on with Playwright.")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode for debugging.",
    )
    parser.add_argument(
        "--screenshot",
        default="post_login.png",
        help="Path to save a screenshot after login (set empty string to disable).",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    screenshot = args.screenshot if args.screenshot else None
    await perform_login(headless=not args.headed, screenshot=screenshot)


if __name__ == "__main__":
    asyncio.run(main())
