"""
Telegram Online-Status-Monitor via Telethon (offizielles MTProto-API).

Setup:
  1. Gehe zu https://my.telegram.org → "API development tools"
  2. Erstelle eine App → notiere API_ID und API_HASH
  3. Setze Umgebungsvariablen:
       TELEGRAM_API_ID=12345
       TELEGRAM_API_HASH=abcdef1234...
       TELEGRAM_PHONE=+49123456789    (dein eigener Telegram-Account)
  4. pip install telethon
  5. python monitors/telegram_status.py
     → Beim ersten Start: SMS-Code eingeben (einmaliger Login)

Was geloggt wird:
  - Jede Online/Offline-Änderung des Ziels mit Zeitstempel
  - "Last seen"-Zeitpunkt bei Offline-Ereignissen
  - Fehler (z.B. Ziel hat Status auf privat gesetzt)

Targets werden über das Web-Dashboard unter /messenger verwaltet.
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime

from telethon import TelegramClient
from telethon.tl.types import (UserStatusLastMonth, UserStatusLastWeek,
                                UserStatusOffline, UserStatusOnline,
                                UserStatusRecently)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

API_ID   = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
PHONE    = os.environ['TELEGRAM_PHONE']
DB_PATH  = os.path.join(os.path.dirname(__file__), '..', 'tracking.db')
INTERVAL = int(os.environ.get('TG_POLL_INTERVAL', '60'))

client = TelegramClient('tg_monitor_session', API_ID, API_HASH)


def log_status(target_id: int, status: str, details: dict | None = None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''INSERT INTO status_events (target_id, timestamp, status, details)
               VALUES (?, ?, ?, ?)''',
            (target_id, datetime.utcnow().isoformat(), status, json.dumps(details or {})),
        )


def parse_status(tg_status) -> tuple[str, dict]:
    if isinstance(tg_status, UserStatusOnline):
        return 'online', {}
    if isinstance(tg_status, UserStatusOffline):
        last = tg_status.was_online
        return 'offline', {'last_seen': last.isoformat() if last else None}
    if isinstance(tg_status, UserStatusRecently):
        return 'recently', {}
    if isinstance(tg_status, UserStatusLastWeek):
        return 'last_week', {}
    if isinstance(tg_status, UserStatusLastMonth):
        return 'last_month', {}
    return 'hidden', {'type': type(tg_status).__name__}


async def poll_once(prev_statuses: dict) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        targets = conn.execute(
            "SELECT id, identifier, label FROM messenger_targets WHERE platform='telegram'"
        ).fetchall()

    for target_id, identifier, label in targets:
        try:
            entity = await client.get_entity(identifier)
            status, details = parse_status(entity.status)
            prev = prev_statuses.get(target_id)
            if status != prev:
                log_status(target_id, status, details)
                log.info('%-20s  %s → %s', label, prev or '?', status)
                prev_statuses[target_id] = status
        except Exception as exc:
            log.warning('Fehler bei %s: %s', identifier, exc)
            log_status(target_id, 'error', {'error': str(exc)})

    return prev_statuses


async def main():
    await client.start(phone=PHONE)
    log.info('Telegram-Monitor gestartet (Intervall: %ds)', INTERVAL)
    prev = {}
    while True:
        prev = await poll_once(prev)
        await asyncio.sleep(INTERVAL)


if __name__ == '__main__':
    asyncio.run(main())
