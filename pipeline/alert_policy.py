"""Interview policy: scheduled reminders, first notice, explicit bet lifecycle.

Uses existing signal eligibility/tiers. No liquidity or invented probability gate.
No transport is called from this module.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def routine_slot(now):
    local = now.astimezone(ZoneInfo("America/New_York"))
    # Scheduler targets the exact minute; bounded recovery tolerates a late
    # transport/retry without turning the entire hour into a routine slot.
    return f"{local.date()}|{local.hour:02d}" if local.hour in (8, 17) and local.minute < 5 else None


def next_routine_at(now):
    zone = ZoneInfo('America/New_York')
    local = now.astimezone(zone)
    for offset in (0, 1):
        for hour in (8, 17):
            candidate = datetime.combine(local.date() + timedelta(days=offset), time(hour), zone)
            if candidate > local:
                return candidate.astimezone(now.tzinfo)
    raise AssertionError('Next routine slot unavailable')


def routine_available(alerts, cfg, now):
    slot = routine_slot(now)
    if not slot:
        return False
    ledger = alerts.get('routine_delivery') or {}
    prefix = f"{cfg.chat_default}|{slot.split('|')[0]}|"
    return f'{cfg.chat_default}|{slot}' not in ledger and sum(key.startswith(prefix) for key in ledger) < 2


def confirmed_for(confirmations, game_id):
    return [
        row
        for row in (confirmations.get("bets") or {}).values()
        if isinstance(row, dict)
        and row.get("game_id") == game_id
        and row.get("confirmed") is True
        and row.get("source") == "explicit_user_confirmation"
    ]


def clear_reason(card, cfg, now):
    from pipeline import alerts as A

    stadium = card.get("stadium") or {}
    if stadium.get("roof_state") in ("closed", "dome") or stadium.get("roof_type") == "dome":
        return "Stadium roof is closed"
    spread = A._num((card.get("consensus") or {}).get("spread_open"))
    if card.get("sport") == "cfb" and spread is not None and abs(spread) > 10:
        return "CFB spread exceeds the encoded eligibility limit"
    wx = card.get("weather") or {}
    fetched = A._dt(wx.get("fetched_at"))
    if (
        fetched is None
        or fetched > now
        or now - fetched > timedelta(hours=3)
        or wx.get("point_aged") is not False
        or wx.get("ensemble_unverified_sources")
        or wx.get("ensemble_aged_sources")
        or any(A._num(wx.get(key)) is None for key in ("wind_fg", "temp_fg", "rain_fg"))
    ):
        return None  # Missing, stale or degraded providers cannot prove invalidation.
    label = A._signal_label(card)
    if (label == A.SIGNAL_NONE or A.signal_slug(label) in A.TIER_RANK) and not A._qualifying_signal(card, cfg):
        return "Complete current weather no longer meets the encoded signal rules"
    return None


def late_context(card):
    cons = card.get("consensus") or {}
    original, current = cons.get("true_total_open"), cons.get("total_now")
    if (
        isinstance(original, (int, float))
        and not isinstance(original, bool)
        and original > 0
        and isinstance(current, (int, float))
        and not isinstance(current, bool)
        and cons.get("true_total_open_status") == "attested"
    ):
        move = (current / original - 1) * 100
        context = f"Original {original:g} → current {current:g} ({move:+.1f}%)."
        if move < -10:
            context += " Probably too late: more than roughly 10% below the attested original total."
        return html.escape(context)
    return "True original total unknown; displayed first-observed/T−6 movement is a separate reference."


def compact_notice(card, cfg, reminder=False):
    from pipeline import alerts as A

    cons = card.get('consensus') or {}
    reference, current = A._num(cons.get('total_open')), A._num(cons.get('total_now'))
    movement = ''
    if reference is not None and current is not None:
        basis = html.escape(str(cons.get('total_open_basis') or 'displayed reference'))
        movement = f'\n{basis} {reference:g}; current {current:g}; move {current-reference:+g} pts.'
    return (f"<b>{html.escape(A._matchup(card))}: {html.escape(A._signal_label(card) or '?')}</b>"
            + (' (reminder)' if reminder else '') + '\n' + late_context(card) + movement
            + '\n' + A._details_link(cfg.board_url, card, 'Weather, source clocks and prices'))


def collect(ctx, cards_by_sport, alerts, cfg, now, confirmations):
    from pipeline import alerts as A

    out = []
    slot = routine_slot(now)
    for sport, cards in cards_by_sport.items():
        for card in cards:
            if not A._card_within_alert_window(card, now, cfg):
                continue
            if re.search(r'final|cancel|postpon|suspend|live|progress', str(card.get('status') or ''), re.I):
                continue
            gid = card.get("game_id")
            bets = confirmed_for(confirmations, gid)
            if bets:
                reason = clear_reason(card, cfg, now)
                if reason:
                    for bet in bets:
                        bid = bet["bet_id"]
                        if bid in (alerts.get("cleared_bets") or {}):
                            continue
                        text = f"<b>CLEAR — {html.escape(A._matchup(card))}</b>\n{html.escape(reason)}\nConfirmed UNDER {bet['line']:g}; cash stake ${bet['stake']:g}.\n{A._details_link(cfg.board_url, card)}"
                        out.append(
                            A.Candidate(
                                f"clear|{bid}",
                                "clear",
                                sport,
                                text,
                                game_id=gid,
                                kickoff_utc=A._dt(card.get("kickoff_utc")),
                                status="invalidated",
                                record={"bet_id": bid, "run_id": getattr(ctx, "run_id", None), "clear_reason": reason},
                            )
                        )
                continue  # Explicit bet confirmation permits CLEAR only.
            spread = A._num((card.get("consensus") or {}).get("spread_open"))
            if card.get("sport") == "cfb" and spread is not None and abs(spread) > 10:
                continue
            if not A._qualifying_signal(card, cfg):
                continue
            first = A.edge_candidates(card, alerts, cfg, getattr(ctx, "run_id", None), now)
            if first:
                for candidate in first:
                    candidate.text += "\n" + late_context(card)
                    candidate.summary = compact_notice(card, cfg)
                    if not routine_available(alerts, cfg, now) and candidate.kickoff_utc <= next_routine_at(now):
                        candidate.record['first_notice_reason'] = 'kickoff_before_next_routine_slot'
                        candidate.text += '\nFirst notice now: kickoff is before the next routine update.'
                out += first
            elif slot:
                edge = A._play_edge(card)
                text = (
                    A.format_edge(card, edge, cfg.board_url)
                    + "\nReminder before explicit bet confirmation.\n"
                    + late_context(card)
                )
                out.append(
                    A.Candidate(
                        f"reminder|{slot}|{gid}",
                        "reminder",
                        sport,
                        text,
                        game_id=gid,
                        tier=A.signal_slug(A._signal_label(card)),
                        kickoff_utc=A._dt(card.get("kickoff_utc")),
                        record={"run_id": getattr(ctx, "run_id", None)},
                        summary=compact_notice(card, cfg, reminder=True),
                    )
                )
    return out


def plan(candidates, alerts, tg, now, cfg):
    from pipeline import alerts as A

    result = A.Plan()
    slot = routine_slot(now)
    seen = set()
    # Queues are advisory. Every pass rebuilds candidates from current evidence.
    tg["queue"] = []
    for candidate in sorted(candidates, key=lambda c: A._priority(c, now)):
        if candidate.key in seen or A.pstate.alert_sent(alerts, candidate.key):
            continue
        seen.add(candidate.key)
        if candidate.family == "edge":
            observed = alerts.setdefault("first_signals", {}).setdefault(
                str(candidate.game_id),
                {
                    "first_signal_at": A.utc_iso(now),
                    "first_signal_label": candidate.record.get("last_signal"),
                    **{
                        f"first_signal_{name}": candidate.record.get(f"last_{name}")
                        for name in ("line", "odds", "fair", "edge", "book")
                    },
                },
            )
            candidate.record.update(observed)
        if candidate.family == "clear" or (candidate.family == "edge" and (
                candidate.tier in A.BYPASS_TIERS or candidate.record.get('first_notice_reason') == 'kickoff_before_next_routine_slot')):
            result.send.append(candidate)
        elif slot:
            candidate.record["routine_slot"] = slot
            result.digest.append(candidate)
        else:
            A.pstate.queue_alert(tg, A._to_queue_item(candidate, now))
            result.queued.append(candidate)
    return result


def dispatch(result, alerts, sender, now, cfg, checkpoint=None, confirmation_reader=None):
    from pipeline import alerts as A

    outcome = A.Outcome()

    def latest():
        if confirmation_reader:
            alerts['_confirmations'] = confirmation_reader()  # Failure stops delivery.
        return alerts.get('_confirmations') or {}

    for candidate in result.send:
        bets = confirmed_for(latest(), candidate.game_id)
        if candidate.family != 'clear' and bets:
            continue
        if candidate.family == 'clear' and not any(b['bet_id'] == candidate.record.get('bet_id') for b in bets):
            continue
        if candidate.family == "clear" and candidate.record.get("bet_id") in (alerts.get("cleared_bets") or {}):
            continue
        A._alert_once(sender, alerts, now, outcome, cfg, checkpoint)(candidate)
    slot = routine_slot(now)
    if not slot:
        return outcome
    if result.digest and not cfg.chat_default:
        raise RuntimeError('Routine delivery requires one shared default chat')
    by_chat = {}
    for candidate in result.digest:
        # One shared routine destination combines NFL/CFB into the same two
        # messages. Immediate first/CLEAR notices retain sport-specific routing.
        by_chat.setdefault(cfg.chat_default or cfg.chat_for(candidate.sport), []).append(candidate)
    for chat, candidates in by_chat.items():
        confirmations = latest()
        candidates = [c for c in candidates if not confirmed_for(confirmations, c.game_id)]
        key = f"{chat}|{slot}"
        ledger = alerts.setdefault("routine_delivery", {})
        if key in ledger or sum(k.startswith(f"{chat}|{slot.split('|')[0]}|") for k in ledger) >= 2:
            continue
        pending, texts, size = [], [], 40
        # First notices have priority over reminders. A verbose per-game price
        # explanation must not consume the entire routine message budget.
        for candidate in sorted(candidates, key=lambda c: c.family != "edge"):
            if A.pstate.alert_sent(alerts, candidate.key):
                continue
            text = candidate.summary or candidate.text
            if len(text) + size > 3900:
                continue  # Unsent first notices remain pending for the next slot.
            pending.append(candidate)
            texts.append(text)
            size += len(text) + 2
        if not pending:
            continue
        try:
            ok = bool(sender("<b>WEATHER WATCH — scheduled update</b>\n" + "\n\n".join(texts), chat))
        except Exception:
            ok = False
        if ok:
            outcome.n_messages += 1
            for candidate in pending:
                A._mark(candidate, alerts, now, outcome)
            ledger[key] = A.utc_iso(now)
            if checkpoint:
                checkpoint()
        else:
            outcome.failed.extend(pending)
    return outcome
