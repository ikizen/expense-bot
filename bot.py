"""
Telegram-бот для учёта ежедневных отчётов магазина.

Flow:
  1. Сотрудник пишет отчёт свободным текстом (должно содержать слово "Отчет").
  2. Groq парсит его в структуру.
  3. Бот показывает превью с кнопками: «Записать» / «Редактировать» / «Отмена».
  4. Редактирование: бот ждёт поправку текстом, перепарсивает и обновляет превью.
  5. По подтверждению — добавляет строку в Google Sheets.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
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

# token -> parsed data
PENDING: dict[str, dict[str, Any]] = {}
# user_id -> token (когда ждём поправку)
EDIT_WAITING: dict[int, str] = {}


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
        return True
    user = update.effective_user
    return bool(user and user.id in allowed)


def _make_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Записать",      callback_data=f"ok:{token}"),
        InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit:{token}"),
        InlineKeyboardButton("❌ Отмена",        callback_data=f"no:{token}"),
    ]])


# -------------------- Handlers --------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я записываю дневные отчёты магазина в Google-таблицу.\n\n"
        "Пришли отчёт текстом — в свободной форме, главное чтобы было слово <b>Отчет</b>. Пример:\n\n"
        "<i>Отчет 21.04.2026\n"
        "Каспи 120к, нал 45000, халык 80к, перевод 30к.\n"
        "На кассе был Ерлан.\n"
        "Лиды: инст 12, вц 7, реклама вц 3, офлайн 4, постоянные 5.\n"
        "Продажи: онлайн 9, офлайн 14.\n"
        "Расходы: курьеры 8000, закуп 60к, аренда 150к, прочее 3500.</i>\n\n"
        "/help — подсказка по формату",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["Я распознаю такие поля:\n"]
    for _, ru in FIELDS:
        lines.append(f"• {ru}")
    lines.append("• Любые другие расходы (аренда, зарплата, налог…) — добавляю как новую колонку")
    lines.append(
        "\nФормат свободный. Если ошибся — нажми ✏️ Редактировать и напиши поправку, "
        "например: <i>Каспи на самом деле 200к, кассир Асель</i>"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed: set[int] = context.bot_data["allowed_users"]
    if not _is_allowed(update, allowed):
        return

    user_id = update.effective_user.id
    text = update.message.text or ""

    # Если ждём поправку от этого пользователя — обрабатываем как редактирование
    if user_id in EDIT_WAITING:
        await _handle_edit_correction(update, context, text)
        return

    # Иначе: реагируем только на сообщения со словом "отчет"
    if not re.search(r"отчет", text, re.IGNORECASE):
        return

    # Определяем целевой лист по ключевым словам
    sheet_name = context.bot_data["sheets"].sheet_name
    for keyword, target_sheet in context.bot_data["sheet_routes"].items():
        if re.search(keyword, text, re.IGNORECASE):
            sheet_name = target_sheet
            break

    parser: ExpenseParser = context.bot_data["parser"]
    status_msg = await update.message.reply_text("Разбираю…")
    try:
        parsed = await asyncio.to_thread(parser.parse, text)
    except Exception as e:
        log.exception("parse failed")
        await status_msg.edit_text(f"Ошибка разбора: {html.escape(str(e))}")
        return

    token = uuid.uuid4().hex[:12]
    PENDING[token] = {"data": parsed, "sheet": sheet_name}

    sheet_label = f" → *{sheet_name}*" if sheet_name != context.bot_data["sheets"].sheet_name else ""
    await status_msg.edit_text(
        f"Проверь данные{sheet_label}:\n\n{format_preview(parsed)}",
        reply_markup=_make_keyboard(token),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _handle_edit_correction(
    update: Update, context: ContextTypes.DEFAULT_TYPE, correction_text: str
) -> None:
    user_id = update.effective_user.id
    token = EDIT_WAITING.pop(user_id, None)

    if not token or token not in PENDING:
        await update.message.reply_text("Сессия истекла. Отправь отчёт заново.")
        return

    parser: ExpenseParser = context.bot_data["parser"]
    entry = PENDING[token]
    original = entry["data"]

    status_msg = await update.message.reply_text("Применяю поправку…")
    try:
        merged = await asyncio.to_thread(parser.parse_correction, original, correction_text)
    except Exception as e:
        log.exception("correction parse failed")
        EDIT_WAITING[user_id] = token
        await status_msg.edit_text(f"Ошибка: {html.escape(str(e))}. Попробуй ещё раз.")
        return

    PENDING[token] = {"data": merged, "sheet": entry["sheet"]}
    await status_msg.edit_text(
        f"Обновлено. Проверь:\n\n{format_preview(merged)}",
        reply_markup=_make_keyboard(token),
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    action, _, token = (query.data or "").partition(":")

    if action == "edit":
        if token not in PENDING:
            await query.edit_message_text("Сессия истекла. Пришли отчёт заново.")
            return
        user_id = query.from_user.id
        EDIT_WAITING[user_id] = token
        parsed = PENDING[token]["data"]
        await query.edit_message_text(
            f"Что исправить? Напиши поправку — например:\n"
            f"<i>Каспи на самом деле 200к, кассир Асель</i>\n\n"
            f"Текущие данные:\n\n{format_preview(parsed)}",
            parse_mode=ParseMode.HTML,
        )
        return

    entry = PENDING.pop(token, None)

    if entry is None:
        await query.edit_message_text("Сессия истекла. Пришли отчёт заново.")
        return

    if action == "no":
        await query.edit_message_text("Отменено.")
        return

    if action != "ok":
        return

    parsed = entry["data"]
    sheet_name = entry["sheet"]
    sheets: SheetsClient = context.bot_data["sheets"]
    parser: ExpenseParser = context.bot_data["parser"]
    row = parser.row_for_sheet(parsed)
    extra = parsed.get("extra_expenses", [])

    try:
        row_num = await asyncio.to_thread(sheets.append_row, row, extra, sheet_name)
    except Exception as e:
        log.exception("sheets append failed")
        await query.edit_message_text(
            f"Не получилось записать в таблицу: {html.escape(str(e))}",
        )
        return

    await query.edit_message_text(
        f"Записал в строку {row_num} → *{sheet_name}*. ✅\n\n{format_preview(parsed)}",
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
    # Маршруты: ключевая фраза (regex) → название листа
    # Более специфичные фразы должны идти ПЕРВЫМИ
    sheet_routes = {
        r"отчет\s+финансы": "Финансы",
    }

    app.bot_data["parser"] = parser
    app.bot_data["sheets"] = sheets
    app.bot_data["allowed_users"] = allowed_users
    app.bot_data["sheet_routes"] = sheet_routes

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    # Ловим все текстовые сообщения — внутри handle_text сами фильтруем
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    return app


def main() -> None:
    app = build_app()
    log.info("Бот запущен. Жду сообщения…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
