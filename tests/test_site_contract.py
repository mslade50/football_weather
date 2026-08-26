"""Static-site contract: index.html references resolve, and every GameCard field the JS
reads exists in the ARCHITECTURE §5 GameCard spec (so json_out.py and the frontend agree)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "site" / "web"
ARCH = ROOT / "docs" / "ARCHITECTURE.md"

JS_FILES = ["app.js", "status.js", "table.js", "map.js", "drawer.js", "signals.js", "backtest.js"]

# variable names that hold GameCard (sub)objects in the JS, mapped to the spec section
CARD_VARS = ("g", "wx", "st", "c", "s", "t", "o", "v1", "v2", "e", "be", "es", "et", "f", "p", "sig")

# JS/DOM identifiers that legitimately follow those variables but are not GameCard keys
NOT_CARD_KEYS = {
    "length", "map", "filter", "find", "some", "every", "sort", "slice", "join", "push", "flatMap",
    "includes", "startsWith", "toFixed", "toLowerCase", "getTime", "target", "value", "checked",
    "dataset", "textContent", "innerHTML", "style", "hidden", "classList", "key", "error",
    "message", "closest", "addEventListener", "stopPropagation", "remove", "getElement",
    "querySelector", "best_total", "best_spread", "edges",
    # drawer.js normalized chart points / uPlot handles
    "ts", "destroy",
}

# keys json_out._stadium_block emits beyond the ARCH §5 GameCard block (provenance / display only)
STADIUM_EXTRA_KEYS = {"surface", "orient_src", "city", "state", "country", "timezone", "needs_review"}


def _spec_keys() -> set[str]:
    text = ARCH.read_text(encoding="utf-8")
    m = re.search(r"\*\*GameCard\*\*:\s*```(.*?)```", text, re.S)
    assert m, "GameCard spec block not found in ARCHITECTURE.md"
    keys = set(re.findall(r"\b[a-z][a-z0-9_]*\b", m.group(1)))
    # Edge fields live in §4.2; the GameCard block only says `edges [Edge...]`
    em = re.search(r"Edge\((.*?)\)", text, re.S)
    assert em
    keys |= set(re.findall(r"\b[a-z][a-z0-9_]*\b", em.group(1)))
    # top-level ids referenced by the cards but declared in §4.1
    keys |= {"home_id", "away_id", "roof_state"}
    keys |= STADIUM_EXTRA_KEYS
    return keys


def test_index_references_exist() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert refs, "no src/href references found"
    for ref in refs:
        if ref.startswith(("http:", "https:", "#")):
            continue
        assert (WEB / ref).is_file(), f"index.html references missing file: {ref}"
    for js in JS_FILES:
        assert f'src="{js}"' in html, f"index.html must load {js}"
    for vendor in ("vendor/maplibre-gl.js", "vendor/maplibre-gl.css", "vendor/uPlot.iife.min.js", "vendor/uPlot.min.css"):
        assert vendor in html


def test_index_has_required_dom_ids() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for el_id in ("event", "updated", "nextrun", "bookchips", "banners", "statusbar", "table", "map",
                  "maplegend", "drawer", "drawer-body", "drawer-title", "hovercard", "sport", "week",
                  "signal", "book", "minedge", "search", "refreshbtn", "lightrefreshbtn"):
        assert f'id="{el_id}"' in html, f"missing #{el_id}"


def test_app_js_documents_shape_and_endpoints() -> None:
    src = (WEB / "app.js").read_text(encoding="utf-8")
    assert "EXPECTED JSON SHAPES" in src
    for endpoint in ("data/meta.json", "data/games_nfl.json", "data/games_cfb.json", "auth/me"):
        assert endpoint in src, f"app.js must fetch {endpoint}"
    drawer = (WEB / "drawer.js").read_text(encoding="utf-8")
    assert "api/history" in drawer
    assert "data/history.json" in drawer


@pytest.mark.parametrize("js", JS_FILES)
def test_gamecard_keys_used_in_js_exist_in_spec(js: str) -> None:
    spec = _spec_keys()
    src = (WEB / js).read_text(encoding="utf-8")
    alts = "|".join(CARD_VARS)
    pattern = re.compile(rf"\b(?:{alts})\.([a-z][a-z0-9_]*)\b")
    used = set(pattern.findall(src)) - NOT_CARD_KEYS
    unknown = sorted(k for k in used if k not in spec)
    assert not unknown, f"{js} reads keys not in the GameCard spec: {unknown}"


def test_table_consensus_spread_and_book_spreads_toggle() -> None:
    """Table: consensus SPREAD column (src on hover) + per-book TOTAL columns; per-book SPREAD
    columns sit behind the #bookspreads checkbox (localStorage, try/catch). Map popup and drawer
    header show the consensus spread with its src."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="bookspreads"' in html and "book spreads" in html
    table = (WEB / "table.js").read_text(encoding="utf-8")
    assert "spread_src" in table and 'getElementById("bookspreads")' in table
    assert "localStorage" in table and "try {" in table and "catch" in table
    for fn in ("function setupTableControls", "function consensusSpreadCell", "function consensusTotalCell",
               "function bookSpreadCell", "function bookTotalCell"):
        assert fn in table, fn
    assert "withSpreads" in table and "BOOK_SPREADS" in table
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "setupTableControls()" in app
    for js in ("map.js", "drawer.js"):
        assert "spread_src" in (WEB / js).read_text(encoding="utf-8"), js
    # §5 spec carries the key the JS reads
    assert "spread_src" in _spec_keys()


