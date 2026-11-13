"""Best-effort workflow Telegram transport, independent of football bet alerts."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # The former curl calls did not follow redirects either.


def open_request(request: Request, *, timeout: float):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def notify(title: str = "", body: str = "", run_url: str = "", *,
           env: Mapping[str, str] | None = None, opener=None,
           timeout: float = 15, strict: bool = False) -> int:
    """Send once; strict errors are usable as a delivery gate for incident markers.

    Missing credentials always skip successfully. Callers that store a delivery
    marker must also guard credentials, so a skip cannot acknowledge a page.
    Only HTTP delivery is checked, matching the former curl -f marker gate.
    """
    values = os.environ if env is None else env
    token = values.get("TELEGRAM_BOT_TOKEN", "")
    chat = values.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("::notice::Telegram credentials absent; skipping notification")
        return 0
    text = "\n".join(part for part in (title, body, f"Open run: {run_url}" if run_url else "") if part)
    try:
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=urlencode({"chat_id": chat, "text": text}).encode("utf-8"),
            method="POST",
        )
        with (opener or open_request)(request, timeout=timeout):
            pass
    except HTTPError as exc:
        # urllib represents unfollowed redirects as exceptions. curl -f accepted
        # these HTTP statuses; preserve that boundary without a second request.
        if exc.code < 400:
            return 0
    except Exception:  # noqa: BLE001 - exception text/URLs can contain the bot token
        pass
    else:
        return 0
    print("::warning::Telegram notification delivery failed")
    return 1 if strict else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 120:
        parser.error("timeout must be greater than zero and at most 120 seconds")
    return notify(args.title, args.body, args.run_url, timeout=args.timeout, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
