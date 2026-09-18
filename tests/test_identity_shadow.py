"""Identity shadow harness (tools/): corpus replay, candidate pre-filter, judge
resumability, the accept rule, the reports, and the two emit-only hooks.

The TypeSafe SDK is not a repo dependency (CI installs requirements-dev.txt only),
so it is stubbed here exactly the way conftest stubs playwright/rapidfuzz. The
question builders import it lazily, so nothing else in the harness needs it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("typesafe_sdk", MagicMock())

import pytest  # noqa: E402

from pipeline.contracts import Game, make_game_id  # noqa: E402
from pipeline.odds import teams as teams_mod  # noqa: E402
from pipeline.stadiums.loader import load_stadium_book  # noqa: E402
from tools import identity_shadow as shadow  # noqa: E402
from tools import shadow_log  # noqa: E402
from tools.questions import (  # noqa: E402
    NONE_OF_THESE,
    TEAM_CHOICE_KEY,
    VENUE_CHOICE_KEY,
    compat_key,
    team_questions,
    team_state,
    venue_questions,
    venue_state,
)

UTC = timezone.utc

STADIUM_COLUMNS = "stadium_id,name,aliases,city,state,lat,lon,cfbd_venue_id\n"
STADIUM_ROWS = [
    "shi-stadium,SHI Stadium,,Piscataway,NJ,40.513,-74.465,3754x",
    "snapdragon-stadium,Snapdragon Stadium,,San Diego,CA,32.783,-117.119,7100",
    "memorial-stadium-ne,Memorial Stadium,Tom Osborne Field,Lincoln,NE,40.820,-96.705,3819",
    "war-memorial-wy,War Memorial Stadium,,Laramie,WY,41.312,-105.568,3976",
    "paycor-stadium,Paycor Stadium,Paul Brown Stadium,Cincinnati,OH,39.095,-84.516,3874",
]
TEAM_ROWS = [
    "team_id,sport,name,short,home_stadium_id,avg_temp_f,conference,classification,aliases",
    "rutgers,cfb,Rutgers,RUTG,shi-stadium,55.0,Big Ten,fbs,Rutgers|RUTG|Rutgers Scarlet Knights",
    "san-diego-state,cfb,San Diego State,SDSU,snapdragon-stadium,65.0,Mountain West,fbs,"
    "San Diego State|SDSU|San Diego State Aztecs",
    "nebraska,cfb,Nebraska,NEB,memorial-stadium-ne,50.0,Big Ten,fbs,Nebraska|NEB|Nebraska Cornhuskers",
    "wyoming,cfb,Wyoming,WYO,war-memorial-wy,45.0,Mountain West,fbs,Wyoming|WYO|Wyoming Cowboys",
    "mercer,cfb,Mercer,MER,memorial-stadium-ne,60.0,Southern,fcs,Mercer|MER|Mercer Bears",
    "merrimack,cfb,Merrimack,MER,shi-stadium,48.0,FCS Independents,fcs,Merrimack|MER|Merrimack Warriors",
    "cin,nfl,Cincinnati Bengals,CIN,paycor-stadium,58.0,,nfl,CIN|Cincinnati Bengals|Bengals",
]
CFB_ALIASES = {
    "rutgers": ["Rutgers", "RUTG", "Rutgers Scarlet Knights"],
    "san-diego-state": ["San Diego State", "SDSU", "San Diego State Aztecs"],
    "nebraska": ["Nebraska", "NEB", "Nebraska Cornhuskers"],
    "wyoming": ["Wyoming", "WYO", "Wyoming Cowboys"],
    "mercer": ["Mercer", "MER", "Mercer Bears"],
    "merrimack": ["Merrimack", "MER", "Merrimack Warriors"],
}
NFL_ALIASES = {"cin": ["CIN", "Cincinnati Bengals", "Bengals"]}

CFBD_GAMES = [
    # venue id hits the csv -> id_hit
    {"season": 2019, "venueId": 3819, "venue": "Memorial Stadium", "homeTeam": "Nebraska",
     "awayTeam": "Wyoming"},
    # venue id unknown, name matches the slug index -> name_hit
    {"season": 2020, "venueId": 9001, "venue": "Snapdragon Stadium",
     "homeTeam": "San Diego State", "awayTeam": "Nebraska"},
    # neither id nor name resolves: a rename -> miss, and the home team saves it
    {"season": 2016, "venueId": 3754, "venue": "High Point Solutions Stadium",
     "homeTeam": "Rutgers", "awayTeam": "Nebraska"},
    {"season": 2017, "venueId": 3754, "venue": "High Point Solutions Stadium",
     "homeTeam": "Rutgers", "awayTeam": "Wyoming"},
]


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "aliases").mkdir(parents=True)
    (root / "stadiums.csv").write_text(STADIUM_COLUMNS + "\n".join(STADIUM_ROWS) + "\n",
                                       encoding="utf-8")
    (root / "teams.csv").write_text("\n".join(TEAM_ROWS) + "\n", encoding="utf-8")
    (root / "aliases" / "cfb.json").write_text(json.dumps(CFB_ALIASES), encoding="utf-8")
    (root / "aliases" / "nfl.json").write_text(json.dumps(NFL_ALIASES), encoding="utf-8")
    teams_mod.clear_cache()
    yield root
    teams_mod.clear_cache()
    teams_mod.reset_unresolved()


@pytest.fixture
def backtest_dir(tmp_path: Path) -> Path:
    root = tmp_path / "backtest"
    root.mkdir()
    (root / "cfbd_games_2019.json").write_text(json.dumps(CFBD_GAMES), encoding="utf-8")
    return root


def _verdict(answers: dict):
    """A Verdict-shaped stand-in: run_judge only calls ``.to_dict()``."""

    class _V:
        def to_dict(self) -> dict:
            return {"answers": answers, "model": "jev-test", "request_id": "req-1",
                    "input_tokens": 11, "output_tokens": 2, "latency_ms": 5.0,
                    "dry_run": False, "state_keys": [], "question_keys": []}

    return _V()


def _choice(choice_key: str, pick: str, prob: float, compat: dict | None = None) -> dict:
    answers = {choice_key: {"kind": "choice", "choice": pick, "confidence": prob,
                            "probabilities": {pick: prob}}}
    for key, value in (compat or {}).items():
        answers[key] = {"kind": "noul", "noul": value}
    return answers


# ---- sink ------------------------------------------------------------------------
def test_shadow_dir_is_overridable_and_jsonl_round_trips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IDENTITY_SHADOW_DIR", str(tmp_path / "shadow"))
    assert shadow_log.shadow_dir() == tmp_path / "shadow"
    monkeypatch.delenv("IDENTITY_SHADOW_DIR")
    assert shadow_log.shadow_dir() == shadow_log.DEFAULT_SHADOW_DIR

    path = tmp_path / "nested" / "rows.jsonl"
    shadow_log.append_jsonl(path, {"a": 1})
    shadow_log.append_jsonl(path, {"a": 2})
    assert shadow_log.read_jsonl(path) == [{"a": 1}, {"a": 2}]
    assert shadow_log.read_jsonl(tmp_path / "missing.jsonl") == []

    path.write_text(path.read_text(encoding="utf-8") + "{not json}\n", encoding="utf-8")
    assert len(shadow_log.read_jsonl(path)) == 2  # malformed line skipped, not fatal


def test_stable_key_is_stable_and_discriminating() -> None:
    assert shadow_log.stable_key("venue", ["Aloha Stadium", "3610"]) == \
        shadow_log.stable_key("venue", ["Aloha Stadium", "3610"])
    assert shadow_log.stable_key("venue", ["Aloha Stadium", "3610"]) != \
        shadow_log.stable_key("venue", ["Aloha Stadium", "3611"])


# ---- harvest: venues -------------------------------------------------------------
def test_harvest_venues_labels_the_branch_find_stadium_took(tmp_path: Path, data_dir: Path,
                                                            backtest_dir: Path) -> None:
    out = tmp_path / "out"
    stats = shadow.harvest_venues(out, backtest_dir, data_dir)
    assert stats["distinct"] == 3
    assert dict(stats["decisions"]) == {"id_hit": 1, "name_hit": 1, "miss": 1}

    rows = {r["venue_name"]: r for r in shadow_log.read_jsonl(stats["path"])}
    assert rows["Memorial Stadium"]["matcher_decision"] == "id_hit"
    assert rows["Memorial Stadium"]["resolved_stadium_id"] == "memorial-stadium-ne"
    assert rows["Snapdragon Stadium"]["matcher_decision"] == "name_hit"
    miss = rows["High Point Solutions Stadium"]
    assert miss["matcher_decision"] == "miss" and miss["resolved_stadium_id"] is None
    assert miss["seen_count"] == 2 and miss["seasons"] == [2016, 2017]
    assert miss["home_team"] == "Rutgers"


def test_harvest_venues_puts_the_misses_first(tmp_path: Path, data_dir: Path,
                                              backtest_dir: Path) -> None:
    stats = shadow.harvest_venues(tmp_path / "out", backtest_dir, data_dir)
    order = [r["matcher_decision"] for r in shadow_log.read_jsonl(stats["path"])]
    assert order == ["miss", "name_hit", "id_hit"]  # a --limit spends the budget well


def test_venue_positives_are_labeled_and_always_carry_their_own_answer(
        tmp_path: Path, data_dir: Path, backtest_dir: Path) -> None:
    stats = shadow.harvest_venues(tmp_path / "out", backtest_dir, data_dir)
    for row in shadow_log.read_jsonl(stats["path"]):
        ids = [c["stadium_id"] for c in row["candidates"]]
        assert len(ids) <= shadow.MAX_VENUE_CANDIDATES
        assert len(ids) == len(set(ids))
        if row["matcher_decision"] in shadow.VENUE_HITS:
            assert row["expected"] == row["resolved_stadium_id"]
            assert row["expected"] in ids      # a positive must be answerable at all
        else:
            assert row["expected"] is None     # misses wait for a human label


def test_venue_candidates_reserve_a_slot_for_the_home_team_stadium(
        tmp_path: Path, data_dir: Path, backtest_dir: Path) -> None:
    """A sponsor rename scores badly on the name alone; the program's own stadium is
    the candidate that makes the record answerable."""
    stats = shadow.harvest_venues(tmp_path / "out", backtest_dir, data_dir)
    miss = {r["venue_name"]: r for r in shadow_log.read_jsonl(stats["path"])}[
        "High Point Solutions Stadium"]
    by_id = {c["stadium_id"]: c for c in miss["candidates"]}
    assert "shi-stadium" in by_id
    assert by_id["shi-stadium"]["source"] == "home_team"
    assert by_id["shi-stadium"]["home_teams"] == ["Merrimack", "Rutgers"]


def test_venue_state_hint_comes_from_a_parenthesized_suffix(tmp_path: Path, data_dir: Path,
                                                            backtest_dir: Path) -> None:
    (backtest_dir / "cfbd_games_2021.json").write_text(json.dumps([
        {"season": 2021, "venueId": 4444, "venue": "War Memorial Stadium (AR)",
         "homeTeam": "Nebraska", "awayTeam": "Wyoming"}]), encoding="utf-8")
    stats = shadow.harvest_venues(tmp_path / "out", backtest_dir, data_dir)
    row = {r["venue_name"]: r for r in shadow_log.read_jsonl(stats["path"])}[
        "War Memorial Stadium (AR)"]
    assert row["state"] == "AR" and row["matcher_decision"] == "miss"


# ---- harvest: teams --------------------------------------------------------------
def _harvest_teams(out: Path, data_dir: Path, backtest_dir: Path, tmp_path: Path) -> dict:
    empty = tmp_path / "empty"
    empty.mkdir(exist_ok=True)
    return shadow.harvest_teams(out, empty, backtest_dir, empty, data_dir)


def test_harvest_teams_labels_exact_hits_and_keeps_labeled_rows_first(
        tmp_path: Path, data_dir: Path, backtest_dir: Path) -> None:
    stats = _harvest_teams(tmp_path / "out", data_dir, backtest_dir, tmp_path)
    rows = shadow_log.read_jsonl(stats["path"])
    assert stats["labeled"] == len(shadow.LABELED_TEAMS)
    assert [r["source"] for r in rows[:stats["labeled"]]] == ["labeled"] * stats["labeled"]

    corpus = {r["raw"]: r for r in rows if r["source"] == "corpus"}
    assert corpus["Nebraska"]["matcher_decision"] == "exact"
    assert corpus["Nebraska"]["expected"] == "nebraska"     # an alias-table positive
    assert "cfbd" in corpus["Nebraska"]["sources"]

    labeled = {r["raw"]: r for r in rows if r["source"] == "labeled"}
    assert labeled["MER"]["expected"] == NONE_OF_THESE
    assert "labeled" not in labeled["MER"]["books"]         # never leak the label into state
    assert labeled["MER"]["matcher_decision"] == "fuzzy_reject"


def test_team_candidates_are_capped_and_carry_the_resolved_team(
        tmp_path: Path, data_dir: Path, backtest_dir: Path) -> None:
    stats = _harvest_teams(tmp_path / "out", data_dir, backtest_dir, tmp_path)
    for row in shadow_log.read_jsonl(stats["path"]):
        ids = [c["team_id"] for c in row["candidates"]]
        assert len(ids) <= shadow.MAX_TEAM_CANDIDATES
        assert len(ids) == len(set(ids))
        for cand in row["candidates"]:
            assert len(cand["aliases"]) <= shadow.MAX_ALIASES
        if row["resolved_id"]:
            assert row["resolved_id"] in ids


def test_ambiguous_abbreviation_offers_every_claimant(tmp_path: Path, data_dir: Path,
                                                      backtest_dir: Path) -> None:
    stats = _harvest_teams(tmp_path / "out", data_dir, backtest_dir, tmp_path)
    mer = {r["raw"]: r for r in shadow_log.read_jsonl(stats["path"])}["MER"]
    ids = [c["team_id"] for c in mer["candidates"]]
    assert {"mercer", "merrimack"} <= set(ids)   # both share the alias; neither may win


def test_team_scores_respect_the_resolver_qualifier_filter(data_dir: Path) -> None:
    resolver = teams_mod.get_resolver("cfb", data_dir)
    scored = dict(shadow_log.team_scores(resolver, "Nebraska"))
    assert scored.get("nebraska") == 100.0
    # 'War Memorial' carries the qualifier-free path; a direction word mismatch is the
    # thing the filter exists for, and TeamResolver.fuzzy uses the same rule.
    assert shadow_log.team_decision(resolver, "Nebraska", "nebraska") == "exact"
    assert shadow_log.team_decision(resolver, "Zzzz Polytechnic", None) in (
        "fuzzy_reject", "unresolved")


# ---- questions -------------------------------------------------------------------
def test_venue_questions_are_one_choice_plus_one_noul_per_candidate(
        tmp_path: Path, data_dir: Path, backtest_dir: Path) -> None:
    stats = shadow.harvest_venues(tmp_path / "out", backtest_dir, data_dir)
    record = shadow_log.read_jsonl(stats["path"])[0]
    questions = venue_questions(record)
    n = len(record["candidates"])
    assert set(questions) == {VENUE_CHOICE_KEY} | {compat_key(i) for i in range(n)}

    state = venue_state(record)
    assert state["venue"] == record["venue_name"]
    assert len(state["candidates"]) == n
    assert "expected" not in json.dumps(state)      # the label never reaches the model


def test_team_questions_key_the_choice_on_team_ids(tmp_path: Path, data_dir: Path,
                                                   backtest_dir: Path) -> None:
    stats = _harvest_teams(tmp_path / "out", data_dir, backtest_dir, tmp_path)
    record = {r["raw"]: r for r in shadow_log.read_jsonl(stats["path"])}["MER"]
    questions = team_questions(record)
    n = len(record["candidates"])
    assert set(questions) == {TEAM_CHOICE_KEY} | {compat_key(i) for i in range(n)}
    assert team_state(record)["raw"] == "MER"


# ---- judge -----------------------------------------------------------------------
def test_judge_is_resumable_and_never_asks_twice(tmp_path: Path, data_dir: Path,
                                                 backtest_dir: Path) -> None:
    out = tmp_path / "out"
    shadow.harvest_venues(out, backtest_dir, data_dir)
    calls: list[dict] = []

    def fake(state, questions, dry_run=False):
        calls.append(state)
        return _verdict(_choice(VENUE_CHOICE_KEY, "c0", 0.9, {compat_key(0): 0.9}))

    first = shadow.run_judge(out, "venues", judge_fn=fake)
    assert first["judged"] == 3 and first["failed"] == 0
    second = shadow.run_judge(out, "venues", judge_fn=fake)
    assert second["judged"] == 0 and second["skipped"] == 3
    assert len(calls) == 3


def test_judge_limit_only_unresolved_and_max_hits(tmp_path: Path, data_dir: Path,
                                                  backtest_dir: Path) -> None:
    out = tmp_path / "out"
    shadow.harvest_venues(out, backtest_dir, data_dir)

    def fake(state, questions, dry_run=False):
        return _verdict(_choice(VENUE_CHOICE_KEY, NONE_OF_THESE, 0.8))

    assert shadow.run_judge(out, "venues", limit=1, judge_fn=fake)["judged"] == 1
    (out / "venues_verdicts.jsonl").unlink()
    assert shadow.run_judge(out, "venues", only_unresolved=True, judge_fn=fake)["judged"] == 1
    (out / "venues_verdicts.jsonl").unlink()
    assert shadow.run_judge(out, "venues", max_hits=1, judge_fn=fake)["judged"] == 2


def test_dry_run_writes_an_echo_the_report_ignores(tmp_path: Path, data_dir: Path,
                                                   backtest_dir: Path) -> None:
    out = tmp_path / "out"
    shadow.harvest_venues(out, backtest_dir, data_dir)

    class _Echo:
        def to_dict(self) -> dict:
            return {"answers": None, "dry_run": True, "input_tokens": None,
                    "output_tokens": None}

    shadow.run_judge(out, "venues", limit=2, dry_run=True, judge_fn=lambda *a, **k: _Echo())
    _, summary = shadow.report_venues(out)
    assert "dry-run ignored: 2" in summary


def test_judge_survives_a_failed_verdict(tmp_path: Path, data_dir: Path,
                                         backtest_dir: Path) -> None:
    out = tmp_path / "out"
    shadow.harvest_venues(out, backtest_dir, data_dir)
    stats = shadow.run_judge(out, "venues", judge_fn=lambda *a, **k: None)
    assert stats["judged"] == 0 and stats["failed"] == 3


def test_judge_reports_an_empty_corpus(tmp_path: Path) -> None:
    stats = shadow.run_judge(tmp_path, "teams", judge_fn=lambda *a, **k: None)
    assert stats["judged"] == 0 and "no candidates" in stats["note"]


# ---- accept rule + reports -------------------------------------------------------
def test_accept_needs_both_probability_and_the_companion_noul() -> None:
    assert shadow.accepted("shi-stadium", 0.91, 0.88)
    assert not shadow.accepted("shi-stadium", 0.55, 0.99)   # Choice not confident enough
    assert not shadow.accepted("shi-stadium", 0.91, 0.30)   # Noul vetoes it
    assert not shadow.accepted("shi-stadium", 0.91, None)   # no Noul == no promotion
    assert not shadow.accepted(NONE_OF_THESE, 0.99, 0.99)
    assert not shadow.accepted(None, 0.99, 0.99)


def test_report_venues_flags_a_disagreement_on_a_resolved_venue(
        tmp_path: Path, data_dir: Path, backtest_dir: Path) -> None:
    out = tmp_path / "out"
    shadow.harvest_venues(out, backtest_dir, data_dir)
    rows = {r["venue_name"]: r for r in shadow_log.read_jsonl(out / "venues_candidates.jsonl")}
    hit = rows["Memorial Stadium"]
    wrong = next(i for i, c in enumerate(hit["candidates"])
                 if c["stadium_id"] != hit["resolved_stadium_id"])
    shadow_log.append_jsonl(out / "venues_verdicts.jsonl", {
        "key": hit["key"], "kind": "venue", "candidate": {},
        "verdict": {"answers": _choice(VENUE_CHOICE_KEY, f"c{wrong}", 0.95,
                                       {compat_key(wrong): 0.9}),
                    "input_tokens": 10, "output_tokens": 2},
        "ts": datetime.now(UTC).isoformat()})
    text, summary = shadow.report_venues(out)
    assert "agreement 0.0%" in summary
    assert "LABELED MISS" in text


def test_report_venues_tables_the_misses_with_their_human_labels(
        tmp_path: Path, data_dir: Path, backtest_dir: Path) -> None:
    out = tmp_path / "out"
    shadow.harvest_venues(out, backtest_dir, data_dir)
    rows = {r["venue_name"]: r for r in shadow_log.read_jsonl(out / "venues_candidates.jsonl")}
    miss = rows["High Point Solutions Stadium"]
    index = next(i for i, c in enumerate(miss["candidates"]) if c["stadium_id"] == "shi-stadium")
    shadow_log.append_jsonl(out / "venues_verdicts.jsonl", {
        "key": miss["key"], "kind": "venue", "candidate": {},
        "verdict": {"answers": _choice(VENUE_CHOICE_KEY, f"c{index}", 0.93,
                                       {compat_key(index): 0.81}),
                    "input_tokens": 10, "output_tokens": 2},
        "ts": datetime.now(UTC).isoformat()})
    (out / "venues_labels.json").write_text(json.dumps({miss["key"]: "shi-stadium"}),
                                            encoding="utf-8")
    text, summary = shadow.report_venues(out)
    assert "misses judged: 1" in summary
    assert "SHI Stadium" in text and "unlabeled: **0**" in text


def test_report_teams_counts_labeled_accuracy(tmp_path: Path, data_dir: Path,
                                              backtest_dir: Path) -> None:
    out = tmp_path / "out"
    _harvest_teams(out, data_dir, backtest_dir, tmp_path)
    rows = {r["raw"]: r for r in shadow_log.read_jsonl(out / "teams_candidates.jsonl")}

    mer = rows["MER"]
    shadow_log.append_jsonl(out / "teams_verdicts.jsonl", {
        "key": mer["key"], "kind": "team", "candidate": {},
        "verdict": {"answers": _choice(TEAM_CHOICE_KEY, NONE_OF_THESE, 0.77),
                    "input_tokens": 9, "output_tokens": 2},
        "ts": datetime.now(UTC).isoformat()})

    nebraska = rows["Nebraska"]
    index = next(i for i, c in enumerate(nebraska["candidates"]) if c["team_id"] == "nebraska")
    shadow_log.append_jsonl(out / "teams_verdicts.jsonl", {
        "key": nebraska["key"], "kind": "team", "candidate": {},
        "verdict": {"answers": _choice(TEAM_CHOICE_KEY, "nebraska", 0.96,
                                       {compat_key(index): 0.94}),
                    "input_tokens": 9, "output_tokens": 2},
        "ts": datetime.now(UTC).isoformat()})

    text, summary = shadow.report_teams(out)
    assert "labeled 2/2 correct (100.0%)" in summary
    assert "MISS" not in text.replace("misses", "")


# ---- emit-only hooks -------------------------------------------------------------
def test_team_hook_is_silent_without_the_flag(tmp_path: Path, data_dir: Path,
                                              monkeypatch) -> None:
    monkeypatch.setenv("IDENTITY_SHADOW_DIR", str(tmp_path / "shadow"))
    monkeypatch.delenv("IDENTITY_SHADOW_LOG", raising=False)
    assert teams_mod.normalize_team("cfb", "Nebraska", "kalshi", data_dir=data_dir) == "nebraska"
    assert not (tmp_path / "shadow").exists()


def test_team_hook_emits_only_the_decisions_under_review(tmp_path: Path, data_dir: Path,
                                                         monkeypatch) -> None:
    shadow_dir = tmp_path / "shadow"
    monkeypatch.setenv("IDENTITY_SHADOW_DIR", str(shadow_dir))
    monkeypatch.setenv("IDENTITY_SHADOW_LOG", "1")

    # exact alias hit: resolved, and deliberately not emitted
    assert teams_mod.normalize_team("cfb", "Nebraska", "kalshi", data_dir=data_dir) == "nebraska"
    # shared abbreviation: unresolved, and the whole point of the hook
    assert teams_mod.normalize_team("cfb", "MER", "kalshi", data_dir=data_dir) is None
    teams_mod.reset_unresolved()

    rows = shadow_log.read_jsonl(shadow_dir / "live_teams_candidates.jsonl")
    assert [r["raw"] for r in rows] == ["MER"]
    row = rows[0]
    assert row["decision"] in ("fuzzy_reject", "unresolved") and row["resolved_id"] is None
    assert row["book"] == "kalshi" and row["sport"] == "cfb"
    assert len(row["top5"]) <= 5 and {"mercer", "merrimack"} <= {c["team_id"] for c in row["top5"]}
    assert row["FUZZY_MIN"] == teams_mod.FUZZY_MIN
    assert row["FUZZY_MARGIN"] == teams_mod.FUZZY_MARGIN


def test_venue_hook_emits_on_the_unresolved_path_only(tmp_path: Path, data_dir: Path,
                                                      monkeypatch) -> None:
    shadow_dir = tmp_path / "shadow"
    monkeypatch.setenv("IDENTITY_SHADOW_DIR", str(shadow_dir))
    book = load_stadium_book(data_dir)
    kick = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)

    def game(home: str, stadium_id: str | None, neutral: bool = False) -> Game:
        return Game(game_id=make_game_id("cfb", 2026, 1, "wyoming", home), sport="cfb",
                    season=2026, week=1, kickoff_utc=kick, kickoff_local=kick, tz="UTC",
                    home_id=home, away_id="wyoming", stadium_id=stadium_id, neutral=neutral)

    monkeypatch.setenv("IDENTITY_SHADOW_LOG", "1")
    resolved = book.resolve(game("nebraska", "3819"))
    assert resolved.stadium is not None and resolved.stadium_source == "game.stadium_id"

    unknown = book.resolve(game("no-such-team", "Georgia Dome"))
    assert unknown.stadium is None      # unchanged production behaviour

    rows = shadow_log.read_jsonl(shadow_dir / "live_venues_candidates.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["stadium_id"] == "Georgia Dome" and row["home_team"] == "no-such-team"
    assert row["source_chain"][0] == "game.stadium_id='Georgia Dome'"
    assert "home_team" in row["source_chain"][1]
    assert len(row["top5"]) <= 5 and all("score" in c for c in row["top5"])


def test_venue_hook_never_breaks_resolve(tmp_path: Path, data_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("IDENTITY_SHADOW_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("IDENTITY_SHADOW_LOG", "1")
    monkeypatch.setattr(shadow_log, "venue_top_candidates",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    book = load_stadium_book(data_dir)
    kick = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)
    game = Game(game_id="cfb:2026:1:wyoming@ghost", sport="cfb", season=2026, week=1,
                kickoff_utc=kick, kickoff_local=kick, tz="UTC", home_id="ghost",
                away_id="wyoming", stadium_id="Nowhere Field")
    assert book.resolve(game).stadium is None   # the sink raising must not surface
