import json

import pytest

from pipeline.publication import prepare_generation, verify_generation


def files(tmp_path, run="fixture"):
    board = tmp_path / "board"
    board.mkdir(exist_ok=True)
    rows = {
        "meta.json": {"run_id": run, "git_sha": "a" * 40, "sport_counts": {"nfl": 1, "cfb": 0}},
        "games_nfl.json": {"meta": {"run_id": run}, "games": [{"run_id": run, "game_id": "fixture"}]},
        "games_cfb.json": {"meta": {"run_id": run}, "games": []},
        "history.json": {"series": {}},
    }
    for name, value in rows.items():
        (board / name).write_text(json.dumps(value), encoding="utf8")
    return {f"board/{name}": board / name for name in rows}


def test_sealed_generation_keeps_previous_bytes_after_partial_new_publish(tmp_path):
    inputs = files(tmp_path)
    sealed, pointer = prepare_generation(inputs, tmp_path / "publication")
    old = json.loads(pointer.read_bytes())
    remote = {key: path.read_bytes() for key, path in sealed.items()}
    assert verify_generation(old, remote.get)["run_id"] == "fixture"
    next_files = files(tmp_path, "next")
    next_sealed, next_pointer = prepare_generation(next_files, tmp_path / "publication")
    key, path = next(iter(next_sealed.items()))
    remote[key] = path.read_bytes()  # Incomplete replacement.
    assert verify_generation(old, remote.get)["run_id"] == "fixture"
    with pytest.raises(ValueError):
        verify_generation(json.loads(next_pointer.read_bytes()), remote.get)
    for key, path in next_sealed.items():
        remote[key] = path.read_bytes()
    assert verify_generation(json.loads(next_pointer.read_bytes()), remote.get)["run_id"] == "next"


def test_generation_receipt_rejects_corruption_mixed_games_counts_and_incomplete_board(tmp_path):
    inputs = files(tmp_path)
    sealed, pointer = prepare_generation(inputs, tmp_path / "publication")
    remote = {key: path.read_bytes() for key, path in sealed.items()}
    key = next(key for key in remote if key.endswith("games_nfl.json"))
    remote[key] += b" "
    with pytest.raises(ValueError, match="checksum"):
        verify_generation(json.loads(pointer.read_bytes()), remote.get)
    inputs["board/games_nfl.json"].write_text('{"meta":{"run_id":"other"},"games":[]}', encoding="utf8")
    with pytest.raises(ValueError, match="envelope"):
        prepare_generation(inputs, tmp_path / "publication")
    with pytest.raises(ValueError, match="missing"):
        prepare_generation({"board/meta.json": inputs["board/meta.json"]}, tmp_path / "publication")
