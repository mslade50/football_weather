"""Content-addressed board generation; one meta pointer commits verified bytes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

# Generic builds have stale snapshots of these other writers' live state.
# Notification ledgers belong exclusively to the post-publication notifier,
# including when that notifier runs in the pipeline. Never round-trip them.
BUILD_PROTECTED_NAMES = frozenset({
    'alerts', 'telegram_state', 'alerts_live_feed', 'notification_owner',
    'bet_confirmations', 'cf_heartbeat', 'live_quotes',
    'owner_review_outbox', 'review_outbox_owner', 'owner_review_receipts',
    # Failure-page incident marker. notify_failure owns it; a board publish must
    # not replace it with a stale or missing copy.
    'pipeline_health',
    # Paper sidecar owns these; generic board builds never round-trip them.
    'paper', 'paper_health',
})

PUBLIC_NAMES = {
    "meta.json",
    "games_nfl.json",
    "games_cfb.json",
    "board.json",
    "history.json",
    "wx_history.json",
    "alerts_feed.json",
    "status.json",
    "backtest.json",
}

SPORTS = ("nfl", "cfb")
_RUN_STAMP = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z")


def rebuilt_sports(meta: dict | None) -> frozenset[str]:
    """Sports this run rebuilt.

    ``nfl`` or ``cfb`` is a partial refresh: only that sport is checked, and the
    other sport's last published games are kept. ``all`` (and a meta that does
    not say) keeps the full two-sport check.
    """
    sport = meta.get("sport") if isinstance(meta, dict) else None
    if sport in SPORTS:
        return frozenset((sport,))
    return frozenset(SPORTS)


def previous_games_keys(prev_meta: dict | None, sport: str) -> list[str]:
    """Where the live board stored ``sport`` before this run."""
    keys: list[str] = []
    publication = (prev_meta or {}).get("publication") if isinstance(prev_meta, dict) else None
    generation = publication.get("generation") if isinstance(publication, dict) else None
    if (
        isinstance(generation, str)
        and len(generation) == 64
        and all(c in "0123456789abcdef" for c in generation)
    ):
        keys.append(f"board/generations/{generation}/games_{sport}.json")
    keys.append(f"board/games_{sport}.json")
    return keys


def _previous_games(prev_meta: dict | None, sport: str, reader: Callable[[str], bytes | None]) -> dict:
    saw_key = False
    for key in previous_games_keys(prev_meta, sport):
        saw_key = True
        try:
            raw = reader(key)
        except Exception as exc:  # noqa: BLE001 - a read error must not become an empty sport
            raise RuntimeError(f"Refusing to publish: could not read the previous {sport} board") from exc
        if raw is None:
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError(f"Refusing to publish: previous {sport} board is unreadable") from exc
        if isinstance(payload, list):
            payload = {"games": payload, "meta": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("games"), list):
            raise RuntimeError(f"Refusing to publish: previous {sport} board is unreadable")
        return payload
    if not saw_key:
        raise RuntimeError(f"Refusing to publish: previous {sport} board is unavailable")
    raise RuntimeError(f"Refusing to publish: {sport} was not rebuilt and its previous board is missing")


def _refreshed_at(prev_meta: dict | None, sport: str, envelope: dict, from_run: object) -> str | None:
    """When the carried sport's weather and prices were last built.

    The games file's own ``last_updated`` wins, then a timestamp already recorded
    for this sport, then the previous board's clock, then the time embedded in
    ``from_run``. A later partial run must not make the copied sport look new.
    """
    stamp = envelope.get("last_updated")
    if isinstance(stamp, str) and stamp.strip():
        return stamp
    previous = (prev_meta or {}).get("retained_sports") if isinstance(prev_meta, dict) else None
    carried = previous.get(sport) if isinstance(previous, dict) else None
    if isinstance(carried, dict):
        stamp = carried.get("refreshed_at")
        if isinstance(stamp, str) and stamp.strip():
            return stamp
    if isinstance(prev_meta, dict):
        for key in ("last_updated", "finished_at"):
            stamp = prev_meta.get(key)
            if isinstance(stamp, str) and stamp.strip():
                return stamp
    if isinstance(from_run, str):
        match = _RUN_STAMP.match(from_run)
        if match:
            year, month, day, hour, minute, second = match.groups()
            return f"{year}-{month}-{day}T{hour}:{minute}:{second}Z"
    return None


def _project_rows(games: list) -> list | None:
    from pipeline.outputs.json_out import table_row

    rows = []
    for card in games:
        if not isinstance(card, dict):
            return None
        try:
            rows.append(table_row(card))
        except (KeyError, TypeError, AttributeError):
            return None
    return rows


def _merge_carried_board(
    files: dict[str, Path],
    dest: Path,
    carried: dict[str, list],
    run_id: str,
) -> dict[str, Path]:
    """Put carried games into ``board.json`` so the plain table matches the games files.

    Rows are the same projection a full rebuild would write. A card that cannot
    be projected leaves the rebuilt-sport file in place rather than publishing a
    half-updated table. The original board file is not modified.
    """
    source = files.get("board/board.json")
    if source is None or not carried:
        return files
    try:
        board = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return files
    if not isinstance(board, dict) or not isinstance(board.get("rows"), list):
        return files
    extra: list = []
    for sport in SPORTS:
        games = carried.get(sport)
        if not games:
            continue
        projected = _project_rows(games)
        if projected is None:
            print(f"Leaving board.json unchanged: {sport} cards could not be projected into table rows")
            return files
        extra.extend(projected)
    sports = set(carried)
    kept = [row for row in board["rows"] if not (isinstance(row, dict) and row.get("sport") in sports)]
    merged = dict(board)
    merged["rows"] = kept + extra
    meta = merged.get("meta")
    if isinstance(meta, dict):
        meta = dict(meta)
        meta["run_id"] = run_id
        sport_counts = dict(meta["sport_counts"]) if isinstance(meta.get("sport_counts"), dict) else {}
        for sport, games in carried.items():
            sport_counts[sport] = len(games)
        meta["sport_counts"] = sport_counts
        merged["meta"] = meta
    path = dest / "board.json"
    try:
        path.write_text(json.dumps(merged, allow_nan=False), encoding="utf-8")
    except ValueError as exc:
        raise RuntimeError("Refusing to publish: carried board table is unreadable") from exc
    updated = dict(files)
    updated["board/board.json"] = path
    return updated


def retain_unrebuilt_sports(
    files: dict[str, Path],
    prev_meta: dict | None,
    reader: Callable[[str], bytes | None],
    dest: Path,
    *,
    floor: float = 0.5,
) -> dict[str, Path]:
    """Copy sports this run did not rebuild into the files about to be sealed.

    A college-only hour must not replace the NFL board with an empty file. The
    copied cards keep their prices and weather; only ``run_id`` is updated so
    the new publication still verifies as one generation. The original meta
    file is left untouched.
    """
    meta_path = files.get("board/meta.json")
    if meta_path is None:
        return files
    try:
        meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return files
    if not isinstance(meta, dict) or not isinstance(meta.get("sport_counts"), dict):
        return files
    prev_counts = (prev_meta or {}).get("sport_counts") if isinstance(prev_meta, dict) else None
    if not isinstance(prev_counts, dict):
        prev_counts = {}
    rebuilt = rebuilt_sports(meta)
    retained: dict[str, dict] = {}
    carried_games: dict[str, list] = {}
    updated = dict(files)
    dest.mkdir(parents=True, exist_ok=True)
    for sport in SPORTS:
        name = f"games_{sport}.json"
        if sport in rebuilt or f"board/{name}" in files:
            continue
        try:
            before = int(prev_counts.get(sport) or 0)
        except (TypeError, ValueError):
            before = 0
        if before <= 0:
            continue
        payload = _previous_games(prev_meta, sport, reader)
        games = payload["games"]
        if len(games) < floor * before:
            raise RuntimeError(
                f"Refusing to publish: previous {sport} board has {len(games)} games, "
                f"below {floor:.0%} of the previous count {before}"
            )
        run_id = meta.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError("Refusing to publish: run_id missing")
        stamped_games = []
        for row in games:
            if not isinstance(row, dict) or not isinstance(row.get("game_id"), str):
                raise RuntimeError(f"Refusing to publish: previous {sport} board has an unusable game card")
            stamped_games.append({**row, "run_id": run_id})
        envelope = dict(payload.get("meta") or {})
        from_run = envelope.get("run_id")
        refreshed_at = _refreshed_at(prev_meta, sport, envelope, from_run)
        if refreshed_at and not (isinstance(envelope.get("last_updated"), str) and envelope["last_updated"].strip()):
            envelope["last_updated"] = refreshed_at
        envelope["run_id"] = run_id
        path = dest / name
        try:
            path.write_text(
                json.dumps({"meta": envelope, "games": stamped_games}, allow_nan=False),
                encoding="utf-8",
            )
        except ValueError as exc:
            raise RuntimeError(f"Refusing to publish: previous {sport} board is unreadable") from exc
        updated[f"board/{name}"] = path
        info = {"from_run_id": from_run, "games": len(stamped_games)}
        if refreshed_at:
            info["refreshed_at"] = refreshed_at
        retained[sport] = info
        carried_games[sport] = stamped_games
        print(f"Retaining {sport}: {len(stamped_games)} games from previous run {from_run}")
    if not retained:
        return files
    updated = _merge_carried_board(updated, dest, carried_games, run_id)
    counts = dict(meta["sport_counts"])
    for sport, info in retained.items():
        counts[sport] = info["games"]
    sealed_meta = {**meta, "sport_counts": counts, "retained_sports": retained}
    meta_out = dest / "meta.json"
    meta_out.write_text(json.dumps(sealed_meta, allow_nan=False), encoding="utf-8")
    updated["board/meta.json"] = meta_out
    return updated


def encoded(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def prepare_generation(files: dict[str, Path], target: Path) -> tuple[dict[str, Path], Path]:
    """Seal local payloads without mutating sources. Same bytes yield same keys."""
    paths = {
        Path(key).name: Path(path)
        for key, path in files.items()
        if key.startswith("board/") and Path(key).name in PUBLIC_NAMES and Path(key).name != 'backtest.json'
    }
    if "meta.json" not in paths:
        raise ValueError("Publication metadata missing")
    data = {name: path.read_bytes() for name, path in paths.items()}
    meta = json.loads(data["meta.json"])
    if not isinstance(meta, dict) or not meta.get("run_id"):
        raise ValueError("Publication run_id missing")
    meta = {key: value for key, value in meta.items() if key not in ("publication", "publication_status", "resident")}
    if not isinstance(meta.get('sport_counts'), dict):
        raise ValueError('Publication sport counts missing')
    for sport in ("nfl", "cfb"):
        name = f"games_{sport}.json"
        if name not in data:
            if meta['sport_counts'].get(sport, 0) != 0:
                raise ValueError(f"Publication {name} missing")
            data[name] = encoded({'meta': {'run_id': meta['run_id']}, 'games': []})
        meta['sport_counts'].setdefault(sport, 0)
        payload = json.loads(data[name])
        rows = payload if isinstance(payload, list) else payload.get("games")
        if not isinstance(rows, list) or (
            isinstance(payload, dict) and payload.get("meta", {}).get("run_id") != meta["run_id"]
        ):
            raise ValueError(f"{sport} publication envelope mismatch")
        if any(row.get("run_id") != meta["run_id"] for row in rows):
            raise ValueError(f"{sport} publication cards mismatch")
        if meta.get("sport_counts", {}).get(sport) != len(rows):
            raise ValueError(f"{sport} publication count mismatch")
    data['meta.json'] = encoded(meta)
    objects = {name: {"sha256": digest(body), "bytes": len(body)} for name, body in sorted(data.items())}
    manifest = {"schema_version": 1, "run_id": meta["run_id"], "git_sha": meta.get("git_sha"), "objects": objects}
    manifest_bytes = encoded(manifest)
    generation = digest(manifest_bytes)
    directory = target / generation
    directory.mkdir(parents=True, exist_ok=True)
    immutable = {}
    for name, body in {**data, "manifest.json": manifest_bytes}.items():
        path = directory / name
        if path.exists() and path.read_bytes() != body:
            raise ValueError("Immutable generation collision")
        path.write_bytes(body)
        immutable[f"board/generations/{generation}/{name}"] = path
    pointer = {
        **meta,
        "publication": {
            "schema_version": 1,
            "generation": generation,
            "manifest_key": f"board/generations/{generation}/manifest.json",
            "manifest_sha256": generation,
        },
    }
    pointer_path = directory / "pointer.json"
    pointer_path.write_bytes(encoded(pointer))
    return immutable, pointer_path


def verify_generation(meta: dict, getter: Callable[[str], bytes | None]) -> dict:
    publication = meta.get("publication") or {}
    generation = publication.get("generation")
    if not isinstance(generation, str) or len(generation) != 64 or any(c not in "0123456789abcdef" for c in generation):
        raise ValueError("Immutable publication receipt missing")
    prefix = f"board/generations/{generation}/"
    if publication.get("manifest_key") != prefix + "manifest.json" or publication.get("manifest_sha256") != generation:
        raise ValueError("Manifest pointer mismatch")
    raw = getter(prefix + "manifest.json")
    if raw is None or digest(raw) != generation:
        raise ValueError("Manifest checksum mismatch")
    manifest = json.loads(raw)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("run_id") != meta.get("run_id")
        or manifest.get("git_sha") != meta.get("git_sha")
    ):
        raise ValueError("Manifest identity mismatch")
    objects = manifest.get("objects")
    if not isinstance(objects, dict) or not {"meta.json", "games_nfl.json", "games_cfb.json"} <= objects.keys():
        raise ValueError("Required publication objects missing")
    verified = {}
    for name, receipt in objects.items():
        if name not in PUBLIC_NAMES:
            raise ValueError("Unexpected publication object")
        body = getter(prefix + name)
        if body is None or len(body) != receipt.get("bytes") or digest(body) != receipt.get("sha256"):
            raise ValueError(f"Publication object checksum mismatch: {name}")
        if name in ('meta.json', 'games_nfl.json', 'games_cfb.json'):
            verified[name] = json.loads(body)
    canonical = verified['meta.json']
    if canonical.get('run_id') != manifest['run_id'] or canonical.get('git_sha') != manifest.get('git_sha'):
        raise ValueError('Canonical publication identity mismatch')
    for sport in ('nfl', 'cfb'):
        payload = verified[f'games_{sport}.json']
        rows = payload if isinstance(payload, list) else payload.get('games')
        if (not isinstance(rows, list) or (isinstance(payload, dict) and payload.get('meta', {}).get('run_id') != canonical['run_id'])
                or any(not isinstance(row, dict) or row.get('run_id') != canonical['run_id'] for row in rows)
                or canonical.get('sport_counts', {}).get(sport) != len(rows)):
            raise ValueError(f'{sport} verified publication identity/count mismatch')
    return manifest
