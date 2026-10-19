# Review policy

A coding agent writes the change. A different agent, with fresh context and no
memory of the writing session, reviews it. People spend attention by risk:
risky changes get a full read, small ones get a light check. The repo owner
does not review the code himself. A bad change should be cheap to undo.

## Proof before asking for review

From the repo root, run the tests and the linter before you open a pull
request or ask for review:

```
python -m pytest tests -q
ruff check .
```

Paste the real output into the PR description, under Proof. "Tests passed"
with no output is not proof.

If the change touches the board UI or Telegram alert text, also attach
something real: the sample alert text, a screenshot of the board, or a JSON
sample. A description of what the screen would look like is not evidence.

## Risk tiers

Call the change **HIGH** when any of these are true. HIGH needs a full
independent review:

- it can send Telegram alerts
- it touches pricing, edge, or fair-price logic
- it publishes to R2 or the Worker
- it changes workflows or secrets

Call it **LOW** only for display, copy, or docs tweaks. LOW gets a lighter
check: the diff matches the description, and the proof is actually there.

If you are unsure, call it HIGH.

## Undo

Where practical, ship a risky change behind an environment variable or a flag
so it can be turned off without hunting through code. Every PR states the
rollback in plain words: what to revert, which flag to flip, or what to
redeploy. "Revert if needed" is not a rollback step.

## Secrets

Never print or commit secrets. That includes logs, PR text, and sample
output. Scrub tokens, chat ids, and API keys before you paste proof.

## What CI checks

`.github/workflows/review-check.yml` fails the check when the Proof or
Rollback section is missing, empty, or still placeholder text, and it runs
the same pytest and ruff commands above. It uses read-only permissions and
no repo secrets. The existing `ci` workflow still runs the worker tests and
the bundle dry-run. The PR template (`.github/PULL_REQUEST_TEMPLATE.md`)
has the three section headings: Risk tier, Proof, and Rollback steps.

Dependabot and Renovate do not fill this template, so the body check does not
fail their PRs. Every other author, including other bots and coding agents,
must fill it in.
