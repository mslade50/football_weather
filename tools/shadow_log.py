"""Append-only JSONL sink for the identity shadow harness.

Deliberately import-light: ``pipeline/odds/teams.py`` and
``pipeline/stadiums/loader.py`` import this lazily, inside an
``IDENTITY_SHADOW_LOG`` guard, so production code paths gain no import cost at
module load and no behaviour change when the flag is unset.

Every public function here is fail-soft — an IO error must never abort a run.

The candidate scorers (``team_scores`` / ``venue_scores``) are shared with the
offline harvest in ``tools/identity_shadow.py``; they mirror the production
matchers (``TeamResolver.fuzzy`` and ``StadiumBook.find_stadium``'s slug index)
so the shadow record shows what the matcher actually saw.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SHADOW_DIR = Path("output") / "identity_shadow"

_ALIAS_CACHE: dict[tuple[str, str], dict[str, list[str]]] = {}
_CLASS_CACHE: dict[tuple[str, str], dict[str, str]] = {}


def shadow_dir() -> Path:
    """Output root; ``IDENTITY_SHADOW_DIR`` overrides it (used by the tests)."""
    override = os.environ.get("IDENTITY_SHADOW_DIR")
    return Path(override) if override else DEFAULT_SHADOW_DIR


def append_jsonl(path: Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning(f"[shadow] skipping malformed JSONL line in {path}")
    return out


def stable_key(kind: str, parts: Sequence[Any]) -> str:
    blob = json.dumps([kind, *[("" if p is None else str(p)) for p in parts]],
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# ---- team candidates -------------------------------------------------------------

def _team_tables(sport: str, data_dir: Any) -> tuple[dict[str, list[str]], dict[str, str]]:
    """(aliases, classification) for one sport, cached per (sport, data_dir)."""
    from pipeline.odds.teams import load_aliases, load_classification

    key = (str(sport), str(data_dir))
    if key not in _ALIAS_CACHE:
        _ALIAS_CACHE[key] = {k: list(v) for k, v in load_aliases(sport, Path(data_dir)).items()}
        _CLASS_CACHE[key] = load_classification(sport, Path(data_dir))
    return _ALIAS_CACHE[key], _CLASS_CACHE[key]


def team_scores(resolver: Any, raw: str) -> list[tuple[str, float]]:
    """``[(team_id, best 0-100 ratio)]``, best first — a read-only replay of
    ``TeamResolver.fuzzy`` (pipeline/odds/teams.py:228), qualifier filter included,
    that keeps the per-team scores ``fuzzy`` throws away."""
    from pipeline.odds.teams import _ratio, normalize_alias, qualifiers, variants

    best: dict[str, float] = {}
    raw_q = qualifiers(raw)
    for variant in variants(raw):
        key = normalize_alias(variant)
        if not key:
            continue
        for cand in resolver._keys:
            cand_q = resolver.quals.get(cand, frozenset())
            if raw_q and cand_q and cand_q != raw_q:
                continue      # 'East Texas A&M' must never fuzz to 'West Texas A&M'
            score = _ratio(key, cand)
            for team_id in resolver._teams_for(cand):
                if score > best.get(team_id, 0.0):
                    best[team_id] = score
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))


def team_decision(resolver: Any, raw: str, resolved_id: str | None) -> str:
    """Which branch of ``normalize_team`` produced ``resolved_id``."""
    if resolved_id is not None and resolver.exact(raw) == resolved_id:
        return "exact"
    if resolved_id is not None:
        return "fuzzy_accept"
    return "fuzzy_reject" if team_scores(resolver, raw) else "unresolved"


def team_top_candidates(
    resolver: Any,
    raw: str,
    limit: int = 5,
    max_aliases: int = 3,
    data_dir: Any = None,
    scores: list[tuple[str, float]] | None = None,
) -> list[dict]:
    """Top ``limit`` teams by ``team_scores``; top-level (nfl / fbs) programs win ties."""
    from pipeline.odds.teams import _TOP_LEVEL, DATA_DIR

    aliases, classification = _team_tables(resolver.sport, data_dir or DATA_DIR)
    ranked = scores if scores is not None else team_scores(resolver, raw)
    ranked = sorted(
        ranked,
        key=lambda kv: (-kv[1], 0 if classification.get(kv[0]) in _TOP_LEVEL else 1, kv[0]),
    )
    out: list[dict] = []
    for team_id, score in ranked[:limit]:
        out.append({
            "team_id": team_id,
            "score": round(float(score), 3),
            "classification": classification.get(team_id) or None,
            "aliases": aliases.get(team_id, [])[:max_aliases],
        })
    return out


# ---- venue candidates ------------------------------------------------------------

def venue_scores(book: Any, name: str) -> list[tuple[str, float, str]]:
    """``[(stadium_id, 0-1 ratio, matched name/alias slug)]``, best first, over the
    same slug index ``StadiumBook.find_stadium`` searches (loader.py:168)."""
    from pipeline.stadiums.loader import _fuzzy_score, slug

    target = slug(name or "")
    if not target:
        return []
    best: dict[str, tuple[float, str]] = {}
    for key, stadium_id in book.name_index.items():
        score = _fuzzy_score(target, key)
        cur = best.get(stadium_id)
        if cur is None or score > cur[0]:
            best[stadium_id] = (score, key)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return [(sid, score, key) for sid, (score, key) in ranked]


def venue_top_candidates(book: Any, name: str, limit: int = 5,
                         scores: list[tuple[str, float, str]] | None = None) -> list[dict]:
    ranked = scores if scores is not None else venue_scores(book, name)
    out: list[dict] = []
    for stadium_id, score, matched in ranked[:limit]:
        st = book.stadiums.get(stadium_id)
        out.append({
            "stadium_id": stadium_id,
            "name": getattr(st, "name", None),
            "city": getattr(st, "city", None),
            "state": getattr(st, "state", None),
            "matched_slug": matched,
            "score": round(float(score), 3),
        })
    return out


# ---- emit-only production hooks --------------------------------------------------

def log_team_resolve(sport: str, raw: str, book: str | None, resolved_id: str | None,
                     resolver: Any, data_dir: Any = None) -> None:
    """Emit-only hook for ``pipeline.odds.teams.normalize_team``.

    Exact alias hits return early: they are not the decisions under review and
    they are the overwhelming majority of the volume.
    """
    try:
        from pipeline.odds.teams import FUZZY_MARGIN, FUZZY_MIN

        decision = team_decision(resolver, raw, resolved_id)
        if decision == "exact":
            return
        scores = team_scores(resolver, raw)
        append_jsonl(shadow_dir() / "live_teams_candidates.jsonl", {
            "kind": "team",
            "sport": sport,
            "book": book or "",
            "raw": raw,
            "decision": decision,
            "resolved_id": resolved_id,
            "top5": team_top_candidates(resolver, raw, limit=5, data_dir=data_dir,
                                        scores=scores),
            "FUZZY_MIN": FUZZY_MIN,
            "FUZZY_MARGIN": FUZZY_MARGIN,
        })
    except Exception as exc:  # never let shadow IO touch a production run
        logger.debug(f"[shadow] team log skipped: {exc}")


def log_venue_unresolved(book: Any, game: Any, source_chain: Sequence[str]) -> None:
    """Emit-only hook for ``StadiumBook.resolve``'s no-stadium fallback path."""
    try:
        name = str(getattr(game, "stadium_id", "") or "")
        append_jsonl(shadow_dir() / "live_venues_candidates.jsonl", {
            "kind": "venue",
            "game_id": getattr(game, "game_id", None),
            "sport": getattr(game, "sport", None),
            "stadium_id": getattr(game, "stadium_id", None),
            "home_team": getattr(game, "home_id", None),
            "away_team": getattr(game, "away_id", None),
            "neutral": bool(getattr(game, "neutral", False)),
            "source_chain": list(source_chain),
            "top5": venue_top_candidates(book, name, limit=5),
        })
    except Exception as exc:
        logger.debug(f"[shadow] venue log skipped: {exc}")


__all__ = [
    "DEFAULT_SHADOW_DIR",
    "append_jsonl",
    "log_team_resolve",
    "log_venue_unresolved",
    "read_jsonl",
    "shadow_dir",
    "stable_key",
    "team_decision",
    "team_scores",
    "team_top_candidates",
    "venue_scores",
    "venue_top_candidates",
]
