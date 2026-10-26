# Delivery preference

<!-- owner-maintenance-authority:start -->
# Maintenance authority -- owner instruction, October 26, 2026

This rule covers every repository and machine the owner uses. It applies to every
agent: Claude, Codex, their subagents and unattended agents. Where an older
instruction in a repository (for example a research-only boundary, or "no commits,
pushes or deployments without approval") would require the owner's approval for the
work below, this rule takes precedence. Those older limits still apply to research
and strategy work.

## Repair and maintain without asking

When something that already exists is broken, stale or not running, fix it end to
end without asking the owner. Examples:

- a scraper, collector, odds feed or data pull that is down or returning bad data;
- a scheduled task, workflow or job that failed, hung or did not run;
- a crash, a broken path, a dependency or environment that drifted, or machines
  that are out of sync;
- a failed or partial deploy, release or publish.

For that work you may edit code and configuration, rerun jobs, restart services and
scheduled tasks, create branches and worktrees, commit, push, open and merge pull
requests, and redeploy (Workers, releases, scheduled tasks). Use the repository's
normal delivery path and review rules.

A repair restores the intended behaviour. It does not change what the system is
trying to do.

## Ask the owner first: research and strategy

- changing a model: its math, features, parameters, coefficients, training data or
  calibration;
- changing strategy or goals: what a repository produces, which markets, events or
  instruments it covers, signals and thresholds, bet or trade sizing, staking and
  risk limits;
- new systems and structural redesigns, such as a new pipeline or replacing a
  component, as opposed to repairing the existing one;
- any change whose right answer depends on judgment about edge, data trade-offs or
  intuition rather than "make it work as intended".

If a repair can only be done through one of these, make the smallest safe holding
move instead (pause the affected output, or fall back to the last good version) and
leave the owner a short note naming the decision needed. If a repair needs the
owner's login or a new secret, ask for exactly that.

## Always ask immediately before

- `git reset --hard`, force-push or history rewrite on a shared branch;
- deleting existing branches, worktrees, run history or stored data (cloud objects,
  databases, tables, Workers);
- placing, sizing or cancelling a broker order, bet or trade, or changing a live
  stop level.

Deployment windows and safeguards written for specific systems (for example the
07:30-11:30 New York blackout for New_Seasonals and trading_ibkr) still apply.

## How to repair

- Fix the root cause, not only the symptom. When practical, add a test or guard so
  the same failure does not return.
- Commit only the files the repair touches. Leave unrelated working-tree changes,
  branches and worktrees alone: no stash, clean or reset.
- Have an independent reviewer check a risky repair (one that publishes, deploys,
  sends alerts or touches money paths) before it ships.
- After deploying, confirm the next run or health check passes. If it does not,
  roll back.
- Never print or commit secrets.
- Record what broke, what changed, where it shipped and how to undo it.
<!-- owner-maintenance-authority:end -->

For finished implementation work in this repository, commit, push to the configured
remote, and deploy through the existing GitHub Actions workflow by default, unless
the user explicitly requests local-only or draft work. Verify CI, deployment, and
the relevant live behavior before reporting completion. Refresh board data when
needed to activate the change. Preserve unrelated changes and all existing approval
requirements for destructive or financially consequential actions.
