"""
Telegram-бот для учёта ежедневных отчётов магазина.

Flow:
  1. Сотрудник пишет отчёт свободным текстом.
  2. Groq парсит его в структуру.
  3. Бот показывает превью и две кнопки: «Записать» / «Отмена».
  4. По подтверждению — добавляет строку в Google Sheets.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import uuid
from typing import Any

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from parser import ExpenseParser, FIELDS, format_preview
from sheets import SheetsClient

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# Кэш разобранных отчётов. Ключ — короткий id, который прилетит в callback_data.
# В памяти процесса; для прод-деплоя можно заменить на Redis.
PENDING: dict[str, dict[str, Any]] = {}


def _parse_user_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _is_allowed(update: Update, allowed: set[int]) -> bool:
    if not allowed:
        return True  # если не задан whitelist — разрешаем всем
    user = update.effective_user
    return bool(user and user.id in allowed)


# -------------------- Handlers --------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я записываю дневные отчёты магазина в Google-таблицу.\n\n"
        "Просто пришли отчёт текстом — в свободной форме. Пример:\n\n"
        "<i>21.04.2026\n"
        "Каспи 120к, нал 45000, халык 80к, перевод 30к.\n"
        "На кассе был Ерлан.\n"
        "Лиды: инст 12, вц 7, реклама вц 3, офлайн 4, постоянные 5.\n"
        "Продажи: онлайн 9, офлайн 14.\n"
        "Расходы: курьеры 8000, закуп 60к, прочее 3500.</i>\n\n"
        "/help — подсказка по формату",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["Я распознаю такие поля:\n"]
    for _, ru in FIELDS:
        lines.append(f"• {ru}")
    lines.append(
        "\nФормат свободный — пиши как удобно, главное чтобы названия можно было узнать. "
        "Если чего-то нет в отчёте — ставлю 0."
    )
    await update.message.reply_text("\n".join(lines))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed: set[int] = context.bot_data["allowed_users"]
    if not _is_allowed(update, allowed):
        await update.message.reply_text("Нет доступа. Попроси админа добавить твой id.")
        return

    parser: ExpenseParser = context.bot_data["parser"]
    text = update.message.text

    status_msg = await update.message.reply_text("Разбираю…")
    try:
        parsed = await asyncio.to_thread(parser.parse, text)
    except Exception as e:
        log.exception("parse failed")
        await status_msg.edit_text(f"Ошибка разбора: {html.escape(str(e))}")
        return

    token = uuid.uuid4().hex[:12]
    PENDING[token] = parsed

    preview = format_preview(parsed)
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Записать", callback_data=f"ok:{token}"),
                InlineKeyboardButton("❌ Отмена",   callback_data=f"no:{token}"),
            ]
        ]
    )
    await status_msg.edit_text(
        f"Проверь данные:\n\n{preview}",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    action, _, token = (query.data or "").partition(":")
    parsed = PENDING.pop(token, None)

    if parsed is None:
        await query.edit_message_text("Срок подтверждения истёк. Пришли отчёт заново.")
        return

    if action == "no":
        await query.edit_message_text("Отменено. Пришли отчёт заново при необходимости.")
        return

    if action != "ok":
        return

    sheets: SheetsClient = context.bot_data["sheets"]
    parser: ExpenseParser = context.bot_data["parser"]
    row = parser.row_for_sheet(parsed)

    try:
        row_num = await asyncio.to_thread(sheets.append_row, row)
    except Exception as e:
        log.exception("sheets append failed")
        await query.edit_message_text(
            f"Не получилось записать в таблицу: {html.escape(str(e))}",
        )
        return

    await query.edit_message_text(
        f"Записал в строку {row_num}. ✅\n\n{format_preview(parsed)}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Handler error", exc_info=context.error)


# -------------------- Bootstrap --------------------

def build_app() -> Application:
    load_dotenv()

    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    groq_key = os.environ["GROQ_API_KEY"]
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    sheet_name = os.environ.get("SHEET_NAME", "Отчёты")
    creds_path = os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials.json")
    allowed_users = _parse_user_ids(os.environ.get("ALLOWED_USER_IDS"))

    parser = ExpenseParser(api_key=groq_key)
    sheets = SheetsClient(creds_path=creds_path, spreadsheet_id=spreadsheet_id, sheet_name=sheet_name)
    sheets.ensure_headers()

    app = Application.builder().token(tg_token).build()
    app.bot_data["parser"] = parser
    app.bot_data["sheets"] = sheets
    app.bot_data["allowed_users"] = allowed_users

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    report_filter = filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)отчет")
    app.add_handler(MessageHandler(report_filter, handle_text))
    app.add_error_handler(on_error)

    return app


def main() -> None:
    app = build_app()
    log.info("Бот запущен. Жду сообщения…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
