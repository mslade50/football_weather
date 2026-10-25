"""Team-total model (pipeline/model/team_totals.py), its stats feed (pipeline/teamstats.py), the
v2 fair blend and the build stage. Offline: parsers run on scrubbed fixtures, fetches are faked."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import state as pstate
from pipeline import teamstats as TS  # noqa: N812
from pipeline.model import fair as F  # noqa: N812
from pipeline.model import team_totals as TT  # noqa: N812

FIX = Path(__file__).resolve().parent / "fixtures" / "raw" / "teamstats"
UTC = timezone.utc


def tg(season: int, week: int, team: str, opp: str, pf: float, pa: float, eff: float = 0.0, d_eff: float = 0.0,
       plays: float = 60.0, sport: str = "nfl", **kw) -> TT.TeamGame:
    return TT.TeamGame(sport, season, week, f"{season}-{week}-{min(team, opp)}", team, opp, eff=eff, pf=pf,
                       d_eff=d_eff, pa=pa, plays=plays, d_plays=plays, **kw)


def pair(season: int, week: int, a: str, b: str, pa_: float, pb: float, sport: str = "nfl", **kw) -> list[TT.TeamGame]:
    return [tg(season, week, a, b, pa_, pb, sport=sport, **kw), tg(season, week, b, a, pb, pa_, sport=sport, **kw)]


def league(season: int, weeks: int = 4, sport: str = "nfl") -> list[TT.TeamGame]:
    """Four teams; 'hi' scores a lot, 'lo' scores little, two average teams."""
    rows: list[TT.TeamGame] = []
    for w in range(1, weeks + 1):
        rows += pair(season, w, "hi", "a1", 34, 24, sport=sport)
        rows += pair(season, w, "lo", "a2", 13, 20, sport=sport)
    return rows


# ---------------------------------------------------------------- ratings


def test_derived_metrics():
    g = TT.TeamGame("cfb", 2025, 1, "k", "x", "y", pf=28, drives=14, pa=14, d_drives=12, plays=70, d_plays=65)
    assert g.value("ppd") == 2.0 and g.value("d_ppd") == pytest.approx(14 / 12) and g.value("pace") == 135
    assert TT.TeamGame("nfl", 2025, 1, "k", "x", "y").value("ppd") is None


def test_ratings_use_only_games_before_the_week():
    rows = league(2024) + league(2025, weeks=6)
    base = TT.build_ratings("nfl", rows, 2025, 4)
    changed = [r if not (r.season == 2025 and r.week >= 4) else
               TT.TeamGame(**{**r.__dict__, "pf": r.pf + 30}) for r in rows]
    assert TT.build_ratings("nfl", changed, 2025, 4)["hi"].values == base["hi"].values   # week 4+ unseen
    assert TT.build_ratings("nfl", changed, 2025, 5)["hi"].values["pf"] > base["hi"].values["pf"]
    assert base["hi"].n_games == 3 and base["hi"].values["pf"] > 0 > base["lo"].values["pf"]


def test_preseason_ratings_are_last_season_shrunk():
    rows = league(2024)
    pre = TT.build_ratings("nfl", rows, 2025, 1)
    k, a, _ = TT.DEFAULT_PARAMS["nfl"]["pf"]
    lg = sum(r.pf for r in rows) / len(rows)
    assert pre["hi"].n_games == 0
    assert pre["hi"].values["pf"] == pytest.approx(a * (34 - lg))


def test_walk_matches_build_ratings():
    rows = league(2024) + league(2025, weeks=3)
    snaps = {(s, w): snap for s, w, snap in TT.walk_ratings("nfl", rows)}
    assert snaps[(2025, 3)]["hi"].values == TT.build_ratings("nfl", rows, 2025, 3)["hi"].values


def test_cfb_opponent_adjustment_credits_scoring_against_good_defences():
    """Same points scored; the team that did it against the stingy defence rates higher."""
    rows: list[TT.TeamGame] = []
    for w in range(1, 5):   # set up a stingy and a leaky defence
        rows += pair(2025, w, "stingy", "x1", 10, 10, sport="cfb")
        rows += pair(2025, w, "leaky", "x2", 45, 45, sport="cfb")
    rows += [tg(2025, 5, "p", "stingy", 30, 10, sport="cfb"), tg(2025, 5, "stingy", "p", 10, 30, sport="cfb"),
             tg(2025, 5, "q", "leaky", 30, 45, sport="cfb"), tg(2025, 5, "leaky", "q", 45, 30, sport="cfb")]
    r = TT.build_ratings("cfb", rows, 2025, 6)
    assert r["p"].values["pf"] > r["q"].values["pf"]


# ---------------------------------------------------------------- points model + blend


def test_features_symmetric_and_predict():
    h = TT.Rating("h", 3, {m: 0.1 * i for i, m in enumerate(TT.METRICS)})
    a = TT.Rating("a", 3, {m: -0.05 * i for i, m in enumerate(TT.METRICS)})
    fa, fb = TT.features(h, a, 44.0), TT.features(a, h, 44.0)
    assert fa.keys() == fb.keys() and all(fa[k] == pytest.approx(fb[k]) for k in fa)
    coef = {"intercept": 1.0, "lg": 1.0, "pts_match": 0.5}
    f = TT.features(h, a, 44.0)
    assert TT.predict_total(coef, f) == pytest.approx(1.0 + 44.0 + 0.5 * f["pts_match"])


def test_blend_weight_by_lead():
    close, far = TT.BLEND_WEIGHTS["cfb"]
    assert TT.blend_weight("cfb", 0) == close and TT.blend_weight("cfb", 200) == far
    assert TT.blend_weight("cfb", None) == close
    assert close < TT.blend_weight("cfb", 39.0) < far
    assert TT.blend_weight("nfl", 100) == TT.BLEND_WEIGHTS["nfl"][1] and TT.blend_weight("xfl", 10) == 0.0


def test_fair_blend_moves_only_the_total():
    assert F.blend_team_total(45.0, 47.0, 52.0, 0.4) == pytest.approx(47.0)
    assert F.blend_team_total(45.0, 47.0, None, 0.4) == 45.0
    assert F.blend_team_total(45.0, 47.0, 52.0, 0.0) == 45.0
    assert F.blend_team_total(None, 47.0, 52.0, 0.4) is None


def test_shipped_model_has_both_sports():
    model = TT.load_model()
    for sport in ("nfl", "cfb"):
        assert model[sport]["coef"]["intercept"] is not None and "lg" in model[sport]["coef"]
    assert TT.load_model(Path("does-not-exist.json")) == {}


# ---------------------------------------------------------------- parsers (scrubbed fixtures)


def test_parse_nfl_fixture():
    rows = TS.parse_nfl((FIX / "nfl_stats_team_week_2025.csv").read_text(), (FIX / "nfl_games_2025.csv").read_text(), 2025)
    by = {r.team: r for r in rows}
    assert set(by) == {"ari", "no", "bal", "buf"}
    ari = by["ari"]
    assert (ari.pf, ari.pa, ari.opp, ari.week) == (20, 13, "no", 1)
    assert ari.plays == 29 + 5 + 27 and ari.eff == pytest.approx((1.52479932586014 + 1.15853980279507) / 61)
    assert ari.d_eff == by["no"].eff and ari.drives is None and ari.value("ppd") is None
    assert TS.parse_nfl((FIX / "nfl_stats_team_week_2025.csv").read_text(), (FIX / "nfl_games_2025.csv").read_text(), 2024) == []


def test_parse_cfb_fixture_keeps_fcs_opponents_and_flags_fbs():
    def resolve(n: str) -> str:
        return TS.resolve_team("cfb", n)

    rows, fbs = TS.parse_cfb(json.loads((FIX / "cfb_advanced_2025.json").read_text()),
                             json.loads((FIX / "cfb_games_2025.json").read_text()), 2025, resolve)
    by = {r.team: r for r in rows}
    assert {"unlv", "idaho-state", "kansas-state", "iowa-state"} <= set(by)
    assert fbs == {"unlv", "kansas-state", "iowa-state"}
    assert (by["unlv"].pf, by["unlv"].pa, by["unlv"].opp) == (38, 31, "idaho-state")
    assert by["kansas-state"].drives and by["kansas-state"].sr is not None
    ret = TS.parse_returning(json.loads((FIX / "cfb_returning_2025.json").read_text()), resolve)
    assert ret and all(0.0 <= v <= 1.0 for v in ret.values())


# ---------------------------------------------------------------- refresh / cache


NOW = datetime(2026, 10, 24, 18, tzinfo=UTC)


def test_needs_refresh_rules():
    old = (NOW - timedelta(hours=60)).isoformat()
    recent = (NOW - timedelta(hours=10)).isoformat()
    assert TS.needs_refresh(None, NOW, final=False, min_hours=48)
    assert not TS.needs_refresh({"fetched_at": old}, NOW, final=True, min_hours=48)        # past season: once
    assert TS.needs_refresh({"fetched_at": old}, NOW, final=False, min_hours=48)           # cadence elapsed
    assert not TS.needs_refresh({"fetched_at": recent}, NOW, final=False, min_hours=48)    # too soon
    assert TS.needs_refresh({"fetched_at": recent}, NOW, final=False, min_hours=6)
    just_failed = (NOW - timedelta(hours=1)).isoformat()
    assert not TS.needs_refresh({"fetched_at": old, "failed_at": just_failed}, NOW, final=False, min_hours=48)


def test_fixed_cadence_picks_up_late_stats_and_skips_off_season(monkeypatch):
    """A feed that publishes a finished game late is picked up by the next cadence fetch."""
    calls: list[int] = []
    seasons = {str(s): {"fetched_at": NOW.isoformat(), "rows": []} for s in (2024, 2025)}
    cache = {"sports": {"nfl": {"seasons": seasons}}}

    def fetch(sport, season, **k):
        calls.append(season)
        return {"rows": []}
    monkeypatch.setattr(TS, "fetch_season", fetch)
    kicks = [NOW + timedelta(days=3)]
    assert TS.refresh("nfl", 2026, cache, kicks, NOW)                                     # first fetch
    assert not TS.refresh("nfl", 2026, cache, kicks, NOW + timedelta(hours=6))            # inside 12 h
    assert TS.refresh("nfl", 2026, cache, kicks, NOW + timedelta(hours=12))               # cadence
    assert not TS.refresh("nfl", 2026, cache, [], NOW + timedelta(days=30))               # off-season
    assert calls == [2026, 2026]


def _fake_cfbd(calls: list[str]):
    adv = (FIX / "cfb_advanced_2025.json").read_text()
    games = (FIX / "cfb_games_2025.json").read_text()
    ret = (FIX / "cfb_returning_2025.json").read_text()

    def get(url: str, headers=None) -> str:
        calls.append(url)
        if "/stats/game/advanced" in url:
            return adv
        if "/games" in url:
            return games
        return ret
    return get


def test_refresh_fetches_history_once_and_returning_once(monkeypatch):
    monkeypatch.setenv("CFBD_API_KEY", "k")
    calls: list[str] = []
    cache: dict = {"sports": {}}
    kicks = [NOW + timedelta(days=1)]
    assert TS.refresh("cfb", 2025, cache, kicks, NOW, get=_fake_cfbd(calls))
    assert len(calls) == 3 * 3                     # 3 seasons x (advanced, games, returning)
    seasons = cache["sports"]["cfb"]["seasons"]
    assert set(seasons) == {"2023", "2024", "2025"}
    calls.clear()
    assert not TS.refresh("cfb", 2025, cache, kicks, NOW + timedelta(hours=47), get=_fake_cfbd(calls))
    assert calls == []                             # CFB cadence is 48 h
    later = NOW + timedelta(days=3)
    assert TS.refresh("cfb", 2025, cache, [later + timedelta(days=4)], later, get=_fake_cfbd(calls))
    assert len(calls) == 2 and not any("returning" in c for c in calls)   # current season only, no returning
    assert "unlv" in cache["sports"]["cfb"]["members"]


def test_refresh_failure_keeps_rows_and_backs_off(monkeypatch):
    monkeypatch.setenv("CFBD_API_KEY", "k")
    cache: dict = {"sports": {}}
    TS.refresh("cfb", 2025, cache, [], NOW, get=_fake_cfbd([]))
    entry = cache["sports"]["cfb"]["seasons"]["2025"]
    entry["fetched_at"] = (NOW - timedelta(days=6)).isoformat()
    rows_before = list(entry["rows"])
    errs: list[str] = []
    calls: list[str] = []

    def boom(url, headers=None):
        calls.append(url)
        raise RuntimeError("502")
    assert TS.refresh("cfb", 2025, cache, [NOW], NOW, get=boom, on_error=errs.append)   # failure is recorded
    entry = cache["sports"]["cfb"]["seasons"]["2025"]
    assert entry["rows"] == rows_before and entry["failed_at"] == NOW.isoformat() and "502" in errs[0]
    n = len(calls)
    for i in range(3):   # later builds inside RETRY_H make no calls at all
        TS.refresh("cfb", 2025, cache, [NOW], NOW + timedelta(minutes=10 * (i + 1)), get=boom)
    assert len(calls) == n
    TS.refresh("cfb", 2025, cache, [NOW], NOW + timedelta(hours=TS.RETRY_H + 1), get=_fake_cfbd(calls))
    assert len(calls) > n and "failed_at" not in cache["sports"]["cfb"]["seasons"]["2025"]


def test_returning_failure_is_not_fatal(monkeypatch):
    monkeypatch.setenv("CFBD_API_KEY", "k")
    good = _fake_cfbd([])

    def get(url, headers=None):
        if "/player/returning" in url:
            raise RuntimeError("timeout")
        return good(url, headers)
    entry = TS.fetch_season("cfb", 2025, get=get)
    assert entry["rows"] and entry["returning"] == {}


def test_season_without_predecessor_is_never_rated():
    """A gap in history (2023 and 2025, no 2024) must not let 2025's own results into 2025 features."""
    rows = pair(2023, 1, "a", "b", 20, 20) + pair(2025, 1, "a", "b", 20, 20) + pair(2025, 10, "a", "b", 20, 20)
    assert TT.league_total(rows, 2025) is None and not TT.has_prior(rows, 2025)
    assert TT.league_total(rows, 2024) == 40.0
    games = [G("g", 2025, 11, "a", "b", NOW)]
    cache = {"sports": {"nfl": {"seasons": {"2023": {"rows": TS._rows_to_json(rows[:2])},
                                            "2025": {"rows": TS._rows_to_json(rows[2:])}}}}}
    assert TS.team_totals_for_games("nfl", games, cache, {"nfl": {"coef": {"lg": 1.0}}}, NOW) == {}
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import fit_team_model as FIT

    assert [t for t in FIT.game_table("nfl", rows, {}, None) if t["season"] == 2025] == []


