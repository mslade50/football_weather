"""pipeline.gate_check: pure decision + parsers + fail-open + output contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import gate_check as G

UTC = timezone.utc
NOW = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)  # Thu in-season

NFLVERSE_CSV = (
    "game_id,season,game_type,week,gameday,weekday,gametime,away_team,home_team\n"
    "2025_22_SEA_NE,2025,SB,22,2026-02-08,Sunday,18:30,SEA,NE\n"
    "2026_01_DAL_PHI,2026,REG,1,2026-09-10,Thursday,20:20,DAL,PHI\n"
    "2026_01_KC_LAC,2026,REG,1,2026-09-13,Sunday,,KC,LAC\n"
    "2026_01_BAD,2026,REG,1,,Sunday,13:00,X,Y\n"
)

CFBD_JSON = [
    {"id": 1, "startDate": "2026-09-12T19:30:00.000Z", "homeTeam": "Ohio State"},
    {"id": 2, "startDate": "2026-09-12T23:00:00+00:00", "homeTeam": "Texas"},
    {"id": 3, "startDate": None},
    {"id": 4, "startDate": "garbage"},
]


# ---- decide -----------------------------------------------------------------------

def test_decide_scrape_inside_horizon():
    assert G.decide([NOW + timedelta(days=3)], NOW) == "scrape"


def test_decide_scrape_within_lookback():
    assert G.decide([NOW - timedelta(hours=2)], NOW) == "scrape"


def test_decide_skip_when_all_past_or_far():
    kicks = [NOW - timedelta(days=1), NOW + timedelta(days=G.HORIZON_DAYS + 1)]
    assert G.decide(kicks, NOW) == "skip"


def test_decide_skip_when_empty():
    assert G.decide([], NOW) == "skip"


def test_decide_naive_kickoffs_treated_as_utc():
    assert G.decide([NOW.replace(tzinfo=None) + timedelta(days=1)], NOW) == "scrape"


# ---- parsers ----------------------------------------------------------------------

def test_parse_nflverse_kickoffs_filters_season_and_converts_et():
    kicks = G.parse_nflverse_kickoffs(NFLVERSE_CSV, [2026])
    assert len(kicks) == 2  # bad row dropped, 2025 dropped
    # 20:20 ET on 2026-09-10 (EDT) == 00:20 UTC next day
    assert kicks[0] == datetime(2026, 9, 11, 0, 20, tzinfo=UTC)
    # missing gametime -> 13:00 ET default
    assert kicks[1] == datetime(2026, 9, 13, 17, 0, tzinfo=UTC)


def test_parse_cfbd_kickoffs():
    kicks = G.parse_cfbd_kickoffs(CFBD_JSON)
    assert kicks == [
        datetime(2026, 9, 12, 19, 30, tzinfo=UTC),
        datetime(2026, 9, 12, 23, 0, tzinfo=UTC),
    ]
    assert G.parse_cfbd_kickoffs({"games": CFBD_JSON}) == kicks
    assert G.parse_cfbd_kickoffs([]) == []


def test_candidate_seasons_span_postseason():
    assert G.candidate_seasons(datetime(2026, 2, 8, 20, 0, tzinfo=UTC)) == [2025, 2026]
    assert G.candidate_seasons(datetime(2026, 9, 1, tzinfo=UTC)) == [2026]


# ---- gate (fetchers monkeypatched) ---------------------------------------------------

def test_gate_skips_off_season(monkeypatch: pytest.MonkeyPatch):
    far = [NOW + timedelta(days=60)]
    monkeypatch.setitem(G.FETCHERS, "nfl", lambda now: far)
    monkeypatch.setitem(G.FETCHERS, "cfb", lambda now: far)
    out = G.gate(["nfl", "cfb"], now=NOW)
    assert out == {"run": "skip", "need_playwright": "false", "run_nfl": "skip", "run_cfb": "skip"}


def test_gate_scrapes_if_any_sport_live(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(G.FETCHERS, "nfl", lambda now: [NOW + timedelta(days=60)])
    monkeypatch.setitem(G.FETCHERS, "cfb", lambda now: [NOW + timedelta(days=2)])
    out = G.gate(["nfl", "cfb"], now=NOW)
    assert out["run"] == "scrape" and out["run_nfl"] == "skip" and out["run_cfb"] == "scrape"


def test_gate_fail_open_on_fetch_error(monkeypatch: pytest.MonkeyPatch):
    def boom(now):
        raise RuntimeError("network down")

    monkeypatch.setitem(G.FETCHERS, "nfl", boom)
    assert G.gate(["nfl"], now=NOW)["run"] == "scrape"


def test_gate_fail_open_when_nothing_parsed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(G.FETCHERS, "cfb", lambda now: [])
    assert G.gate(["cfb"], now=NOW)["run"] == "scrape"


def test_gate_force_bypasses_fetch(monkeypatch: pytest.MonkeyPatch):
    def boom(now):
        raise AssertionError("must not fetch when forced")

    monkeypatch.setitem(G.FETCHERS, "nfl", boom)
    assert G.gate(["nfl"], now=NOW, force=True)["run"] == "scrape"


def test_cfb_fetcher_requires_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CFBD_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        G.kickoffs_cfb(NOW)


def test_need_playwright_false_in_phase1(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(G.FETCHERS, "nfl", lambda now: [NOW])
    assert G.gate(["nfl"], now=NOW)["need_playwright"] == "false"


# ---- CLI output contract -----------------------------------------------------------

def test_main_prints_and_writes_github_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    monkeypatch.setitem(G.FETCHERS, "nfl", lambda now: [datetime.now(UTC) + timedelta(days=1)])
    gh = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh))
    assert G.main(["--sport", "nfl"]) == 0
    stdout = capsys.readouterr().out.splitlines()
    assert "run=scrape" in stdout
    assert "need_playwright=false" in stdout
    assert "run=scrape" in gh.read_text(encoding="utf-8").splitlines()


def test_main_never_raises(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    def boom(*a, **k):
        raise RuntimeError("total failure")

    monkeypatch.setattr(G, "gate", boom)
    assert G.main(["--sport", "all"]) == 0
    assert "run=scrape" in capsys.readouterr().out