def test_signals_view_wiring() -> None:
    """Phase 5: Signals tab + presets exist and are reachable from the shell."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'data-view="signals"' in html
    for el_id in ("signalsbar", "presetchip"):
        assert f'id="{el_id}"' in html, f"missing #{el_id}"
    sig = (WEB / "signals.js").read_text(encoding="utf-8")
    for preset in ("CFB Wind", "NFL Wind", "Heat", "Alt+Heat"):
        assert f'"{preset}"' in sig, f"signals.js must define the {preset} preset"
    for fn in ("function activePreset", "function gameFlags", "function hasFlag", "function renderSignals", "function setPreset"):
        assert fn in sig
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "renderSignals()" in app
    assert 'params.set("preset"' in app and 'params.get("preset")' in app


def test_map_draws_phase5_vectors() -> None:
    """Phase 5: wind arrow (wind_dir_deg, length ∝ wind_fg), field axis (orient_deg), hollow domes,
    confidence/static opacity toggle, clustering below zoom 4."""
    src = (WEB / "map.js").read_text(encoding="utf-8")
    assert "wind_dir_deg" in src and "wind_fg" in src and "orient_deg" in src
    assert 'class="arrow"' in src and 'class="axis"' in src
    assert "CLUSTER_ZOOM = 4" in src
    assert 'id="map-static"' in src and 'id="map-vectors"' in src
    assert "wind_vol_static" in src and "conf" in src
    assert "transparent" in src  # hollow dome disc


def test_drawer_phase5_sections() -> None:
    """Phase 5: hourly strip with P10–P90 band, forecast-drift sparkline (/api/wx → wx_history.json),
    stadium compass card."""
    src = (WEB / "drawer.js").read_text(encoding="utf-8")
    assert "api/wx" in src and "data/wx_history.json" in src
    for fn in ("function renderHourlyChart", "function renderDriftChart", "function compassCard", "function weakDirs"):
        assert fn in src
    assert "p10" in src and "p90" in src and "bands" in src
    for key in ("orient_deg", "elevation_m", "roof_type", "surface", "avg_wind_month", "weakest_wind_effect"):
        assert key in src


def test_backtest_tab_wiring() -> None:
    """Phase 6: Backtest tab (grid + stadium results + matched games), loaded from
    /data/backtest.json, reachable from the shell and rendered by app.js."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'data-view="backtest"' in html and 'id="backtestwrap"' in html
    assert html.index('src="alerts.js"') < html.index('src="backtest.js"') < html.index('src="app.js"')
    bt = (WEB / "backtest.js").read_text(encoding="utf-8")
    assert "data/backtest.json" in bt
    for fn in ("function loadBacktest", "function renderBacktest", "function backtestMatch", "function backtestHover",
               "function bucketMatch", "function gridSectionHtml", "function stadiumSectionHtml", "function gamesSectionHtml"):
        assert fn in bt, fn
    # first-match semantics of pages/cfb_weather.py: NaN Wind Below -> 100, NaN Spread_l -> 0, NaN Temp Above -> 0,
    # NaN Spread_h never matches an NCAAF row; walk in id order
    assert "row.wind_hi ?? 100" in bt and "row.spread_lo ?? 0" in bt and "row.temp_lo ?? 0" in bt
    assert 'sportLabel !== "NFL"' in bt and "sort((x, y) => x.id - y.id)" in bt
    assert "clvStatus ? row.clv !== clvStatus : row.clv !== null" in bt   # CLV row must equal the game's status
    for legacy_col in ("Wind Above", "Wind Below", "Temp Above", "Temp Below", "Spread_l", "Spread_h", "CLV from Open", "Signal", "Percentage"):
        assert legacy_col in bt, legacy_col
    # pipeline.backtest payload spellings: meta.run_id, GameRow fields, alerts_clv list-shaped by_model
    for key in ("alerts_clv", "by_model", "pos_frac", "wind_fc", "temp_act", "under_result", "actual_total", "stadium_name", "home_name"):
        assert key in bt, key
    assert "function normalizeClv" in bt
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "renderBacktest()" in app and 'view === "backtest"' in app and "loadBacktest" in app
    assert "data/backtest.json" in app   # documented in EXPECTED JSON SHAPES


