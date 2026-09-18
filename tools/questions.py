"""Typed-question builders for the identity shadow harness.

Two adjudications, both re-runs of a decision production already made:

  * venues — which stadium in ``data/stadiums.csv`` a CFBD venue string names
             (``StadiumBook.find_stadium`` / ``StadiumBook.resolve``).
  * teams  — which program a sportsbook's team string names
             (``TeamResolver`` behind ``pipeline.odds.teams.normalize_team``).

Both are framed as a Choice over a pre-filtered candidate list plus
``none_of_these``, with one companion Noul per candidate. The Choice makes the
candidates compete (at most one can be right, so a better fit visibly beats a
worse one); the Noul is a code-side veto the report applies after the fact.

Instructions stay concrete and situation-shaped: the model sees the same
candidate rows the matcher scored, never a summary of them.
"""

from __future__ import annotations

from typing import Any

NONE_OF_THESE = "none_of_these"
COMPAT_PREFIX = "compat_"

VENUE_CHOICE_KEY = "venue"
TEAM_CHOICE_KEY = "team"


def _sdk() -> tuple[Any, Any]:
    """``(Choice, Noul)``, imported lazily so ``tools.identity_shadow`` (harvest and
    report) stays importable without the TypeSafe SDK installed — CI has pytest and
    ruff only."""
    from typesafe_sdk import Choice, Noul
    return Choice, Noul


def candidate_key(index: int) -> str:
    """Stable, content-free criteria label: a long or punctuated venue name can
    never break the label."""
    return f"c{index}"


def compat_key(index: int) -> str:
    return f"{COMPAT_PREFIX}{candidate_key(index)}"


# ---- venues ----------------------------------------------------------------------

_VENUE_INSTRUCTIONS = (
    "A college-football schedule feed published `venue` as the stadium a game was "
    "played at. Pick the stadium in the candidate list that is the SAME PHYSICAL "
    "VENUE.\n"
    "Judge the candidates against each other, not one at a time. At most one can be "
    "that stadium, so a candidate that fits better than the rest wins and the rest "
    "lose — never pick a candidate merely because it is not impossible.\n"
    "A candidate IS that stadium when the two names are the same place written "
    "differently:\n"
    "  - a sponsor rename, e.g. \"High Point Solutions Stadium\" and \"SHI Stadium\" "
    "(Piscataway, Rutgers), \"Sports Authority Field at Mile High\" and \"Empower "
    "Field at Mile High\" (Denver);\n"
    "  - a former or later name of the same building, e.g. \"Paul Brown Stadium\" and "
    "\"Paycor Stadium\" (Cincinnati);\n"
    "  - a naming-rights-free short form, a field name kept or dropped, e.g. \"Sonny "
    "Lubick Field at Hughes Stadium\" and \"Hughes Stadium\";\n"
    "  - a spelling, punctuation or abbreviation difference, e.g. \"Bobby Dodd "
    "Stadium\" and \"Bobby Dodd Stadium at Historic Grant Field\".\n"
    "A candidate is NOT that stadium when it is a DIFFERENT building, even when the "
    "same team plays there. A stadium that was demolished and replaced by a new "
    "stadium elsewhere in the city is not the new stadium: they are two venues. Two "
    "stadiums on one campus are two venues. A stadium rebuilt on its own site, "
    "keeping the site, is the same venue.\n"
    "The names may also be IDENTICAL. That is the easy case, not a trick: when a "
    "candidate's name is the same string as `venue`, it is that stadium unless another "
    "candidate carries the same name in a different city and the location says "
    "otherwise.\n"
    "Evidence in the state: `city` and `state` when the feed carried them, the "
    "`home_team` the feed listed for games at this venue, the seasons the name was "
    "seen, and for each candidate its city, state, the programs whose home stadium it "
    "is, and the string-similarity `score` the production matcher computed. "
    "home_team is the SCHEDULE's home side, not necessarily the venue's tenant: many "
    "of these rows are bowl games, kickoff classics and other neutral sites, so a "
    "candidate in a different city from the home team is perfectly normal. But when a "
    "candidate IS the home team's own stadium and its name differs from `venue` only "
    "by naming rights — one carries a sponsor or a donor and the other does not, or "
    "they carry two different sponsors — that combination is STRONG evidence for a "
    "rename, not weak: a program's home ground changing name between seasons is the "
    "single commonest reason a venue string stops resolving.\n"
    "A high `score` is not an answer. \"Memorial Stadium\", \"War Memorial Stadium\" "
    "and \"Memorial Stadium (KS)\" score near-identically and are different places in "
    "different states; the city and state decide, not the score.\n"
    f"Choose {NONE_OF_THESE!r} when `venue` is a venue not in the list — a stadium "
    "that has been demolished with no successor in the list, a professional baseball "
    "park, a soccer-specific ground, a motor speedway or an overseas stadium used "
    "once for a neutral-site game, or any venue the list simply does not track. "
    "Choosing it is the right answer for a real venue the list is missing; do not "
    "stretch to the nearest-looking name."
)