def test_state_file_round_trip(tmp_path: Path):
    fresh = pstate.load_team_stats(tmp_path)
    assert fresh["sports"] == {} and fresh["schema_version"] == 1
    fresh["sports"]["nfl"] = {"seasons": {}}
    pstate.save_team_stats(tmp_path, fresh)
    assert pstate.load_team_stats(tmp_path)["sports"] == {"nfl": {"seasons": {}}}


def test_team_stats_synced_with_r2():
    from pipeline.outputs import r2

    assert "team_stats" in r2.STATE_FILES
    wf = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pipeline.yml").read_text()
    line = next(ln for ln in wf.splitlines() if ln.strip().startswith("STATE_FILES:"))
    assert "team_stats" in line.split()


# ---------------------------------------------------------------- per-game totals + build stage


@dataclass
class G:
    game_id: str
    season: int
    week: int
    home_id: str
    away_id: str
    kickoff_utc: datetime


def _cache_with(rows: list[TT.TeamGame], sport: str, members: list[str] | None = None) -> dict:
    return {"sports": {sport: {"seasons": {"2025": {"rows": TS._rows_to_json(rows)}}, "members": members or []}}}


def test_team_totals_for_games_and_skips():
    rows = league(2024) + league(2025)
    model = {"nfl": {"coef": {"intercept": 0.0, "lg": 1.0, "pts_match": 1.0}}}
    cache = {"sports": {"nfl": {"seasons": {"2024": {"rows": TS._rows_to_json(league(2024))},
                                            "2025": {"rows": TS._rows_to_json(league(2025))}}}}}
    games = [G("g1", 2025, 5, "hi", "lo", NOW + timedelta(days=4)), G("g2", 2025, 5, "hi", "nobody", NOW)]
    out = TS.team_totals_for_games("nfl", games, cache, model, NOW)
    assert set(out) == {"g1"}
    lg = TT.league_total(rows, 2025)
    r = TT.build_ratings("nfl", rows, 2025, 5)
    assert out["g1"]["total"] == round(TT.predict_total(model["nfl"]["coef"], TT.features(r["hi"], r["lo"], lg)), 1)
    assert out["g1"]["weight"] == TT.blend_weight("nfl", 96.0) and out["g1"]["n_games"] == 4
    assert TS.team_totals_for_games("nfl", games, cache, {}, NOW) == {}
    cfb_cache = _cache_with([TT.TeamGame(**{**x.__dict__, "sport": "cfb"}) for x in league(2025)], "cfb", members=["hi"])
    assert TS.team_totals_for_games("cfb", games, cfb_cache, {"cfb": model["nfl"]}, NOW) == {}   # 'lo' is not FBS


