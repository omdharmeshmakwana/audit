"""One Playwright smoke test for the UI (spec §1): a real browser against a real server on a
throwaway database. Skipped when Playwright or its browser isn't installed."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

SERVER_DIR = Path(__file__).resolve().parents[1]
DB = "faible_e2e"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    data = tmp_path_factory.mktemp("e2e-data")
    env = {
        **os.environ,
        "FAIBLE_DATABASE_URL": f"postgresql+psycopg:///{DB}",
        "FAIBLE_DATA_DIR": str(data),
    }
    run = lambda *a: subprocess.run(a, cwd=SERVER_DIR, env=env, check=True, capture_output=True)  # noqa: E731
    subprocess.run(["dropdb", "--if-exists", DB], check=True, capture_output=True)
    subprocess.run(["createdb", DB], check=True, capture_output=True)
    run(sys.executable, "-m", "alembic", "upgrade", "head")
    run(sys.executable, "-m", "faible.cli", "seed")
    run(sys.executable, "-m", "faible.cli", "fingerprint")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "faible.main:app", "--port", str(port)],
        cwd=SERVER_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    try:
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        subprocess.run(["dropdb", "--if-exists", DB], check=False, capture_output=True)


def test_login_project_shot_chain_asset(server: str) -> None:
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser binary not installed
            pytest.skip(f"no Playwright browser: {exc}")
        page = browser.new_page()
        page.goto(f"{server}/")
        assert page.url.endswith("/login")
        page.fill("input[name=email]", "artist1@studio")
        page.fill("input[name=password]", "changeme")
        page.click("button[type=submit]")
        page.wait_for_url("**/projects/*")
        assert page.locator("text=SQ01_SH010").first.is_visible()

        page.click("text=SQ01_SH010")
        page.wait_for_url("**/shots/*")
        page.get_by_text("Chain for v1").wait_for()
        assert page.locator(".step").count() >= 4  # origin + generate + variation + upscale
        assert page.locator(".prompt ins").count() > 0  # word diff rendered
        assert (
            page.locator(".versions img").first.evaluate("i => i.naturalWidth") > 0
        )  # image served

        page.locator(".step .io a").last.click()  # the upscale's output → asset page
        page.wait_for_url("**/assets/*")
        page.get_by_text("How it was made").wait_for()
        assert page.locator("text=Assert a link").is_visible()
        browser.close()
