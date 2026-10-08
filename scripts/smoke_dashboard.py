"""Authenticated local dashboard smoke check; do not print credentials or content."""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

root = Path(sys.argv[1]).resolve()
load_dotenv(root / ".env")
password = os.environ["DASHBOARD_PASSWORD"]
result = {"scope": "authenticated dashboard read-only navigation", "modes": {}}
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    page.goto("http://127.0.0.1:8501", wait_until="domcontentloaded")
    page.get_by_label("Şifre").fill(password, timeout=45000)
    page.get_by_role("button", name="Giriş yap").click()
    page.get_by_role("button", name="İlan Masası", exact=True).wait_for(timeout=45000)
    for mode in ["Bugün", "İlan Masası", "Başvurular", "Belgeler"]:
        page.get_by_role("button", name=mode, exact=True).click()
        page.wait_for_timeout(2500)
        result["modes"][mode] = {"streamlit_exceptions": page.locator('[data-testid="stException"]').count()}
    result["browser_error_count"] = len(errors)
    browser.close()
result["passed"] = not errors and all(item["streamlit_exceptions"] == 0 for item in result["modes"].values())
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result["passed"] else 1)