class _Ctx:
    def __init__(self) -> None:
        self.now_utc = NOW
        self.started_et = NOW
        self.dry_run = False
        self.degradations: list[tuple] = []
        self.counts: dict = {}

    def degrade(self, *a) -> None:
        self.degradations.append(a)

    def count(self, k, sport, n) -> None:
        self.counts[(k, sport)] = n


def test_build_stage_switch_and_offline_cache(tmp_path: Path, monkeypatch):
    from pipeline import build as B  # noqa: N812
    from pipeline.outputs.raw_out import NullRawStore

    games = [G("g1", 2025, 5, "hi", "lo", NOW + timedelta(days=4))]
    pstate.save_team_stats(tmp_path, {"sports": {"nfl": {"seasons": {
        "2024": {"rows": TS._rows_to_json(league(2024)), "fetched_at": NOW.isoformat()},
        "2025": {"rows": TS._rows_to_json(league(2025)), "fetched_at": NOW.isoformat()},
        "2023": {"rows": [], "fetched_at": NOW.isoformat()}}}}})
    monkeypatch.setenv("TEAM_TOTALS", "0")
    assert B.stage_team_totals(_Ctx(), "nfl", games, [], NullRawStore(), 2025, tmp_path) == {}
    monkeypatch.setenv("TEAM_TOTALS", "1")

    def no_fetch(*a, **k):
        raise AssertionError("must not fetch: the cache is fresh")
    monkeypatch.setattr(TS, "fetch_season", no_fetch)
    ctx = _Ctx()
    out = B.stage_team_totals(ctx, "nfl", games, [], NullRawStore(), 2025, tmp_path)
    assert set(out) == {"g1"} and ctx.counts[("team_totals", "nfl")] == 1 and not ctx.degradations


