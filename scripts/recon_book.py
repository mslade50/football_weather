"""Recon script: discover a sportsbook's underlying JSON API endpoints.

Adapted from golf_scraping/recon_betcris.py. Loads a page under Playwright +
stealth, logs every response whose URL or content-type looks like an API, and
optionally saves matching JSON bodies as raw fixtures.

    python scripts/recon_book.py --url https://www.betonline.ag/sportsbook/football/nfl \
        --keyword offering --out tests/fixtures/raw/betonline/

Only responses whose URL contains ``--keyword`` (case-insensitive) are written
to ``--out``; everything captured is still printed. Requires ``playwright`` and
``playwright-stealth`` (user-run locally; never in CI).
"""

import argparse
import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

API_HINTS = [
    "odds", "match", "event", "market", "football", "nfl", "ncaaf", "line",
    "sport", "offer", "fixture", "schedule", "league", "game",
    "graphql", "gateway", "v1", "v2", "v3",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)


def _slug(url: str, n: int) -> str:
    tail = re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[-1])[-80:].strip("_")
    return f"{n:03d}_{tail or 'response'}.json"


async def main(url: str, keyword: str | None, out: Path | None, wait_ms: int, headed: bool, click: str | None) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        captured: list[dict] = []
        saved: list[Path] = []
        kw = (keyword or "").lower()

        async def log_response(response) -> None:
            r_url = response.url
            ct = response.headers.get("content-type", "")
            if response.status != 200:
                return
            if not ("json" in ct or "api" in r_url.lower() or any(h in r_url.lower() for h in API_HINTS)):
                return
            try:
                body = await response.text()
            except Exception:
                captured.append({"url": r_url, "status": response.status, "size": 0})
                return
            is_json = body.strip()[:1] in ("{", "[")
            entry = {
                "url": r_url,
                "status": response.status,
                "size": len(body),
                "is_json": is_json,
                "content_type": ct[:60],
                "preview": body[:300] if is_json else "",
            }
            captured.append(entry)
            if out is not None and is_json and (not kw or kw in r_url.lower()):
                out.mkdir(parents=True, exist_ok=True)
                path = out / _slug(r_url, len(saved) + 1)
                try:
                    path.write_text(json.dumps(json.loads(body), indent=1), encoding="utf-8")
                except json.JSONDecodeError:
                    path.write_text(body, encoding="utf-8")
                saved.append(path)

        page.on("response", log_response)

        print(f"Loading {url} ...")
        await page.goto(url, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(wait_ms)

        if click:
            try:
                links = await page.locator("a, button").filter(has_text=click).all()
                if links:
                    print(f"\nFound {len(links)} '{click}' element(s), clicking first...")
                    await links[0].click()
                    await page.wait_for_timeout(wait_ms)
            except Exception as e:
                print(f"Click on '{click}' failed: {e}")

        print(f"\nCaptured {len(captured)} relevant responses:\n")
        for r in sorted(captured, key=lambda x: -x.get("size", 0)):
            print(f"  [{r['status']}] {r.get('size', '?'):>8}B  json={r.get('is_json', '?'):<5}  {r.get('content_type', '')}")
            print(f"           {r['url'][:150]}")
            if r.get("preview"):
                print(f"           preview: {r['preview'][:200]}")
            print()

        if out is not None:
            shot = out / "recon.png"
            await page.screenshot(path=str(shot))
            print(f"Saved {len(saved)} JSON bodies + screenshot to {out}")

        await browser.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Playwright response logger for sportsbook API recon")
    parser.add_argument("--url", required=True, help="Page to load")
    parser.add_argument("--keyword", default=None, help="Only save responses whose URL contains this (e.g. 'offering')")
    parser.add_argument("--out", default=None, help="Directory for saved JSON bodies (omit to only print)")
    parser.add_argument("--wait", type=int, default=5000, help="ms to wait after load/click (default 5000)")
    parser.add_argument("--click", default=None, help="Text of a link/button to click after load (e.g. 'Totals')")
    parser.add_argument("--headed", action="store_true", help="Visible browser")
    args = parser.parse_args()
    out = Path(args.out) if args.out else None
    asyncio.run(main(args.url, args.keyword, out, args.wait, args.headed, args.click))


if __name__ == "__main__":
    cli()
