"""Telegram Bot API integration (copied verbatim from golf_scraping/utils/telegram.py).

Only ``send_message`` is kept here; football alert formatting lives in
``pipeline/alerts.py``.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


async def send_message(text: str, bot_token: str | None = None, chat_id: str | None = None) -> bool:
    """Send a Telegram message. Returns True on success.

    Token/chat_id sourced from args or env vars TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
    """
    token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat:
        logger.warning("Telegram credentials not configured — skipping alert")
        return False

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("Telegram alert sent")
                return True
            else:
                logger.error(f"Telegram API error {resp.status_code}: {resp.text}")
                return False
    except httpx.HTTPError as e:
        logger.error(f"Telegram send failed: {e}")
        return False