def test_evaluate_game_blends_team_total_into_fair_total_only():
    from pipeline.contracts import GameLine

    gid = "cfb:2026:1:tennessee@oklahoma"

    def two(book: str, market: str, line: float) -> list[GameLine]:
        sides = ("over", "under") if market == "total" else ("home", "away")
        return [GameLine(sport="cfb", game_id=gid, book=book, market=market, side=sides[0], odds=-110, line=line),
                GameLine(sport="cfb", game_id=gid, book=book, market=market, side=sides[1], odds=-110,
                         line=line if market == "total" else -line)]

    lines = two("pinnacle", "total", 56.0) + two("fanduel", "total", 56.0) + two("pinnacle", "spread", -7.0)
    base = F.evaluate_game("cfb", gid, lines, -5.0, 0.0, lead_hours=72.0)
    blended = F.evaluate_game("cfb", gid, lines, -5.0, 0.0, lead_hours=72.0, team_total=62.0, team_weight=0.35)
    assert blended.fair_total == pytest.approx(base.fair_total + 0.35 * (62.0 - 56.0))
    assert blended.fair_spread == base.fair_spread
    assert F.evaluate_game("cfb", gid, lines, -5.0, 0.0, team_total=62.0, team_weight=0.0).fair_total == base.fair_total


