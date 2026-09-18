# Identity shadow harness

Re-adjudicates two identity decisions with TypeSafe's Jev model **without
changing production behaviour**, and reports where Jev and the existing matcher
disagree.

1. **Venues** — which stadium in `data/stadiums.csv` a CFBD venue string names
   (`StadiumBook.find_stadium` / `StadiumBook.resolve` in
   `pipeline/stadiums/loader.py`).
2. **Teams** — which program a sportsbook's team string names
   (`TeamResolver` behind `pipeline/odds/teams.py::normalize_team`).

Nothing here writes to `data/`, `site/web/data/` or any production path. The two
hooks in production code are emit-only, env-guarded and wrapped in `try/except`;
with `IDENTITY_SHADOW_LOG` unset they are a single `os.environ.get`.

Needs `TYPESAFE_API_KEY` in the environment (or `.env` — `tools/jev_client.py`
calls `load_dotenv()`). The SDK is **not** a repo dependency: `tools/questions.py`
imports it lazily and `run_judge` imports `jev_client` lazily, so harvest, report
and the whole test suite run on a machine that has never installed it.

## Commands

Run from the repo root.

```bash
# 1. Replay the committed corpus into candidate records (offline, no API key).
python -m tools.identity_shadow harvest venues   # [--backtest-dir data/backtest/git]
python -m tools.identity_shadow harvest teams    # [--raw-runs data/raw_runs] [--legacy-db ...]

# 2. Ask Jev about candidates not yet judged.
python -m tools.identity_shadow judge --target venues --only-unresolved   # the 36 misses
python -m tools.identity_shadow judge --target venues --max-hits 20       # a sample of hits
python -m tools.identity_shadow judge --target teams  --max-hits 20
python -m tools.identity_shadow judge --target venues --dry-run --limit 3 # no network

# 3. Write the agreement report.
python -m tools.identity_shadow report --target venues
python -m tools.identity_shadow report --target teams
```

`judge` keys on a stable hash of each candidate, so it is resumable: re-running
it only judges what is new. `--dry-run` writes a verdict with `answers: null`
that echoes the state and question keys; reports count those rows and ignore
them. `--limit N` caps the calls, `--only-unresolved` skips every row the matcher
already resolved, `--max-hits N` caps them instead. Labeled rows are never
counted as hits, so `--max-hits` can neither spend the budget on them nor skip
one.

Both harvests order their records so a `--limit` spends the budget well: venues
run misses first, then `name_hit`, then `id_hit`; teams run labeled rows first,
then unresolved / fuzzy rows, then exact hits with the two sports interleaved.

## The framing: select, don't generate

Each record is one request:

* one `Choice` over a **pre-filtered** candidate list plus `none_of_these`;
* one companion `Noul` per candidate, `compat_c0 … compat_cN`.

The Choice makes the candidates compete — at most one can be right, so a better
fit visibly beats a worse one, which a per-pair score can never see. The Noul is
a **code-side veto** applied afterwards by the report, not advice to the model.

### The accept rule

```
accepted = pick != none_of_these
           and p(pick)   >= ACCEPT_MIN_P       (0.60)
           and compat_<pick> >= ACCEPT_MIN_COMPAT  (0.50)
```

Both constants live in `tools/identity_shadow.py`. The veto earns its keep:
`Qualcomm Stadium -> Snapdragon Stadium` came back at p=0.88 with compat 0.43 and
is therefore **not** promoted — Snapdragon is a new build on the Qualcomm site,
not the same building, and the Choice alone would have promoted it.

## Candidate pre-filters

A Choice is only as good as its list. `data/aliases/cfb.json` holds 661 teams and
`data/stadiums.csv` 696 stadiums, so neither list can be shown whole.

**Venues** (`venue_candidates`, <= 10 rows, filled in this order):

1. the stadium the matcher resolved, so every positive is answerable at all;
2. the home stadium of the two programs the feed listed most often at this venue
   — this is what catches a rename, where string similarity is worthless
   (`High Point Solutions Stadium` scores **0.68** against its own successor
   `SHI Stadium`, far below ten irrelevant "... Stadium" neighbours);
