"""
WhatsApp Online-Status-Monitor via Selenium + WhatsApp Web.

Requirements:
  pip install selenium webdriver-manager

Setup:
  1. Setze: WHATSAPP_TARGET_PHONE=+49123456789
  2. python monitors/whatsapp_status.py
  3. Beim ersten Start: QR-Code mit deinem WhatsApp scannen
     (Session wird gespeichert → folgende Starts brauchen keinen Scan)
  4. Nach Login: headless betreiben (Zeile unten auskommentieren)

Was geloggt wird:
  - Jeder Wechsel Online ↔ Offline mit Zeitstempel
  - "Schreibt gerade..." Ereignisse
  - Roh-Statustext für Analyse

Einschränkungen:
  - WhatsApp Web läuft nur wenn dein Telefon mit Internet verbunden ist
  - Nach ~14 Tagen Inaktivität muss erneut gescannt werden
  - Apple Mail Privacy: nicht betroffen (direkte WhatsApp-Verbindung)
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

TARGET_PHONE = os.environ.get('WHATSAPP_TARGET_PHONE', '')
DB_PATH      = os.path.join(os.path.dirname(__file__), '..', 'tracking.db')
PROFILE_DIR  = os.path.join(os.path.dirname(__file__), 'wa_profile')
POLL_SEC     = int(os.environ.get('WA_POLL_INTERVAL', '15'))


def get_or_create_target(phone: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM messenger_targets WHERE platform='whatsapp' AND identifier=?",
            (phone,),
        ).fetchone()
        if row:
            return row[0]
        cur = conn.execute(
            '''INSERT INTO messenger_targets (label, platform, identifier, created_at)
               VALUES (?, ?, ?, ?)''',
            (f'WhatsApp {phone}', 'whatsapp', phone, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def log_status(target_id: int, status: str, raw: str = ''):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''INSERT INTO status_events (target_id, timestamp, status, details)
               VALUES (?, ?, ?, ?)''',
            (target_id, datetime.utcnow().isoformat(), status, json.dumps({'raw': raw})),
        )


def classify_status(text: str) -> str:
    t = text.lower()
    if 'online' in t:
        return 'online'
    if 'tipp' in t or 'typing' in t or 'schreibt' in t:
        return 'typing'
    if 'zuletzt' in t or 'last seen' in t:
        return 'offline'
    return 'unknown'


def build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument(f'--user-data-dir={PROFILE_DIR}')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    # Nach erstem QR-Scan-Login folgende Zeile aktivieren:
    # opts.add_argument('--headless=new')
    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts,
    )


def open_chat(driver: webdriver.Chrome, phone: str):
    """Öffnet den Chat direkt via wa.me-URL (kein Kontakt nötig)."""
    driver.get(f'https://web.whatsapp.com/send?phone={phone.replace("+", "")}')
    WebDriverWait(driver, 120).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="conversation-panel-messages"]'))
    )
    log.info('Chat geöffnet: %s', phone)


def read_status(driver: webdriver.Chrome) -> str | None:
    """Liest den Status-Text aus dem Chat-Header."""
    selectors = [
        '[data-testid="conversation-info-header"] [data-testid="status"]',
        'header [title]',
        '._3W2ap',   # Fallback-Klasse (WhatsApp ändert diese regelmäßig)
    ]
    for sel in selectors:
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if els:
            return els[0].text or els[0].get_attribute('title') or ''
    return None


def main():
    target_id = get_or_create_target(TARGET_PHONE)
    driver = build_driver()

    log.info('Warte auf WhatsApp Web Login (ggf. QR-Code scannen)...')
    WebDriverWait(driver, 120).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="chat-list"]'))
    )
    log.info('Eingeloggt. Öffne Chat: %s', TARGET_PHONE)
    open_chat(driver, TARGET_PHONE)

    last_status = None
    while True:
        try:
            raw = read_status(driver)
            if raw is not None:
                status = classify_status(raw)
                if status != last_status:
                    log_status(target_id, status, raw)
                    log.info('%s → %s (%s)', TARGET_PHONE, status, raw)
                    last_status = status
        except Exception as exc:
            log.warning('Lesefehler: %s', exc)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    main()
