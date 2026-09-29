"""Read-only, alert-time snapshots using the same depth adapters as the admin preview."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from utils.timeutil import now_utc, parse_iso

logger = logging.getLogger(__name__)
BRIDGE = Path(__file__).resolve().parents[1] / "scripts" / "alert_liquidity.mjs"


def enrich_liquidity(cards: list[dict]) -> None:
    """Mutate only this run's alert cards. Unknown size never counts toward $500."""
    eligible = [c for c in cards if c.get("execution_markets") and (c.get("total_prices") or {}).get("quotes")]
    if not eligible:
        return
    try:
        proc = subprocess.run(["node", str(BRIDGE)], input=json.dumps(eligible), text=True,
                              encoding="utf-8", capture_output=True, check=True, timeout=60)
        snapshots = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.warning("Alert liquidity unavailable: %s", type(exc).__name__)
        return
    for card in eligible:
        snapshot = snapshots.get(card["game_id"])
        if not isinstance(snapshot, dict):
            continue
        try:
            age = (now_utc() - parse_iso(snapshot["checked_at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= age <= 60:
            continue
        card["alert_liquidity"] = snapshot
        updates = {(q["book"], q["line"], q["side"]): q for q in snapshot["quotes"]}
        prices = dict(card["total_prices"])
        prices["quotes"] = [{**q, **updates.get((q["book"], q["line"], q["side"]), {})} for q in prices["quotes"]]
        card["total_prices"] = prices
        books = dict(card.get("odds") or {})
        for update in snapshot["quotes"]:
            markets = books.get(update["book"]) or {}
            total = markets.get("total") or {}
            if total.get("line") != update["line"]:
                continue
            if update["liquidity_status"] == "verified":
                total = {**total, "under": update["odds"], "updated_at": update["updated_at"]}
            elif update["liquidity_status"] == "empty":
                total = {**total, "under": None}
            else:
                continue
            books[update["book"]] = {**markets, "total": total}
        card["odds"] = books
