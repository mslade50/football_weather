# Backend repair implementation, 2026-10-09

## Venue identity and failed ensemble verification

NFL schedule parsing now resolves an explicit venue name before an upstream stadium ID. A known name conflicting with a known ID is resolved to that named physical stadium, with the original name/ID and resolution method in the game card. A venue different from the designated home team's home ground is neutral for travel calculations. An unregistered explicit name is unresolved: no home-stadium weather substitution is permitted. The PHI–JAX regression covers Tottenham, JAX00, the misleading Home label, London local time, coordinates, and a visible warning.

On failed before/after ensemble metadata checks, previously verified raw members may be retained for the same location and complete requested hour window. They retain their original dataset metadata and per-hour fetch clocks. Retrieval age must be strictly below three hours for near/active games or twelve hours for distant inactive games; known dataset initialization must be at most thirty hours old. Unknown/future initialization, future retrieval timestamps, missing hours, changed parameter signature, and corrupt entries are rejected. No newly retrieved unverified payload is cached. Published status is `retained_members_degraded`, with separate unverified sources and verification errors. Fresh point forecasts keep a separate clock.

This is continuity of previously verified evidence, not a new source verification or a claim about empirical forecast accuracy. Dataset metadata does not establish initialization provenance for every long-range member hour. No v1 impact formula changes.

Validation: full Python suite and `python -m ruff check .` passed locally. No frontend files changed. Production activation and exact-generation validation remain required after integration.