def test_card_and_drawer_carry_team_total():
    import inspect

    from pipeline.outputs import json_out

    assert "team_total" in inspect.signature(json_out.build_card).parameters
    assert '"team_total": dict(team_total) if team_total else None' in inspect.getsource(json_out.build_card)
    drawer = (Path(__file__).resolve().parents[1] / "site" / "web" / "drawer.js").read_text(encoding="utf-8")
    assert "g.team_total" in drawer and "Our total" in drawer


def test_relative_to_slate_removes_level_and_needs_a_slate():
    totals = {f"g{i}": {"total": 50.0 + i + 3.0} for i in range(10)}          # ours runs 3 pts high everywhere
    lines = {f"g{i}": 50.0 + i for i in range(10)}
    rel = TS.relative_to_slate(totals, lines)
    assert all(v["slate_offset"] == 3.0 and v["total_rel"] == lines[g] for g, v in rel.items())
    lines["g0"] -= 4.0                                                          # one game where we're 4 pts more over
    rel = TS.relative_to_slate(totals, lines)
    assert rel["g0"]["total_rel"] - lines["g0"] == pytest.approx(4.0)
    few = TS.relative_to_slate({"a": {"total": 60.0}}, {"a": 50.0})
    assert "total_rel" not in few["a"] and few["a"]["total"] == 60.0
    nolines = TS.relative_to_slate(totals, {k: v for k, v in list(lines.items())[:8]} | {})
    assert "total_rel" not in nolines["g9"] and "total_rel" in nolines["g1"]


