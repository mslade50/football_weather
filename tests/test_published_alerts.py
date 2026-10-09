"""Failed publication must not send; saved receipts survive later failures."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import alerts, published_alerts, state
from pipeline.outputs import json_out

NOW = datetime(2026, 10, 8, 19, tzinfo=timezone.utc)
RUN = "published-test"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    monkeypatch.setenv("FOOTBALL_TELEGRAM_ENABLED", "1")
    monkeypatch.setattr(alerts, "enrich_liquidity", lambda cards: None)
    board_dir, state_dir = tmp_path / "board", tmp_path / "state"
    board_dir.mkdir()
    state_dir.mkdir()
    meta = {"run_id": RUN, "last_updated": NOW.isoformat()}
    card = {
        "game_id": "nfl:2026:5:a@b", "sport": "nfl", "season": 2026, "week": 5,
        "kickoff_utc": (NOW + timedelta(days=2)).isoformat(),
        "home": {"short": "B"}, "away": {"short": "A"},
        "stadium": {"name": "test", "roof_state": "outdoors"},
        "signal": {"label": "Mid Impact", "level": "Mid Impact", "drivers": ["wind"]},
        "weather": {"wind_fg": 18, "temp_fg": 45, "rain_fg": 0},
        "consensus": {"total_now": 45}, "fair": {}, "odds": {},
    }
    json_out.dump_json(board_dir / "meta.json", meta)
    json_out.dump_json(board_dir / "games_nfl.json", {"meta": meta, "games": [card]})
    return board_dir, state_dir


def test_failed_publication_never_sends_or_writes_receipts(board):
    messages, uploads = [], []

    def fail(*args):
        raise RuntimeError("old published run")

    with pytest.raises(RuntimeError, match="old published run"):
        published_alerts.notify_published(*board, RUN, "bucket", verifier=fail,
                                         uploader=lambda *args: uploads.append(args),
                                         sender=lambda *args: messages.append(args), now=NOW)
    assert not messages and not uploads


def test_receipt_survives_d1_failure_and_prevents_repeat(board):
    messages, remote = [], {}

    def upload(bucket, key, path, content_type):
        remote[key] = json.loads(path.read_text(encoding="utf-8"))

    def send(*args):
        messages.append(args)
        return True

    def fail_archive(path):
        raise RuntimeError("D1 unavailable")

    with pytest.raises(RuntimeError, match="D1 unavailable"):
        published_alerts.notify_published(*board, RUN, "bucket", verifier=lambda *args: None,
                                         uploader=upload, archiver=fail_archive, sender=send, now=NOW)
    assert len(messages) == 1
    assert remote["board/alerts.json"]["sent"]
    assert len(remote["board/alerts_feed.json"]["alerts"]) == 1
    archived = []
    second = published_alerts.notify_published(*board, RUN, "bucket", verifier=lambda *args: None,
                                              uploader=upload, archiver=lambda path: archived.append(path.read_text()),
                                              sender=send, now=NOW)
    assert second.n_alerts == 0 and len(messages) == 1
    assert "INSERT INTO alerts" in archived[0] and "n_alerts = 1" in archived[0]


def test_unavailable_receipt_storage_stops_before_send(board):
    messages = []

    def fail_upload(*args):
        raise RuntimeError("R2 unavailable")

    with pytest.raises(RuntimeError, match="R2 unavailable"):
        published_alerts.notify_published(*board, RUN, "bucket", verifier=lambda *args: None,
                                         uploader=fail_upload, sender=lambda *args: messages.append(args), now=NOW)
    assert not messages


def test_checkpoint_failure_stops_next_message():
    candidates = [alerts.Candidate(f"key-{i}", "edge", "nfl", "message", game_id=str(i)) for i in range(2)]
    messages = []
    alert_state = {"sent": {}}

    def fail_checkpoint():
        raise RuntimeError("receipt failed")

    with pytest.raises(RuntimeError, match="receipt failed"):
        alerts.dispatch(alerts.Plan(send=candidates), alert_state,
                        lambda *args: messages.append(args) or True, NOW, alerts.Config(), fail_checkpoint)
    assert len(messages) == 1
    assert state.alert_sent(alert_state, "key-0") and not state.alert_sent(alert_state, "key-1")


def test_safe_refresh_does_not_touch_delivery_state(board, monkeypatch):
    monkeypatch.setenv("TELEGRAM_DISABLED", "1")
    assert published_alerts.notify_published(*board, RUN, "bucket",
                                            verifier=lambda *args: pytest.fail("must not read remote")) is None
    assert not (board[1] / "alerts.json").exists()


def test_liquidity_delay_crossing_kickoff_does_not_send(board, monkeypatch):
    path = board[0] / "games_nfl.json"
    payload = json.loads(path.read_text())
    payload["games"][0]["kickoff_utc"] = (NOW + timedelta(minutes=1)).isoformat()
    json_out.dump_json(path, payload)
    monkeypatch.setattr(published_alerts, "now_utc", lambda: NOW)
    clock = iter((NOW, NOW + timedelta(minutes=2)))
    monkeypatch.setattr(alerts, "now_utc", lambda: next(clock))
    messages = []
    result = published_alerts.notify_published(*board, RUN, "bucket", verifier=lambda *args: None,
                                              uploader=lambda *args: None, archiver=lambda *args: None,
                                              sender=lambda *args: messages.append(args) or True)
    assert not messages and result.n_alerts == 0