def venue_criteria(candidates: list[dict]) -> dict[str, str]:
    criteria: dict[str, str] = {}
    for index, cand in enumerate(candidates or []):
        where = ", ".join(p for p in [cand.get("city"), cand.get("state")] if p) or "location unknown"
        parts = [f"{cand.get('name')} — {where}"]
        homes = [h for h in (cand.get("home_teams") or []) if h]
        if homes:
            parts.append("home of " + ", ".join(homes[:3]))
        aliases = [a for a in (cand.get("aliases") or []) if a]
        if aliases:
            parts.append("also known as " + ", ".join(aliases[:3]))
        criteria[candidate_key(index)] = " — ".join(parts)
    criteria[NONE_OF_THESE] = ("a venue not in the list — demolished, renamed beyond the "
                               "list, or a neutral site not tracked")
    return criteria


def venue_state(record: dict) -> dict:
    return {
        "venue": record.get("venue_name", ""),
        "venue_id": record.get("venue_id", ""),
        "city": record.get("city"),
        "state": record.get("state"),
        "home_team": record.get("home_team"),
        "home_teams_seen": record.get("home_teams") or [],
        "seasons": record.get("seasons") or [],
        "games_in_corpus": record.get("seen_count", 0),
        "candidates": [
            {"key": candidate_key(index),
             "name": cand.get("name"),
             "city": cand.get("city"),
             "state": cand.get("state"),
             "home_teams": cand.get("home_teams") or [],
             "aliases": cand.get("aliases") or [],
             "score": cand.get("score")}
            for index, cand in enumerate(record.get("candidates") or [])
        ],
    }


def venue_questions(record: dict) -> dict:
    Choice, Noul = _sdk()
    candidates = record.get("candidates") or []
    raw = record.get("venue_name", "")
    where = ", ".join(p for p in [record.get("city"), record.get("state")] if p)
    questions: dict[str, object] = {
        VENUE_CHOICE_KEY: Choice(instructions=_VENUE_INSTRUCTIONS,
                                 criteria=venue_criteria(candidates)),
    }
    for index, cand in enumerate(candidates):
        other = cand.get("name") or ""
        cand_where = ", ".join(p for p in [cand.get("city"), cand.get("state")] if p)
        questions[compat_key(index)] = Noul(instructions=(
            f"Do {raw!r} and {other!r} ({cand_where or 'location unknown'}) name one and "
            "the same physical stadium, given the city/state "
            f"({where or 'not given'}) and the home team "
            f"({record.get('home_team') or 'not given'})? Answer yes when the two "
            "strings are the same building — whether they are written IDENTICALLY, or "
            "one is a former name, a sponsor rename, a field or donor name kept or "
            "dropped, or a spelling variant. Two identical names are a yes unless the "
            "locations put them in different places. Answer no when they are two "
            "different buildings, including a demolished stadium and the new stadium "
            "that replaced it on another site."))
    return questions


# ---- teams -----------------------------------------------------------------------

