"""Content-addressed board generation; one meta pointer commits verified bytes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

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


def encoded(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def prepare_generation(files: dict[str, Path], target: Path) -> tuple[dict[str, Path], Path]:
    """Seal local payloads without mutating sources. Same bytes yield same keys."""
    paths = {
        Path(key).name: Path(path)
        for key, path in files.items()
        if key.startswith("board/") and Path(key).name in PUBLIC_NAMES
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
