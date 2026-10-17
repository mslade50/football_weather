"""Saturday CFB hours publish when college football is healthy.

The 2026-10-17 15:00 UTC refresh (run 39043184946) rebuilt 108 CFB games and
did not rebuild NFL. The previous board (run 39034528962) had 27 NFL games.
The publish check counted that absence as zero and refused to publish, so the
college board stayed on the 12:58 UTC snapshot. These tests are the dry-run of
that check and of the page that should have fired.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.outputs import r2
from pipeline.publication import prepare_generation, retain_unrebuilt_sports, verify_generation
from scripts import publish_r2

# Exact ids and counts from the Actions logs. The failed hour's CFB count
# matched the morning board; only the missing NFL count tripped the floor.
FAILED_RUN = "20261017T150154Z-gh39043184946-1"
PREVIOUS_RUN = "20261017T125948Z-gh39034528962-1-pw"
PREVIOUS_COUNTS = {"nfl": 27, "cfb": 108}


def _meta(path: Path, run: str, sport: str, counts: dict) -> None:
    path.write_text(json.dumps({
        "run_id": run, "sport": sport, "git_sha": "a" * 40, "sport_counts": counts,
    }), encoding="utf-8")


def _prev(path: Path, generation: str | None = None) -> None:
    body = {"run_id": PREVIOUS_RUN, "sport": "all", "sport_counts": dict(PREVIOUS_COUNTS)}
    if generation:
        body["publication"] = {"generation": generation}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _nfl_board(run: str = PREVIOUS_RUN) -> bytes:
    games = [
        {"game_id": f"nfl:2026:7:away-{i}@home-{i}", "run_id": run, "sport": "nfl", "wind_fg": 12}
        for i in range(PREVIOUS_COUNTS["nfl"])
    ]
    return json.dumps({"meta": {"run_id": run}, "games": games}).encode()


def test_saturday_cfb_hour_passes_the_same_check_that_rejected_run_39043184946(tmp_path: Path, capsys):
    meta, prev = tmp_path / "meta.json", tmp_path / "prev.json"
    _meta(meta, FAILED_RUN, "cfb", {"cfb": 108})
    _prev(prev)
    code = r2.main([
        "--self-check", "--run-id", FAILED_RUN, "--meta-file", str(meta), "--prev-meta", str(prev),
    ])
    assert code == 0
    assert f"self-check OK: meta.run_id={FAILED_RUN}" in capsys.readouterr().out


def test_empty_or_broken_cfb_rebuild_still_fails_loudly(tmp_path: Path, capsys):
    meta, prev = tmp_path / "meta.json", tmp_path / "prev.json"
    _prev(prev)
    _meta(meta, FAILED_RUN, "cfb", {"cfb": 0})
    assert r2.main([
        "--self-check", "--run-id", FAILED_RUN, "--meta-file", str(meta), "--prev-meta", str(prev),
    ]) == 1
    out = capsys.readouterr().out
    assert "refusing to publish an empty board (cfb 0)" in out
    assert "content floor: cfb games 0 < 50% of previous 108" in out

    _meta(meta, FAILED_RUN, "cfb", {"cfb": 53})  # 50% of 108 is 54
    assert r2.main([
        "--self-check", "--run-id", FAILED_RUN, "--meta-file", str(meta), "--prev-meta", str(prev),
    ]) == 1
    assert "content floor: cfb games 53 < 50% of previous 108" in capsys.readouterr().out

    _meta(meta, FAILED_RUN, "cfb", {"cfb": 54})
    assert r2.main([
        "--self-check", "--run-id", FAILED_RUN, "--meta-file", str(meta), "--prev-meta", str(prev),
    ]) == 0


def test_a_run_that_publishes_nothing_fails_even_without_a_previous_board(tmp_path: Path, capsys):
    meta = tmp_path / "meta.json"
    _meta(meta, FAILED_RUN, "cfb", {"cfb": 0})
    assert r2.main(["--self-check", "--run-id", FAILED_RUN, "--meta-file", str(meta)]) == 1
    assert "refusing to publish an empty board (cfb 0)" in capsys.readouterr().out
    assert r2.main([
        "--self-check", "--run-id", FAILED_RUN, "--meta-file", str(meta), "--force",
    ]) == 0


def test_full_run_still_rejects_a_wiped_nfl_board(tmp_path: Path, capsys):
    meta, prev = tmp_path / "meta.json", tmp_path / "prev.json"
    _meta(meta, "full-run", "all", {"nfl": 0, "cfb": 108})
    _prev(prev)
    assert r2.main([
        "--self-check", "--run-id", "full-run", "--meta-file", str(meta), "--prev-meta", str(prev),
    ]) == 1
    out = capsys.readouterr().out
    assert "content floor: nfl games 0 < 50% of previous 27" in out
    assert "empty board" not in out


def test_cfb_publish_keeps_the_previous_nfl_games(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    board = Path("data/board")
    board.mkdir(parents=True)
    _meta(board / "meta.json", FAILED_RUN, "cfb", {"cfb": 108})
    cfb_games = {"meta": {"run_id": FAILED_RUN}, "games": [
        {"game_id": f"cfb:2026:8:a-{i}@b-{i}", "run_id": FAILED_RUN, "sport": "cfb"} for i in range(108)
    ]}
    (board / "games_cfb.json").write_text(json.dumps(cfb_games), encoding="utf-8")
    (board / "board.json").write_text(json.dumps({"meta": {"run_id": FAILED_RUN}, "rows": []}), encoding="utf-8")
    generation = "ab" * 32
    _prev(Path("data/state/prev_meta.json"), generation)
    stored = {f"board/generations/{generation}/games_nfl.json": _nfl_board()}
    uploaded: dict[str, bytes] = {}

    def reader(key: str) -> bytes | None:
        return stored.get(key)

    def upload(bucket, key, path, content_type):
        uploaded[key] = Path(path).read_bytes()

    def read_back(key: str) -> bytes | None:
        return uploaded.get(key)

    publish_r2.publish_phase("board", "bucket", upload_fn=upload, retain_reader=reader)
    publish_r2.publish_phase("meta", "bucket", upload_fn=upload, read_fn=read_back, retain_reader=reader)

    nfl_keys = [key for key in uploaded if key.endswith("/games_nfl.json")]
    assert len(nfl_keys) == 1
    payload = json.loads(uploaded[nfl_keys[0]])
    assert len(payload["games"]) == 27
    assert {row["game_id"] for row in payload["games"]} == {
        f"nfl:2026:7:away-{i}@home-{i}" for i in range(27)
    }
    assert all(row["run_id"] == FAILED_RUN and row["wind_fg"] == 12 for row in payload["games"])
    assert payload["meta"]["run_id"] == FAILED_RUN
    pointer = json.loads(uploaded["board/meta.json"])
    assert pointer["sport"] == "cfb"
    assert pointer["sport_counts"] == {"cfb": 108, "nfl": 27}
    assert pointer["retained_sports"]["nfl"] == {"from_run_id": PREVIOUS_RUN, "games": 27}
    assert pointer["run_id"] == FAILED_RUN
    verify_generation(pointer, read_back)
    # Alert delivery reads the local board. Carried NFL must not land there.
    assert not (board / "games_nfl.json").exists()
    assert json.loads((board / "meta.json").read_text(encoding="utf-8"))["sport_counts"] == {"cfb": 108}


def test_missing_previous_sport_refuses_before_any_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    board = Path("data/board")
    board.mkdir(parents=True)
    _meta(board / "meta.json", FAILED_RUN, "cfb", {"cfb": 108})
    (board / "games_cfb.json").write_text(json.dumps({
        "meta": {"run_id": FAILED_RUN},
        "games": [{"game_id": "cfb:2026:8:a@b", "run_id": FAILED_RUN}],
    }), encoding="utf-8")
    _prev(Path("data/state/prev_meta.json"))
    calls = []

    def upload(*args):
        calls.append(args)

    with pytest.raises(RuntimeError, match="previous board is missing"):
        publish_r2.publish_phase("board", "bucket", upload_fn=upload, retain_reader=lambda key: None)
    assert calls == []


def test_retain_then_seal_uses_the_generation_copy(tmp_path: Path):
    board = tmp_path / "board"
    board.mkdir()
    _meta(board / "meta.json", FAILED_RUN, "cfb", {"cfb": 1})
    (board / "games_cfb.json").write_text(json.dumps({
        "meta": {"run_id": FAILED_RUN}, "games": [{"game_id": "cfb:2026:8:a@b", "run_id": FAILED_RUN}],
    }), encoding="utf-8")
    generation = "cd" * 32
    prev = {"run_id": PREVIOUS_RUN, "sport_counts": {"nfl": 1, "cfb": 1},
            "publication": {"generation": generation}}
    files = retain_unrebuilt_sports(
        {"board/meta.json": board / "meta.json", "board/games_cfb.json": board / "games_cfb.json"},
        prev,
        lambda key: _nfl_board() if key.endswith("games_nfl.json") else None,
        tmp_path / "retain",
    )
    sealed, pointer = prepare_generation(files, tmp_path / "publication")
    remote = {key: path.read_bytes() for key, path in sealed.items()}
    manifest = verify_generation(json.loads(pointer.read_bytes()), remote.get)
    nfl = json.loads(remote[next(key for key in remote if key.endswith("games_nfl.json"))])
    assert len(nfl["games"]) == 27
    assert manifest["run_id"] == FAILED_RUN
    assert json.loads((board / "meta.json").read_text(encoding="utf-8"))["sport_counts"] == {"cfb": 1}