def test_evaluate_fair_blends_toward_total_rel_only():
    from types import SimpleNamespace

    from pipeline import build as B  # noqa: N812

    seen: list[dict] = []

    def evaluate_game(*a, **k):
        seen.append(k)
        return "gf"

    fair_mod = SimpleNamespace(evaluate_game=evaluate_game)
    game = SimpleNamespace(game_id="g")
    impact = SimpleNamespace(gs_fg_pct=-2.0, away_fg_pct=0.0, rain_c=0.0)
    ctx = _Ctx()
    B._evaluate_fair(fair_mod, ctx, "cfb", game, ["line"], impact, None, None, "v2",
                     team_total={"total": 60.0, "total_rel": 57.5, "weight": 0.35})
    B._evaluate_fair(fair_mod, ctx, "cfb", game, ["line"], impact, None, None, "v2",
                     team_total={"total": 60.0, "weight": 0.35})                    # no slate offset -> no blend
    assert seen[0]["team_total"] == 57.5 and seen[0]["team_weight"] == 0.35
    assert "team_total" not in seen[1]


def test_expiry_repricing_keeps_the_team_blend():
    """refresh_expired_quotes re-prices v2 after a reference quote expires: the blend must survive."""
    from types import SimpleNamespace

    from pipeline import build as B  # noqa: N812
    from pipeline.contracts import GameLine

    gid = "cfb:2026:9:a@h"
    lines = [GameLine("cfb", gid, book, "total", side, -110, line=56, expires_at=exp)
             for book, exp in (("pinnacle", None), ("fanduel", None), ("betcris", NOW - timedelta(minutes=1)))
             for side in ("over", "under")]
    impact = SimpleNamespace(gs_fg_pct=0.0, away_fg_pct=0.0, rain_c=0.0)
    ctx = SimpleNamespace(now_utc=NOW, counts={}, degrade=lambda *a: None)
    game = SimpleNamespace(game_id=gid)
    tt = {"total": 64.0, "total_rel": 62.0, "weight": 0.35}
    base = B._evaluate_fair(F, ctx, "cfb", game, lines, impact, None, None, "v2", team_total=tt)
    odds = B.OddsResult(lines, {}, {"openers": {}}, {gid: lines}, [], {})
    res = B.SportResult("cfb", [], [], [], [game], {}, {}, {}, {gid: impact}, odds, impacts_v2={gid: impact},
                        fairs_v2={gid: base}, team_totals={gid: tt})
    B.refresh_expired_quotes(ctx, res)
    assert base.fair_total == pytest.approx(56.0 + 0.35 * 6.0)
    assert res.fairs_v2[gid].fair_total == pytest.approx(base.fair_total)


def test_published_card_repricing_keeps_the_team_blend():
    import inspect

    from pipeline import current_quotes as CQ  # noqa: N812

    src = inspect.getsource(CQ._recompute_card)
    assert 'card.get("team_total")' in src and '"team_total": tt["total_rel"]' in src and "**blend" in src


def test_malformed_returning_payload_keeps_rows(monkeypatch):
    monkeypatch.setenv("CFBD_API_KEY", "k")
    good = _fake_cfbd([])

    def get(url, headers=None):
        return "not-json" if "/player/returning" in url else good(url, headers)
    entry = TS.fetch_season("cfb", 2025, get=get)
    assert entry["rows"] and entry["returning"] == {}

    def get_bad_shape(url, headers=None):
        return "[1, 2]" if "/player/returning" in url else good(url, headers)
    assert TS.fetch_season("cfb", 2025, get=get_bad_shape)["rows"]


