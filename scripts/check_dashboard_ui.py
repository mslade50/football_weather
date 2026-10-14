"""Render synthetic, offline desktop/mobile board fixtures. Never load a live URL.

Requires the already-installed Playwright Chromium. Writes ignored tmp evidence.
Run from repo root: python scripts/check_dashboard_ui.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "site" / "web"
OUT = ROOT / "tmp" / "ui-evidence"
NOW = "2026-10-14T12:00:00Z"


def card(index: int) -> dict:
    game = {
        "game_id": f"nfl:2026:6:away{index}@home{index}", "sport": "nfl", "run_id": "fixture",
        "season": 2026, "week": 6, "status": "scheduled", "kickoff_utc": "2026-10-18T20:25:00Z",
        "home": {"name": ["Fixture Seattle", "Fixture Cleveland", "Fixture Chicago"][index]},
        "away": {"name": ["Fixture New York", "Fixture Denver", "Fixture Dallas"][index]},
        "venue_provenance": {"source_id": "NYC01", "resolved_id": "metlife-stadium"},
        "stadium": {"stadium_id": "metlife-stadium", "name": "Synthetic stadium", "roof_state": "open",
                    "timezone": "America/Los_Angeles", "lat": 47.5952, "lon": -122.3316,
                    "orient_deg": [0, 45, None][index], "weakest_wind_effect": "E/W"},
        "weather": {"temp_fg": 55, "wind_fg": 18, "gust_fg": 27, "rain_fg": .4,
                    "wind_dir_deg": [0, 270, None][index], "wind_dir_fg": ["N", "W", ""][index]},
        "signal": {"label": "Mid Impact", "flags": ["NFL Wind"]},
        "consensus": {"total_now": 46.5, "total_open": 48.5, "spread_now": -3, "spread_open": -2.5,
                      "spread_src": "pinnacle", "total_n_books": 4, "spread_n_books": 3},
        "odds": {}, "impact": {"v1": {"gs_fg_pct": -8, "away_fg_pct": -4}},
        "total_prices": {"quotes": []},
    }
    for book in ["pinnacle", "fanduel", "betonline", "kalshi", "novig"]:
        game["odds"][book] = {"total": {"line": 46.5, "open_line": 48.5, "under": -110,
            "over": -105, "open_under": -108, "open_kind": "first_seen", "open_ts": "2026-10-10T14:00:00Z",
            "updated_at": "2026-10-14T11:59:55Z", "expires_at": "2026-10-14T12:00:10Z"}}
    if index == 0:
        game["execution_preview"] = {"ok": True, "game_id": game["game_id"], "board_run_id": "fixture",
            "publication_generation": "fixture-gen", "stake_mode": "principal", "settlement_verified": True,
            "rules_key": "fixture-rules", "side": "under", "line": 46.5, "principal": 500, "fees": 5,
            "average_price": .505, "payout_if_win": 1000, "fetched_at": "2026-10-14T11:59:55Z",
            "depth_fetched_at": "2026-10-14T11:59:53Z", "expires_at": "2026-10-14T12:00:10Z",
            "allocations": [{"book": "kalshi", "side": "under", "line": 46.5, "rules_key": "fixture-rules",
                             "principal": 500, "fees": 5}]}
    elif index == 1:
        game["expired_markets"] = ["kalshi/total", "novig/total"]
    return game


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    source = WEB.joinpath("index.html").read_text(encoding="utf-8")
    source = re.sub(r"<script\b[^>]*>.*?</script>", "", source, flags=re.S)
    source = re.sub(r"<link\b[^>]*>", "", source)
    source = source.replace("<head>", '<head><meta http-equiv="Content-Security-Policy" '
                            'content="connect-src \'none\'; img-src data:;">')
    games = [card(index) for index in range(3)]
    history_module = ROOT.joinpath("site/worker/stadium-history.js").read_text(encoding="utf-8")
    history_module = re.sub(r"^import .*?;\s*$", "", history_module, flags=re.M).replace("export ", "")
    for league in ["nfl", "cfb"]:
        data = ROOT.joinpath(f"data/raw/{league}_stadium_signal_history.json").read_text(encoding="utf-8")
        history_module = f"const {league}={data};\n" + history_module
    evidence = {"fixture": "Synthetic only; no live data or orders", "network_requests": [], "viewports": {}}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for label, width, height in [("desktop", 1680, 1050), ("mobile", 390, 844)]:
            context = browser.new_context(viewport={"width": width, "height": height},
                                          is_mobile=label == "mobile", has_touch=label == "mobile")
            page = context.new_page()
            page.route("**/*", lambda route: (evidence["network_requests"].append(route.request.url), route.abort()))
            errors = []
            page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
            page.set_content(source)
            page.add_style_tag(content=WEB.joinpath("styles.css").read_text(encoding="utf-8"))
            for filename in ["discovery.js", "app.js", "table.js", "signals.js", "drawer.js", "backtest.js"]:
                code = WEB.joinpath(filename).read_text(encoding="utf-8")
                code = re.sub(r"\nboot\(\);\s*$", "", code)
                page.add_script_tag(content=code)
            page.evaluate("stamp => { Date.now = () => Date.parse(stamp); }", NOW)
            page.evaluate("""games => {
                IS_ADMIN=true; STATE.sport='nfl'; DATA.games.nfl=games; RAW_GAMES.nfl=games;
                RAW_META={run_id:'fixture',publication_status:'manifest_verified',publication:{generation:'fixture-gen'}};
                BOOKS=['pinnacle','fanduel','betonline','kalshi','novig'];
                BT.loaded=true; BT.data=normalizeBacktest({games:[]});
                document.querySelector('header').innerHTML='<h1>Football Weather · synthetic offline fixture</h1>';
                document.getElementById('statusbar').textContent='No network · verified, stale and unknown states';
                document.getElementById('book').innerHTML='<option value="">All books</option>';
                document.getElementById('tablewrap').style.display='';
                renderHourlyChart=()=>{}; renderDriftChart=()=>{}; loadHistory=()=>{}; renderDrawerAlerts=()=>{};
                writeHash=()=>{}; renderTable(games); setupHover(); setupDrawer();
            }""", games)
            page.add_script_tag(content=history_module)
            page.evaluate("STADIUM_HISTORY.data=stadiumHistoryPayload(); renderTable(DATA.games.nfl)")
            page.screenshot(path=str(OUT / f"{label}.png"), full_page=True)
            assert page.locator(".compact-compass svg").count() == 3
            assert "Exchange quotes stale" in page.locator("#table").inner_text()
            assert "No exchange quote" in page.locator("#table").inner_text()
            metrics = page.evaluate("""() => ({
                bodyWidth:document.body.scrollWidth, viewport:innerWidth,
                bookWidths:[...document.querySelectorAll('tr:first-child .compact-total')].map(el=>el.getBoundingClientRect().width),
                kickoff:document.querySelector('.game').innerText
            })""")
            if label == "mobile":
                assert metrics["bodyWidth"] <= width, metrics
            getattr(page.locator(".stadium-detail").first, "tap" if label == "mobile" else "click")()
            assert page.locator("#drawer").is_visible()
            assert "Source" in page.locator("#drawer-body").inner_text()
            assert "Updated:" in page.locator("#drawer-odds").inner_text()
            page.screenshot(path=str(OUT / f"{label}-details.png"), full_page=True)
            page.keyboard.press("Escape")
            assert page.locator("#drawer").is_hidden()
            assert page.locator(".stadium-detail").first.evaluate("el => el === document.activeElement")
            # Exercise the table shortcut locally; no request is sent until explicit check click.
            getattr(page.locator(".exchange-detail").first, "tap" if label == "mobile" else "click")()
            assert page.locator("#verify-exchange-stake").evaluate("el => el === document.activeElement")
            page.keyboard.press("Escape")
            page.evaluate("""() => {
                STATE.sport='cfb'; DATA.games.cfb=DATA.games.nfl.map((game,i)=>({...game,sport:'cfb',
                    game_id:game.game_id.replace('nfl:','cfb:'), execution_preview:null,
                    stadium:{...game.stadium,stadium_id:i===0?'camp-randall-stadium':'dowdy-ficklen-stadium'},
                    venue_provenance:{source_id:i===0?'347':'3699',resolved_id:i===0?'camp-randall-stadium':'dowdy-ficklen-stadium'}}));
                renderTable(DATA.games.cfb);
                document.getElementById('sport').value='cfb';
            }""")
            assert "synthetic totals" in page.locator("#table").inner_text()
            assert "Signal history unavailable" in page.locator("#table").inner_text()
            if label == "mobile":
                assert page.evaluate("document.body.scrollWidth") <= width
                assert page.locator("#hovercard").is_hidden()
            page.screenshot(path=str(OUT / f"{label}-cfb.png"), full_page=True)
            getattr(page.locator(".stadium-detail").first, "tap" if label == "mobile" else "click")()
            assert "quarter-point synthetic" in page.locator("#drawer-stadium-history").inner_text()
            assert not errors, errors
            evidence["viewports"][label] = {**metrics, "errors": errors, "detail_and_keyboard_checks": "passed"}
            context.close()
        browser.close()
    assert not evidence["network_requests"], evidence["network_requests"]
    OUT.joinpath("validation.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
