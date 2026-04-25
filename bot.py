"""
Telegram-бот — универсальный учёт отчётов.

Команды:
  /start         — приветствие
  /help          — справка
  /config        — текущая конфигурация
  /addcol <название> [text]  — добавить колонку (по умолчанию числовая)
  /removecol <название>      — убрать колонку
  /addalias <слово> <метка>  — добавить алиас (напр. /addalias гортензия "Закуп Цветов")
  /removealias <слово>       — удалить алиас
  /newsheet <название>       — создать новую вкладку
  /addroute <слово> <лист>   — слово в отчёте → конкретная вкладка
  /stats [лист] [дней]       — дашборд (по умолчанию 7 дней)
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
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

import requests as http_requests
from config_manager import ConfigManager
from parser import ExpenseParser, format_preview
from sheets import SheetsClient, create_new_spreadsheet

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

PENDING: dict[str, dict[str, Any]] = {}
EDIT_WAITING: dict[int, str] = {}


def _parse_user_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(p.strip()) for p in raw.split(",") if p.strip().isdigit()}


def _is_allowed(update: Update, allowed: set[int]) -> bool:
    if not allowed:
        return True
    u = update.effective_user
    return bool(u and u.id in allowed)


def _make_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Записать",       callback_data=f"ok:{token}"),
        InlineKeyboardButton("✏️ Редактировать",  callback_data=f"edit:{token}"),
        InlineKeyboardButton("❌ Отмена",         callback_data=f"no:{token}"),
    ]])


# ── Стандартные команды ────────────────────────────────────────────────────

async def cmd_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = context.bot_data.get("spreadsheet_url", "")
    sid = context.bot_data.get("spreadsheet_id", "")
    if url:
        await update.message.reply_text(
            f"📊 <b>Ссылка на таблицу:</b>\n{url}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("Таблица не настроена. Задай SPREADSHEET_ID.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: ConfigManager = context.bot_data["config"]
    triggers = " / ".join(f"<b>{t.capitalize()}</b>" for t in cfg.triggers)
    url = context.bot_data.get("spreadsheet_url", "")
    sheet_line = f'\n📊 <a href="{url}">Открыть таблицу</a>' if url else ""
    await update.message.reply_text(
        f"Привет! Пришли отчёт — начни сообщение со слова {triggers}.\n\n"
        "Пример:\n"
        "<i>Отчет 24.04\n"
        "Каспи 120к, нал 45к\n"
        "Кассир Айгуль\n"
        "Лиды: инст 12, вц 7\n"
        "Расходы: курьеры 8000, закуп гортензии 60к</i>"
        f"{sheet_line}\n\n"
        "/help — все команды  /sheet — ссылка на таблицу",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>Команды бота:</b>\n\n"
        "<b>Отчёт</b>\n"
        "Просто пришли текст — начни с триггерного слова (Отчет / Отчёт).\n\n"
        "<b>Колонки</b>\n"
        "/addcol Название — добавить числовую колонку\n"
        "/addcol Название text — добавить текстовую колонку\n"
        "/removecol Название — убрать колонку\n\n"
        "<b>Алиасы</b> (слово → что записать)\n"
        '/addalias гортензия "Закуп Цветов"\n'
        "/removealias гортензия\n\n"
        "<b>Листы</b>\n"
        "/newsheet Склад — создать новую вкладку\n"
        "/addroute финансы Финансы — слово «финансы» → лист «Финансы»\n\n"
        "<b>Дашборд</b>\n"
        "/stats — за 7 дней\n"
        "/stats Финансы 30 — лист Финансы, 30 дней\n\n"
        "<b>Конфиг</b>\n"
        "/config — текущие настройки"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: ConfigManager = context.bot_data["config"]
    lines = ["<b>Поля:</b>"]
    for f in cfg.fields:
        lines.append(f"  • {f['label']} ({f['type']})")
    if cfg.aliases:
        lines.append("\n<b>Алиасы:</b>")
        for w, t in cfg.aliases.items():
            lines.append(f"  • {w} → {t}")
    if cfg.routes:
        lines.append("\n<b>Маршруты:</b>")
        for k, s in cfg.routes.items():
            lines.append(f"  • «{k}» → лист «{s}»")
    lines.append(f"\n<b>Триггеры:</b> {', '.join(cfg.triggers)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Управление колонками ───────────────────────────────────────────────────

async def cmd_addcol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /addcol Название [text]")
        return
    ftype = "text" if args[-1].lower() == "text" else "number"
    label = " ".join(args[:-1] if ftype == "text" else args)
    if not label:
        await update.message.reply_text("Укажи название колонки.")
        return
    cfg: ConfigManager = context.bot_data["config"]
    if cfg.add_field(label, ftype):
        await update.message.reply_text(f"✅ Колонка <b>{label}</b> добавлена.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"Колонка <b>{label}</b> уже есть.", parse_mode=ParseMode.HTML)


async def cmd_removecol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /removecol Название")
        return
    label = " ".join(args)
    cfg: ConfigManager = context.bot_data["config"]
    result = cfg.remove_field(label)
    if result == "ok":
        await update.message.reply_text(f"✅ Колонка <b>{label}</b> убрана из отчётов.", parse_mode=ParseMode.HTML)
    elif result == "protected":
        await update.message.reply_text(f"❌ Колонку <b>{label}</b> нельзя удалить — она обязательная.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"Колонка <b>{label}</b> не найдена.", parse_mode=ParseMode.HTML)


# ── Алиасы ────────────────────────────────────────────────────────────────

async def cmd_addalias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    # /addalias слово Целевая Метка
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text('Использование: /addalias слово "Целевая метка"\nПример: /addalias гортензия "Закуп Цветов"')
        return
    word = args[0]
    target = " ".join(args[1:]).strip('"\'')
    context.bot_data["config"].add_alias(word, target)
    await update.message.reply_text(
        f'✅ Алиас добавлен: <b>{word}</b> → <b>{target}</b>', parse_mode=ParseMode.HTML)


async def cmd_removealias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /removealias слово")
        return
    word = args[0]
    if context.bot_data["config"].remove_alias(word):
        await update.message.reply_text(f"✅ Алиас <b>{word}</b> удалён.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"Алиас <b>{word}</b> не найден.")


# ── Листы ─────────────────────────────────────────────────────────────────

async def cmd_newsheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /newsheet Название")
        return
    name = " ".join(args)
    sheets: SheetsClient = context.bot_data["sheets"]
    try:
        created = await asyncio.to_thread(sheets.create_sheet, name)
        if created:
            await update.message.reply_text(
                f"✅ Лист <b>{name}</b> создан с текущими заголовками.\n"
                f"Чтобы направлять отчёты туда: /addroute ключслово {name}",
                parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"Лист <b>{name}</b> уже существует.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {html.escape(str(e))}")


async def cmd_addroute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Использование: /addroute ключслово НазваниеЛиста\nПример: /addroute финансы Финансы")
        return
    keyword = args[0]
    sheet_name = " ".join(args[1:])
    context.bot_data["config"].add_route(keyword, sheet_name)
    await update.message.reply_text(
        f'✅ Маршрут: сообщения с «<b>{keyword}</b>» → лист <b>{sheet_name}</b>',
        parse_mode=ParseMode.HTML)


# ── Дашборд ───────────────────────────────────────────────────────────────

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    sheets: SheetsClient = context.bot_data["sheets"]
    cfg: ConfigManager = context.bot_data["config"]

    # /stats [sheet_name] [days]
    sheet_name = sheets.sheet_name
    days = 7
    if args:
        try:
            days = int(args[-1])
            sheet_name = " ".join(args[:-1]) or sheet_name
        except ValueError:
            sheet_name = " ".join(args)

    status = await update.message.reply_text("Собираю данные…")
    try:
        data = await asyncio.to_thread(sheets.get_stats, sheet_name, days)
    except Exception as e:
        await status.edit_text(f"Ошибка: {html.escape(str(e))}")
        return

    if not data or data["count"] == 0:
        await status.edit_text(f"За последние {days} дней нет данных на листе «{sheet_name}».")
        return

    totals = data["totals"]
    count = data["count"]

    # Группируем поля по типу
    money_fields = [f for f in cfg.fields if f["type"] == "number" and
                    any(k in f["label"].lower() for k in ["каспи","наличка","халык","перевод"])]
    expense_fields = [f for f in cfg.fields if f["type"] == "number" and
                      any(k in f["label"].lower() for k in ["расход","курьер","закуп","прочие"])]
    lead_fields = [f for f in cfg.fields if f["type"] == "number" and
                   any(k in f["label"].lower() for k in ["инстаграм","ватсап","офлайн","постоянн","лиды"])]
    sale_fields = [f for f in cfg.fields if f["type"] == "number" and
                   "продаж" in f["label"].lower()]

    # Остальные числовые (кастомные)
    known = {f["label"] for f in money_fields + expense_fields + lead_fields + sale_fields}
    other_fields = [f for f in cfg.fields if f["type"] == "number" and
                    f["label"] not in known and f["label"] in totals and totals[f["label"]] != 0]

    def fmt(n: float) -> str:
        return f"{int(n):,}".replace(",", " ") + " ₸" if n >= 100 else str(int(n))

    lines = [f"📊 <b>{sheet_name}</b> — последние {days} дн. ({count} записей)\n"]

    if money_fields:
        lines.append("💰 <b>Выручка:</b>")
        total_income = 0.0
        for f in money_fields:
            v = totals.get(f["label"], 0)
            if v:
                lines.append(f"  {f['label']}: {fmt(v)}")
                total_income += v
        if total_income:
            lines.append(f"  <b>Итого: {fmt(total_income)}</b>")

    if expense_fields:
        lines.append("\n📦 <b>Расходы:</b>")
        total_exp = 0.0
        for f in expense_fields:
            v = totals.get(f["label"], 0)
            if v:
                lines.append(f"  {f['label']}: {fmt(v)}")
                total_exp += v
        # Кастомные расходы из extra
        for col, v in totals.items():
            if col not in {f["label"] for f in cfg.fields} and v:
                lines.append(f"  {col}: {fmt(v)}")
                total_exp += v
        if total_exp:
            lines.append(f"  <b>Итого: {fmt(total_exp)}</b>")

        if money_fields and total_income and total_exp:
            profit = total_income - total_exp
            lines.append(f"\n🏦 <b>Прибыль: {fmt(profit)}</b>")

    if lead_fields:
        parts = [f"{f['label']}: {int(totals.get(f['label'],0))}" for f in lead_fields if totals.get(f['label'],0)]
        if parts:
            total_leads = sum(totals.get(f['label'], 0) for f in lead_fields)
            lines.append(f"\n👥 <b>Лиды:</b> {int(total_leads)}")
            lines.append("  " + " | ".join(parts))

    if sale_fields:
        parts = [f"{f['label']}: {int(totals.get(f['label'],0))}" for f in sale_fields if totals.get(f['label'],0)]
        if parts:
            total_sales = sum(totals.get(f['label'], 0) for f in sale_fields)
            lines.append(f"\n🛒 <b>Продажи:</b> {int(total_sales)}")
            lines.append("  " + " | ".join(parts))

    if other_fields:
        lines.append("\n📌 <b>Прочее:</b>")
        for f in other_fields:
            lines.append(f"  {f['label']}: {fmt(totals[f['label']])}")

    await status.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Обработка отчётов ──────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return

    user_id = update.effective_user.id
    text = update.message.text or ""

    # Режим редактирования — перехватываем первым
    if user_id in EDIT_WAITING:
        await _handle_edit_correction(update, context, text)
        return

    # Проверяем триггерные слова
    cfg: ConfigManager = context.bot_data["config"]
    if not re.search(cfg.trigger_pattern(), text, re.IGNORECASE):
        return

    # Определяем целевой лист
    sheets: SheetsClient = context.bot_data["sheets"]
    sheet_name = cfg.detect_sheet(text, sheets.sheet_name)

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

    sheet_label = f" → <b>{sheet_name}</b>" if sheet_name != sheets.sheet_name else ""
    preview = format_preview(parsed, cfg.fields)
    await status_msg.edit_text(
        f"Проверь данные{sheet_label}:\n\n{preview}",
        reply_markup=_make_keyboard(token),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _handle_edit_correction(
        update: Update, context: ContextTypes.DEFAULT_TYPE, correction: str) -> None:
    user_id = update.effective_user.id
    token = EDIT_WAITING.pop(user_id, None)
    if not token or token not in PENDING:
        await update.message.reply_text("Сессия истекла. Отправь отчёт заново.")
        return

    entry = PENDING[token]
    parser: ExpenseParser = context.bot_data["parser"]
    cfg: ConfigManager = context.bot_data["config"]

    status_msg = await update.message.reply_text("Применяю поправку…")
    try:
        merged = await asyncio.to_thread(parser.parse_correction, entry["data"], correction)
    except Exception as e:
        log.exception("correction failed")
        EDIT_WAITING[user_id] = token
        await status_msg.edit_text(f"Ошибка: {html.escape(str(e))}. Попробуй ещё раз.")
        return

    PENDING[token] = {"data": merged, "sheet": entry["sheet"]}
    await status_msg.edit_text(
        f"Обновлено:\n\n{format_preview(merged, cfg.fields)}",
        reply_markup=_make_keyboard(token),
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, _, token = (query.data or "").partition(":")
    cfg: ConfigManager = context.bot_data["config"]

    if action == "edit":
        if token not in PENDING:
            await query.edit_message_text("Сессия истекла.")
            return
        EDIT_WAITING[query.from_user.id] = token
        preview = format_preview(PENDING[token]["data"], cfg.fields)
        await query.edit_message_text(
            f"Что исправить? Напиши поправку, например:\n"
            f"<i>Каспи 200к, кассир Асель</i>\n\n"
            f"Текущие данные:\n\n{preview}",
            parse_mode=ParseMode.HTML)
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
        await query.edit_message_text(f"Ошибка записи: {html.escape(str(e))}")
        return

    await query.edit_message_text(
        f"Записал в строку {row_num} → *{sheet_name}*. ✅\n\n{format_preview(parsed, cfg.fields)}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Handler error", exc_info=context.error)


# ── Bootstrap ──────────────────────────────────────────────────────────────

def _try_save_spreadsheet_id_to_railway(spreadsheet_id: str) -> None:
    """Обновляет SPREADSHEET_ID в Railway через API (если токен доступен)."""
    token    = os.environ.get("RAILWAY_API_TOKEN")
    svc_id   = os.environ.get("RAILWAY_SERVICE_ID")
    env_id   = os.environ.get("RAILWAY_ENVIRONMENT_ID")
    proj_id  = os.environ.get("RAILWAY_PROJECT_ID")
    if not all([token, svc_id, env_id, proj_id]):
        log.info("Railway API недоступен — задай SPREADSHEET_ID вручную: %s", spreadsheet_id)
        return
    try:
        resp = http_requests.post(
            "https://backboard.railway.app/graphql/v2",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "query": "mutation U($p:String!,$e:String!,$s:String!,$n:String!,$v:String!)"
                         "{variableUpsert(input:{projectId:$p,environmentId:$e,serviceId:$s,name:$n,value:$v})}",
                "variables": {"p": proj_id, "e": env_id, "s": svc_id,
                              "n": "SPREADSHEET_ID", "v": spreadsheet_id},
            },
            timeout=10,
        )
        if resp.json().get("data", {}).get("variableUpsert"):
            log.info("SPREADSHEET_ID сохранён в Railway env.")
    except Exception as e:
        log.warning("Не удалось сохранить в Railway: %s", e)


def build_app() -> Application:
    load_dotenv()

    tg_token      = os.environ["TELEGRAM_BOT_TOKEN"]
    groq_key      = os.environ["GROQ_API_KEY"]
    sheet_name    = os.environ.get("SHEET_NAME", "Отчёты")
    creds_path    = os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials.json")
    allowed_users = _parse_user_ids(os.environ.get("ALLOWED_USER_IDS"))
    folder_id     = os.environ.get("DRIVE_FOLDER_ID", "").strip()
    owner_email   = os.environ.get("SPREADSHEET_OWNER_EMAIL", "").strip()
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()

    # Временный клиент без config для авторизации Google
    tmp_sheets = SheetsClient(creds_path, spreadsheet_id or "placeholder", sheet_name, config=None)  # type: ignore

    # Автосоздание таблицы если SPREADSHEET_ID не задан
    if not spreadsheet_id:
        log.info("SPREADSHEET_ID не задан — создаю новую таблицу…")
        bot_name = os.environ.get("BOT_NAME", "Expense Bot")
        spreadsheet_id, spreadsheet_url = create_new_spreadsheet(
            gs_client=tmp_sheets.client,
            title=bot_name,
            folder_id=folder_id or None,
            share_email=owner_email or None,
        )
        os.environ["SPREADSHEET_ID"] = spreadsheet_id
        _try_save_spreadsheet_id_to_railway(spreadsheet_id)
    else:
        spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"

    sheets = SheetsClient(creds_path, spreadsheet_id, sheet_name, config=None)  # type: ignore
    config = ConfigManager(spreadsheet_id, sheets.client)
    sheets.config = config

    sheets.ensure_headers()
    parser = ExpenseParser(api_key=groq_key, config=config)

    app = Application.builder().token(tg_token).build()
    app.bot_data.update({
        "parser": parser,
        "sheets": sheets,
        "config": config,
        "allowed_users": allowed_users,
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_url": spreadsheet_url,
    })

    app.add_handler(CommandHandler("sheet",       cmd_sheet))
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("config",      cmd_config))
    app.add_handler(CommandHandler("addcol",      cmd_addcol))
    app.add_handler(CommandHandler("removecol",   cmd_removecol))
    app.add_handler(CommandHandler("addalias",    cmd_addalias))
    app.add_handler(CommandHandler("removealias", cmd_removealias))
    app.add_handler(CommandHandler("newsheet",    cmd_newsheet))
    app.add_handler(CommandHandler("addroute",    cmd_addroute))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    return app


def main() -> None:
    app = build_app()
    log.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
