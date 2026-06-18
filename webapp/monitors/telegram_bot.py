"""
Telegram Bot-Falle — loggt Nutzerdaten beim ersten Kontakt.

Setup:
  1. Schreibe @BotFather auf Telegram → /newbot → Token kopieren
  2. Setze: TELEGRAM_BOT_TOKEN=123456789:ABCdef...
  3. pip install python-telegram-bot
  4. python monitors/telegram_bot.py

Einsatz:
  - Jeder Token im Dashboard hat einen Bot-Link:
      https://t.me/DeinBotName?start=TOKEN
  - Ziel klickt → Bot antwortet unauffällig → Daten landen in der DB:
      Telegram-ID, Username, Vor-/Nachname, Sprache, Zeitstempel

  Tipp: Bot-Link lässt sich auch als Button in andere Nachrichten einbetten.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
DB_PATH   = os.path.join(os.path.dirname(__file__), '..', 'tracking.db')


def log_interaction(user, token: str = '', chat_type: str = ''):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''INSERT INTO bot_interactions
               (timestamp, platform, user_id, username, first_name,
                last_name, language, token, extra)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                datetime.utcnow().isoformat(),
                'telegram',
                str(user.id),
                user.username or '',
                user.first_name or '',
                user.last_name or '',
                user.language_code or '',
                token,
                json.dumps({'is_bot': user.is_bot, 'chat_type': chat_type}),
            ),
        )
    log.info('Interaktion: %s (@%s) token=%s', user.first_name, user.username, token)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = context.args[0] if context.args else ''
    user  = update.effective_user
    chat  = update.effective_chat
    log_interaction(user, token=token, chat_type=chat.type if chat else '')
    await update.message.reply_text(
        'Dieser Link ist leider abgelaufen. Bitte wende dich an den Absender.'
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_interaction(user, token='help')
    await update.message.reply_text('Dieser Bot ist nicht aktiv.')


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('help',  cmd_help))
    log.info('Telegram-Bot gestartet')
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
