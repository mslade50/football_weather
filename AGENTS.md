# Delivery preference

For finished implementation work in this repository, open a focused pull request from a
branch (no direct commits to main), merge it yourself when the gate in
`docs/REVIEW_POLICY.md` passes, and deploy through the existing GitHub Actions workflow by
default, unless the user explicitly requests local-only or draft work. Verify CI, deployment, and
the relevant live behavior before reporting completion. Refresh board data when
needed to activate the change. Preserve unrelated changes and all existing approval
requirements for destructive or financially consequential actions.

## Review

Read and follow `docs/REVIEW_POLICY.md` before opening a pull request.
Run `python -m pytest tests -q` and `ruff check .`, paste the real output
into the PR, name the risk tier, and write the rollback steps in plain
words. Never print or commit secrets.