3. same-city then same-state rows, when the feed carried a location;
4. the best string neighbours over the slug index `find_stadium` searches.

The reserved slots come first so a flood of near-matches can never push the real
answer out, and the returned list is then sorted by score, so a candidate's
position never leaks which source put it there.

**Teams** (`team_candidates`, <= 10 rows): `team_scores` replays
`TeamResolver.fuzzy` read-only — including its `qualifiers()` filter, so
`East Texas A&M` is never offered `West Texas A&M` — and keeps the per-team
scores `fuzzy` throws away. Top-level (`nfl` / `fbs`) programs break ties;
aliases are capped at 3 per candidate; the resolved team is always present.

## Labels

**Venues.** `id_hit` and `name_hit` rows are positives: `expected` is the stadium
the matcher resolved, and a disagreement is a labeled miss. The 36 `miss` rows
are the human-review table — `report --target venues` reads an optional
`output/identity_shadow/venues_labels.json`:

```json
{
  "2fc05dc67d7f146c": "shi-stadium",
  "88895c2e7b6a5bd3": "none"
}
```

Keys are the `key` field of the candidate record (also the first column of the
miss table). Values are the correct `stadium_id`, or `"none"` when no row in
`stadiums.csv` is that venue. The report shows the label beside Jev's pick and
counts how many rows are still unlabeled.

**Teams.** Every exact alias hit is a `data/aliases/{sport}.json` positive:
`expected` is the team the alias table already names. `LABELED_TEAMS` in
`tools/identity_shadow.py` adds the rows the corpus cannot label by itself — the
rejections pinned by `tests/test_merge_aliases.py` (`MER`, bare `Miami`,
`Los Angeles`, `New York`, `Zzzz Polytechnic Univ`) and the strings the matcher
measurably fails on (`Southern Mississippi Golden Eagles`, `UConn Huskies`,
`UL Monroe Warhawks`, `Saint Francis`, plus the Miami (FL)/(OH) pair). `"labeled"`
joins a record's `sources`, never its `books`: the state Jev sees carries the
books that really quoted the string and nothing that hints at a label. A
`teams_labels.json` works the same way for rows `LABELED_TEAMS` does not cover.

One label is deliberately a disagreement with production: bare `Miami` is labeled
`none_of_these` because nothing but the opponent separates Miami (FL) from Miami
(OH), while `data/aliases/cfb.json` resolves it to `miami-fl` by convention. Jev
answers `miami-fl` at p=0.96, so it scores as a miss. That is the label's design,
not a model error — read it as "production has a convention here", not as a bug.

## The corpus

| Target | Sources | Distinct records |
| --- | --- | --- |
| venues | `data/backtest/git/cfbd_games_2015..2025.json` | 197 `(venue, venue_id)` pairs: 141 `id_hit`, 20 `name_hit`, **36 `miss`** |
| teams | `data/raw_runs/{cfb,nfl}/<run>/` replayed through `pipeline/odds/parsers/*` + `merge.parse_provisional` (1106 `(sport, book, raw)`), `tests/fixtures/raw/legacy_db/{bol_ncaaf,fd_cfb}.db` (321), CFBD school strings (246) | 811 `(sport, raw)`: 803 exact, 8 fuzzy rejects |

Team records dedupe on `(sport, raw)`, not `(sport, book, raw)` — the same string
judged twice under two books would only spend budget — and each record carries
the `books` and `sources` that produced it.

`data/backtest/` and `data/raw_runs/` are gitignored, so neither harvest runs on
CI. The tests build their own tiny `data/` and CFBD fixtures under `tmp_path`.

## Live hooks

Set `IDENTITY_SHADOW_LOG=1` on a pipeline run to capture the decisions production
is making right now:

| Hook | File | Emits |
| --- | --- | --- |
| `normalize_team`, after the resolve | `pipeline/odds/teams.py` | `output/identity_shadow/live_teams_candidates.jsonl` |
| `StadiumBook.resolve`, on the no-stadium fallback | `pipeline/stadiums/loader.py` | `output/identity_shadow/live_venues_candidates.jsonl` |

