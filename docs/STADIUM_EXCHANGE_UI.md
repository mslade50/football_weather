# Stadium and exchange display changes

The Exchange under column requires a fresh, verified principal allocation before
showing a $500 stake. The former fallback said “Liquidity unverified” for missing
quotes, expired previews, stale quotes, changed totals and invalid evidence alike.
The table now distinguishes these states and opens the existing details check.
Fresh reference prices remain explicitly unverified. No automatic quote/depth
request, refresh or order is added. A previously successful allocation must still
match the currently selected exact total, and started games cannot qualify.

The last externally verified pipeline publication supplied for this task was
38486174252, October 13 at 23:50 UTC, revision 4857589. Those acquisition clocks
cannot qualify as fresh the following day. This is separate from the confirmed UI
fallback defect. Current served quotes and resident-collector health were not
inspected: access to the served dashboard was denied and that restriction remains
in force. A deployment receipt does not establish live quote freshness.

The stadium cell reuses `compassSvg` from the drawer, including weak-wind sectors,
field orientation and the arrow from the reported wind bearing. Unknown bearings
remain unknown. Exact stadium coordinates retain their supplied precision. The
table keeps Eastern kickoff followed by stadium-local time when its offset differs.

Book-total cells display the current under total and price on two short lines.
Baseline, movement, fair comparison and source clocks remain in hover/details.
The touch-accessible odds table includes visible baseline observation, quote/source
update and expiry clocks. Buttons open the relevant details section; no hover is
required on mobile.

## Frozen stadium research input

`data/raw/nfl_stadium_signal_history.json` is an immutable, aggregate-only research
reference input. It is not generated current board data. Its original SHA-256 is
`99053e1daddcf3138685534ddbea31a55f518cd469a320efe1c4db377a958950`.
No per-game totals, scores or source odds are included. The authenticated GET-only
`/api/stadium-history` endpoint serves this reference without R2/D1 reads or writes.

The CFB reference, `data/raw/cfb_stadium_signal_history.json`, preserves the audited
dashboard handoff bytes with SHA-256
`d74cf5f7e6b3e1c5dd48f87da0015b43a16175c0a6a1e727dacdc36e90150334`.
All 180 known study venues are retained, including 17 without eligible weather
history. Full 2021–2025 / recent 2024–2025 cohorts reconcile to 102 / 37 games;
opening W–L–P is 60–42–0 / 20–17–0 and closing W–L–P is 56–46–0 / 17–20–0.
These cohorts overlap and are retrospective. The original same-provider-subset
median totals include synthetic quarter-point thresholds; they are not actual
offered lines or Asian split settlement. Historical prices were imputed at −110.
The compact UI shows W–L–P only and identifies synthetic totals. Every CFB full
venue sample has at most four qualifying games.

CFB registry mappings use the handoff's audited unique physical-venue slug, plus
matching source/resolved venue identity. Unresolved aliases and multiple IDs at a
slug are not pooled. Geography-conflicted IDs 3699, 3713 and 4258 stay withheld;
their original counts remain in the source reconciliation, not live venue records.
The study's cached postal/airport weather locations are not silently corrected or
substituted. Period-to-coverage fields are explicit for both leagues.

The NFL source contains 2,094 eligible paired games, 2014–2025, at 29 covered
physical venues. Full/recent moderate-wind counts reconcile to 404/143; strong-wind
counts reconcile to 120/40. Full and recent W–L–P are paired by source opener and
close. NFL venue crosswalks come from the existing stadium registry. Both the
canonical stadium ID and schedule source/resolved venue IDs must agree; team-name
or stadium-name fallback is prohibited. Unmapped venues remain unavailable.
The old Buffalo BUF00 building is capped at the 2025 season, so its record is
not inherited by the new 2026 stadium even if a provider reuses an ID/name.
The physical replacement is documented on the [Bills stadium page](https://www.buffalobills.com/stadium/).

The historical weather is the frozen hourly IEM airport-observation cohort, not a
new forecast or field measurement. Outdoor and recorded-open retractable-roof
games retain the source cohort's eligibility. The source bookmaker varies by era;
opener posting times are unaudited and 2025 coverage is incomplete. Details expose
the exact rule, period, underlying coverage, paired basis and source limitations.
N<20 receives a descriptive small-sample notice. Missing coverage is unavailable;
a covered venue with no qualifying games is explicitly distinct. No venue ranking
or prospective edge is inferred from these retrospective records.

## Local checks

Run `node --test site/worker/test/*.test.mjs`, `python -m pytest tests -q`, and
`python -m ruff check .`. `python scripts/check_dashboard_ui.py` generates ignored
desktop/mobile screenshots and a validation receipt under `tmp/ui-evidence/`.
That fixture disables production boot, blocks all connections, uses synthetic
upcoming cards and the audited aggregate reference, and sends no depth requests.
Both NFL and CFB screenshots are produced; mobile uses touch input and suppresses
hover-only panels while retaining the full evidence in tap-accessible details.
It checks 390px document overflow, kickoff labels, source-clock details, drawer
navigation and keyboard focus. Screenshot fixture cards do not represent games or
available trading prices.