def test_backtest_hover_record_roi_lookup_wired() -> None:
    """Hover Record / ROI by first-match bucket on the map popup, the table total cell and the drawer."""
    for js in ("map.js", "table.js", "drawer.js"):
        src = (WEB / js).read_text(encoding="utf-8")
        assert "backtestHover(g)" in src, js
    drawer = (WEB / "drawer.js").read_text(encoding="utf-8")
    assert "function backtestRows" in drawer and "stadiumResultFor" in drawer
    bt = (WEB / "backtest.js").read_text(encoding="utf-8")
    assert '"Record (under)"' in bt and '"ROI"' in bt and "function stadiumResultFor" in bt


def test_alerts_tab_clv_columns_and_drawer_clv_timeline() -> None:
    alerts = (WEB / "alerts.js").read_text(encoding="utf-8")
    assert "<th title=\"closing line" in alerts and "CLV</th>" in alerts and "Close</th>" in alerts
    assert "function clvSummary" in alerts and 'id="al-sort"' in alerts and '"clv"' in alerts
    assert "closing_line" in alerts and "clv_pts" in alerts
    drawer = (WEB / "drawer.js").read_text(encoding="utf-8")
    assert "function clvTimelineHtml" in drawer and "CLV timeline" in drawer
    assert "closing_line" in drawer
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    assert ".clv-tl" in css and ".clv-row" in css and "table.bt" in css


def test_vendor_files_are_real_libraries() -> None:
    ml = WEB / "vendor" / "maplibre-gl.js"
    up = WEB / "vendor" / "uPlot.iife.min.js"
    if not (ml.is_file() and up.is_file()):
        assert (WEB / "vendor" / "VENDOR.md").is_file()
        pytest.skip("vendor libraries not downloaded; VENDOR.md lists the URLs")
    assert ml.stat().st_size > 100_000
    assert "maplibre" in ml.read_text(encoding="utf-8", errors="ignore")[:500].lower()
    assert "uPlot" in up.read_text(encoding="utf-8", errors="ignore")[:200]