The team hook emits only when the decision was **not** an exact alias hit (fuzzy
accept, fuzzy reject, unresolved) — exact hits are not the decisions under review
and they are the overwhelming majority of the volume. It carries
`{sport, book, raw, decision, resolved_id, top5[{team_id, score, aliases<=3}],
FUZZY_MIN, FUZZY_MARGIN}`. The venue hook sits on the branch that already calls
`_degrade("stadiums", …)` and carries `{game_id, stadium_id, home_team,
source_chain, top5 slug neighbours with scores}`.

`IDENTITY_SHADOW_DIR` overrides the output root (used by the tests).

## Output files

All under `output/identity_shadow/` (gitignored):

| File | Contents |
| --- | --- |
| `venues_candidates.jsonl` | one record per distinct `(venue, venue_id)` with the matcher's branch and <= 10 candidates |
| `teams_candidates.jsonl` | one record per distinct `(sport, raw)`, labeled rows first |
| `*_verdicts.jsonl` | one Jev verdict per candidate key, with probabilities, model, request id, tokens and latency |
| `*_agreement.md` | the reports |
| `venues_labels.json` | **hand-written**; see above |
| `run1_*` | the superseded first judging pass, kept for comparison (see below) |
| `live_*_candidates.jsonl` | whatever the two production hooks captured |

## Run log

**Run 1 (76 venue calls)** scored 85.0% agreement on 40 resolved venues. Every
one of the six "disagreements" was the *same* instruction bug, not a model error:
five picked the right stadium at p=0.58-0.93 and were then vetoed by a companion
Noul that scored 0.32-0.45 — because the Noul asked whether the candidate was "a
former name, a sponsor rename or a spelling variant", which excludes the case
where the two names are **identical**. `Ford Field` is not a variant of
`Ford Field`; it is `Ford Field`.

**The revision** (one pass, both question sets) rewrote both companion Nouls to
"do these two name one and the same thing … whether written IDENTICALLY, or …",
and told the venue Choice that a candidate which *is* the home team's own stadium
and differs only by naming rights is **strong** evidence of a rename, not weak
(run 1 answered `none_of_these` at p=0.56 for `Kroger Field` while its own
companion Noul for Commonwealth Stadium sat at 0.87).

**Run 2 (56 venue + 31 team calls)**: venues 100.0% agreement on 20 resolved
venues, zero labeled misses; teams 93.5% labeled accuracy (29/31).

## Promotion criteria

Do not promote any of this into `data/stadiums.csv`, `data/stadiums_overrides.csv`
or `data/aliases/*.json` until all four hold:

1. **>= 95% agreement** between Jev and the matcher on resolved venues
   (`id_hit` / `name_hit`) and on exact team alias hits.
2. **Every Jev pick on a `miss` venue** carries a `human_label` in
   `venues_labels.json`. A pick is a proposal for a `stadiums.csv` alias or a
   `stadiums_overrides.csv` row, never an automatic edit.
3. **Every labeled team rejection** (`MER`, `Los Angeles`, `New York`) still
   answers `none_of_these`, and no promotion crosses `Miami (FL)` / `Miami (OH)`.
4. The four documented matcher failures (`Southern Mississippi Golden Eagles`,
   `UConn Huskies`, `UL Monroe Warhawks`, `Saint Francis`) are promoted as
   **aliases**, not as fuzzy-threshold changes: `FUZZY_MIN` and `FUZZY_MARGIN`
   are what keep `Miami` off `Miami (OH)`, and every one of those four is a
   missing alias, not a threshold that is set too high.

A venue whose Jev pick is a *new building on a different site* (Qualcomm ->
Snapdragon, Hughes -> Canvas, Ladd-Peebles -> Hancock Whitney) must never be
promoted as an alias: the alias would move a historical game's weather to the
wrong coordinates. Those belong in a separate "venue succeeded by" column if they
are ever wanted at all. The accept rule already catches the clearest of them —
Qualcomm is not accepted — but it does not catch all three, which is exactly why
criterion 2 requires a human label.
