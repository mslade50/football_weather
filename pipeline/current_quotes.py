"""Expire current quotes without changing their clocks or historical openers."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


def expired(value, now: datetime) -> bool:
    if value is None:
        return False
    try:
        expiry = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return expiry.tzinfo is None or now > expiry
    except (ValueError, TypeError, AttributeError):
        return True


def expire_card(card: dict, now: datetime) -> dict:
    card = deepcopy(card)
    removed = []
    for book, markets in card.get("odds", {}).items():
        for market, quote in markets.items():
            if not expired(quote.get("expires_at"), now):
                continue
            removed.append(f"{book}/{market}")
            markets[market] = {k: v for k, v in quote.items()
                               if k.startswith("open") or k in ("expires_at", "updated_at", "source_updated_at")}
            markets[market]["expired"] = True
    if removed:
        card["expired_markets"] = removed
        # Other live books can still be inspected. Derived comparisons require a
        # new calculation; never present a consensus that used an expired quote.
        card["consensus"] = {**card.get("consensus", {}), **dict.fromkeys(
            ("spread_now", "total_now", "move_s", "move_t", "spread_src", "ref_book")), "n_books": 0, "thin": True}
        card["fair"] = {k: [] if k == "edges" else None for k in card.get("fair", {})}
        card["total_prices"] = None
    return card


def expire_meta(meta: dict, now: datetime) -> dict:
    meta = deepcopy(meta)
    if "quote_expiries" not in meta:
        return meta
    live = []
    removed = Counter()
    for group in meta.get("quote_expiries", []):
        if expired(group["expires_at"], now):
            removed[(group["book"], group["sport"], group["market"])] += group["count"]
        else:
            live.append(group)
    meta["quote_expiries"] = live
    for (book, sport, market), count in removed.items():
        counts = meta.get("counts", {}).get(book, {})
        for key in (sport, f"{sport}.{market}"):
            if key in counts:
                counts[key] = max(0, counts[key] - count)
        status = meta.get("books", {}).get(book)
        if status is not None:
            status["count"] = max(0, (status.get("count") or 0) - count)
            status["status"] = "amber" if status["count"] else "red"
            status["reason"] = "Expired quotes excluded from current prices"
    if removed:
        meta.setdefault("degradations", []).append({"component": "odds.expiry", "severity": "warn",
            "reason": f"{sum(removed.values())} expired quotes excluded; source timestamps unchanged",
            "run_id": meta.get("run_id"), "ts": now.isoformat()})
    return meta


def publication_guard(directory: Path, now: datetime) -> None:
    """Recheck after raw uploads, immediately before uploading current board JSON."""
    meta = expire_meta(json.loads((directory / "meta.json").read_text(encoding="utf-8")), now)
    cards = []
    for sport in ("nfl", "cfb"):
        path = directory / f"games_{sport}.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["games"] = [expire_card(c, now) for c in payload["games"]]
        payload["meta"] = {**payload.get("meta", {}), "degradations": meta.get("degradations", [])}
        cards.extend(payload["games"])
        path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    from pipeline.outputs.json_out import table_row
    guard_legacy(directory.parent, cards)
    board = directory / "board.json"
    if board.exists():
        payload = json.loads(board.read_text(encoding="utf-8"))
        payload["rows"] = [table_row(c) for c in cards]
        payload["meta"] = {**payload.get("meta", {}), "degradations": meta.get("degradations", [])}
        board.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    status = directory / "status.json"
    if status.exists():
        payload = json.loads(status.read_text(encoding="utf-8"))
        for key in ("degradations", "books", "counts"):
            payload[key] = meta.get(key, {})
        payload["meta"] = {**payload.get("meta", {}), "degradations": meta.get("degradations", [])}
        payload["quote_expiries"] = meta.get("quote_expiries", [])
        status.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
        state_status = directory.parent / "state/status.json"
        if state_status.exists():
            state_status.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    (directory / "meta.json").write_text(json.dumps(meta, allow_nan=False), encoding="utf-8")


def guard_legacy(directory: Path, cards: list[dict]) -> None:
    """Clear current comparisons in legacy snapshots; opening/history fields stay."""
    import csv

    from pipeline.outputs.legacy import cfb_game_label, nfl_game_label
    from utils.timeutil import date_label
    labels = {(c["sport"], (nfl_game_label if c["sport"] == "nfl" else cfb_game_label)(c["away"]["name"], c["home"]["name"]),
               date_label(datetime.fromisoformat(c["kickoff_local"]))) for c in cards if c.get("expired_markets")}
    fields = {"Spread_now", "Odds_now", "Total_now", "Under_now", "FD_now", "Odds_n", "Current", "Spread",
              "Total_proj", "Move_t", "Move_s", "My_total", "Edge", "My_spread", "Edge_s"}
    path = directory / "nfl_weather.csv"
    if path.exists() and any(s == "nfl" for s, _, _ in labels):
        with path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            headers, rows = reader.fieldnames, list(reader)
        for row in rows:
            if ("nfl", row.get("Game"), row.get("Date")) in labels:
                for key in fields & row.keys():
                    row[key] = ""
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, headers)
            writer.writeheader()
            writer.writerows(rows)
    path = directory / "cfb_weather.xlsx"
    if path.exists() and any(s == "cfb" for s, _, _ in labels):
        from openpyxl import load_workbook
        workbook = load_workbook(path)
        for sheet in workbook:
            headers = {cell.value: cell.column for cell in sheet[1]}
            if "Game" not in headers or "Date" not in headers:
                continue
            for row in sheet.iter_rows(min_row=2):
                if ("cfb", row[headers["Game"] - 1].value, row[headers["Date"] - 1].value) in labels:
                    for key in fields & headers.keys():
                        row[headers[key] - 1].value = None
        workbook.save(path)
        workbook.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-dir", type=Path, required=True)
    args = parser.parse_args()
    publication_guard(args.board_dir, datetime.now(timezone.utc))