_TEAM_INSTRUCTIONS = (
    "A sportsbook (or a schedule feed) published `raw` as one side of a football "
    "game. Pick the program in the candidate list that `raw` names.\n"
    "Judge the candidates against each other. At most one can be right, so a "
    "candidate that fits better than the rest wins and the rest lose.\n"
    "A candidate IS that program when `raw` is it written differently:\n"
    "  - the mascot added or dropped: \"UConn Huskies\" is Connecticut, \"Warhawks\" "
    "belongs to Louisiana Monroe;\n"
    "  - an abbreviation or initialism: \"ULM\", \"SJSU\", \"UTRGV\", \"UWF\", or an "
    "NFL book code such as \"WSH\", \"JAC\", \"LAR\";\n"
    "  - an older or longer form of the school name: \"Southern Mississippi\" is the "
    "school now branded Southern Miss; \"Saint Francis\" is the school listed as "
    "St Francis;\n"
    "  - a \"St\" / \"St.\" / \"State\" spelling, a \"Univ\"/\"University\" kept or "
    "dropped, a parenthesized qualifier unwrapped: \"Miami (FL)\", \"Miami Florida\";\n"
    "  - a city, nickname or short form a book uses for a professional club.\n"
    "Direction and type words must AGREE. East, West, North, South, Eastern, Western, "
    "Northern, Southern, Central, State, Tech and A&M are part of the identity, not "
    "decoration: \"Southern Illinois\" is not \"Illinois\", \"East Texas A&M\" is not "
    "\"West Texas A&M\", \"Miami (OH)\" is not \"Miami (FL)\". A candidate that drops "
    "or adds one of these words is a different school.\n"
    "Level matters. `classification` is nfl, fbs, fcs, ii or iii. Books price top-"
    "level games far more often than lower-division ones, so when `raw` is a clean "
    "name that several levels share, the top-level program (nfl / fbs) is the "
    "likelier reading — but only when nothing in `raw` says otherwise; an explicit "
    "lower-division school name still wins.\n"
    "`score` is the production matcher's 0-100 string similarity on normalized keys. "
    "It is evidence, not an answer: a three-letter abbreviation scores badly against "
    "every full school name, and two unrelated schools can score in the 90s.\n"
    f"Choose {NONE_OF_THESE!r} when `raw` is an abbreviation or a bare city that "
    "several of the listed programs share equally and nothing in the state separates "
    "them — no opponent, no conference, no week — so the right answer cannot be "
    "chosen without that context; or when the program `raw` names is not in the list "
    "at all. Do not pick the top-level candidate just to avoid answering "
    f"{NONE_OF_THESE!r}."
)


def team_choice_key(candidate: dict) -> str:
    return str(candidate.get("team_id") or "")


def team_criteria(candidates: list[dict]) -> dict[str, str]:
    criteria: dict[str, str] = {}
    for index, cand in enumerate(candidates or []):
        key = team_choice_key(cand) or candidate_key(index)
        if key in criteria:
            key = f"{key}#{index}"
        bits = [str(cand.get("name") or key)]
        level = cand.get("classification")
        if level:
            bits.append(str(level))
        conf = cand.get("conference")
        if conf:
            bits.append(str(conf))
        aliases = [a for a in (cand.get("aliases") or []) if a]
        text = " — ".join(bits)
        if aliases:
            text += " — also written " + ", ".join(aliases[:3])
        criteria[key] = text
    criteria[NONE_OF_THESE] = ("none of the listed programs, or an abbreviation several "
                               "of them share that cannot be settled without the opponent")
    return criteria


def team_state(record: dict) -> dict:
    return {
        "raw": record.get("raw", ""),
        "sport": record.get("sport", ""),
        "books": record.get("books") or [],
        "candidates": [
            {"key": team_choice_key(cand) or candidate_key(index),
             "name": cand.get("name"),
             "classification": cand.get("classification"),
             "conference": cand.get("conference"),
             "aliases": cand.get("aliases") or [],
             "score": cand.get("score")}
            for index, cand in enumerate(record.get("candidates") or [])
        ],
    }


def team_questions(record: dict) -> dict:
    Choice, Noul = _sdk()
    candidates = record.get("candidates") or []
    raw = record.get("raw", "")
    sport = "NFL" if record.get("sport") == "nfl" else "college football"
    questions: dict[str, object] = {
        TEAM_CHOICE_KEY: Choice(instructions=_TEAM_INSTRUCTIONS,
                                criteria=team_criteria(candidates)),
    }
    for index, cand in enumerate(candidates):
        other = cand.get("name") or cand.get("team_id") or ""
        questions[compat_key(index)] = Noul(instructions=(
            f"Do {other!r} and {raw!r} name the same {sport} program? Answer yes when a "
            "book could write the one as the other — written IDENTICALLY, the mascot "
            "added or dropped, an abbreviation or initialism, an older or longer form "
            "of the school name, a St/State spelling, a qualifier wrapped or unwrapped, "
            "or a typo. Two identical strings are a yes. Answer no when a direction or "
            "type word (East, West, North, South, Central, State, Tech, A&M) differs, "
            "or when they are two different schools that merely share a word."))
    return questions


__all__ = [
    "COMPAT_PREFIX",
    "NONE_OF_THESE",
    "TEAM_CHOICE_KEY",
    "VENUE_CHOICE_KEY",
    "candidate_key",
    "compat_key",
    "team_choice_key",
    "team_criteria",
    "team_questions",
    "team_state",
    "venue_criteria",
    "venue_questions",
    "venue_state",
]
