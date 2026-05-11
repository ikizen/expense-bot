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
import json
import logging
import os
import re
import time
import uuid
from typing import Any

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

import requests as http_requests
from config_manager import ConfigManager
from parser import ExpenseParser, format_preview
from sheets import SheetsClient, create_new_spreadsheet, _build_gs_client

from logging.handlers import RotatingFileHandler as _RotatingFileHandler

# FIX #9: ротация логов — максимум 5 MB × 3 файла
_log_handler = _RotatingFileHandler(
    "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[_log_handler, logging.StreamHandler()],
)
log = logging.getLogger("bot")

PENDING: dict[str, dict[str, Any]] = {}
EDIT_WAITING: dict[int, str] = {}
# Состояние мастера создания листа: user_id → {name, selected: set[label]}
NEW_SHEET_PENDING: dict[int, dict] = {}
# Ожидание текстового ввода из меню настроек
SETTINGS_WAITING: dict[int, dict] = {}
# {user_id: {action: str, chat_id: int, msg_id: int}}

# Нижняя клавиатура — всегда видна
MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📊 Статистика"), KeyboardButton("🔗 Таблица")],
        [KeyboardButton("⚙️ Настройки"),  KeyboardButton("❓ Помощь")],
    ],
    resize_keyboard=True,
)


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
    if url:
        await update.message.reply_text(
            f"📊 <b>Ссылка на таблицу:</b>\n{url}",
            parse_mode=ParseMode.HTML,
        )
    else:
        sa_email = context.bot_data.get("service_account_email", "см. credentials.json")
        await update.message.reply_text(
            "⚠️ Таблица не настроена.\n\n"
            "Чтобы подключить таблицу:\n"
            "1. Создайте таблицу: https://sheets.new\n"
            f"2. Поделитесь с: <code>{sa_email}</code> (роль «Редактор»)\n"
            "3. Скопируйте ID из URL (часть между /d/ и /edit)\n"
            "4. Отправьте: /setsheet ID",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_setsheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Привязать бота к существующей Google Таблице."""
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /setsheet SPREADSHEET_ID\nID можно найти в URL таблицы.")
        return
    new_id = args[0].strip()
    # Принять полный URL тоже
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", new_id)
    if m:
        new_id = m.group(1)

    sheets: SheetsClient = context.bot_data["sheets"]
    sheets.spreadsheet_id = new_id

    cfg: ConfigManager = context.bot_data["config"]
    cfg._spreadsheet_id = new_id

    url = f"https://docs.google.com/spreadsheets/d/{new_id}"
    context.bot_data["spreadsheet_id"] = new_id
    context.bot_data["spreadsheet_url"] = url

    # Инициализируем заголовки
    try:
        await asyncio.to_thread(sheets.ensure_headers)
        _try_save_spreadsheet_id_to_railway(new_id)
        await update.message.reply_text(
            f"✅ Таблица подключена!\n📊 {url}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка доступа к таблице: {html.escape(str(e))}\n"
            "Проверь, что таблица расшарена с сервисным аккаунтом."
        )


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
        "<code>v2.0 — меню, кнопки, мультилист</code>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=MAIN_KB,
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
        "/config — текущие настройки\n"
        "/setsheet ID — подключить Google Таблицу по ID\n"
        "/sheet — ссылка на текущую таблицу\n"
        "/resetconfig — сбросить поля к заводским (осторожно!)\n"
        "/syncheaders [лист] — синхронизировать заголовки таблицы с конфигом"
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


async def cmd_syncheaders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Синхронизирует строку заголовков листа с текущим конфигом.
    Использование: /syncheaders [НазваниеЛиста]"""
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    args = context.args or []
    sheets: SheetsClient = context.bot_data["sheets"]
    sheet_name = " ".join(args) if args else sheets.sheet_name

    status = await update.message.reply_text(f"Синхронизирую заголовки листа «{sheet_name}»…")
    try:
        result = await asyncio.to_thread(sheets.sync_headers, sheet_name)
    except Exception as e:
        await status.edit_text(f"❌ Ошибка: {html.escape(str(e))}")
        return

    lines = [f"✅ <b>Заголовки листа «{html.escape(sheet_name)}» обновлены!</b>\n"]
    if result["added"]:
        lines.append("➕ <b>Добавлены:</b> " + ", ".join(
            f"<b>{html.escape(h)}</b>" for h in result["added"]))
    if result["cleared"]:
        lines.append("🗑 <b>Очищены (старые):</b> " + ", ".join(
            f"<code>{html.escape(h)}</code>" for h in result["cleared"]))
    if result["kept"]:
        lines.append(f"✔️ Без изменений: {len(result['kept'])} колонок")
    lines.append("\n⚠️ Если в таблице были старые данные — проверь их вручную, "
                 "строки данных не перемещались.")
    await status.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_resetconfig(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбрасывает конфиг к заводским настройкам (поля из отчёта по умолчанию)."""
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    cfg: ConfigManager = context.bot_data["config"]
    try:
        await asyncio.to_thread(cfg.reset_to_defaults)
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось сбросить конфиг: {html.escape(str(e))}")
        return
    lines = [
        "✅ <b>Конфиг сброшен к заводским настройкам!</b>\n",
        "<b>Поля теперь:</b>",
    ]
    for f in cfg.fields:
        lines.append(f"  • {f['label']} ({f['type']})")
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
    try:
        added = cfg.add_field(label, ftype)
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось сохранить: {html.escape(str(e))}")
        return
    if added:
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
    try:
        result = cfg.remove_field(label)
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось сохранить: {html.escape(str(e))}")
        return
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
    try:
        context.bot_data["config"].add_alias(word, target)
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось сохранить: {html.escape(str(e))}")
        return
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
    try:
        found = context.bot_data["config"].remove_alias(word)
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось сохранить: {html.escape(str(e))}")
        return
    if found:
        await update.message.reply_text(f"✅ Алиас <b>{word}</b> удалён.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"Алиас <b>{word}</b> не найден.")


# ── Листы ─────────────────────────────────────────────────────────────────

# ── Меню настроек ─────────────────────────────────────────────────────────

def _settings_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Колонки",   callback_data="m_cols"),
            InlineKeyboardButton("🔀 Маршруты",  callback_data="m_routes"),
        ],
        [
            InlineKeyboardButton("💬 Алиасы",    callback_data="m_aliases"),
            InlineKeyboardButton("📄 Новый лист", callback_data="m_newsheet_menu"),
        ],
        [InlineKeyboardButton("❌ Закрыть", callback_data="m_close")],
    ])


def _cols_text_and_kb(cfg: ConfigManager) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"📋 <b>Колонки ({len(cfg.fields)}):</b>\n"]
    rows: list[list[InlineKeyboardButton]] = []
    for i, f in enumerate(cfg.fields):
        protected = f["key"] in {"date", "kassir"}
        lock = " 🔒" if protected else ""
        lines.append(f"  • {f['label']} ({f['type']}){lock}")
        if not protected:
            rows.append([InlineKeyboardButton(
                f"➖ {f['label']}", callback_data=f"m_cdel:{i}",  # индекс, не ключ
            )])
    rows += [
        [
            InlineKeyboardButton("➕ Числовая",   callback_data="m_cadd_n"),
            InlineKeyboardButton("➕ Текстовая",  callback_data="m_cadd_t"),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="m_main")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _routes_text_and_kb(cfg: ConfigManager) -> tuple[str, InlineKeyboardMarkup]:
    routes = cfg.routes
    if routes:
        lines = ["🔀 <b>Маршруты:</b>\n"]
        rows: list[list[InlineKeyboardButton]] = []
        for i, (kw, sheet) in enumerate(routes.items()):
            lines.append(f"  • «{kw}» → <b>{html.escape(sheet)}</b>")
            rows.append([InlineKeyboardButton(
                f"➖ «{kw}»", callback_data=f"m_rdel:{i}",  # индекс
            )])
    else:
        lines = ["🔀 <b>Маршруты:</b>\n", "  Пусто — все идёт на основной лист."]
        rows = []
    rows += [
        [InlineKeyboardButton("➕ Добавить маршрут", callback_data="m_radd")],
        [InlineKeyboardButton("◀️ Назад", callback_data="m_main")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _aliases_text_and_kb(cfg: ConfigManager) -> tuple[str, InlineKeyboardMarkup]:
    aliases = cfg.aliases
    if aliases:
        lines = ["💬 <b>Алиасы:</b>\n"]
        rows: list[list[InlineKeyboardButton]] = []
        for i, (word, target) in enumerate(aliases.items()):
            lines.append(f"  • {word} → {target}")
            rows.append([InlineKeyboardButton(
                f"➖ {word}", callback_data=f"m_adel:{i}",  # индекс
            )])
    else:
        lines = ["💬 <b>Алиасы:</b>\n", "  Пусто."]
        rows = []
    rows += [
        [InlineKeyboardButton("➕ Добавить алиас", callback_data="m_aadd")],
        [InlineKeyboardButton("◀️ Назад", callback_data="m_main")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    cfg: ConfigManager = context.bot_data["config"]
    text = (
        "⚙️ <b>Настройки бота</b>\n\n"
        f"Полей: <b>{len(cfg.fields)}</b>  |  "
        f"Алиасов: <b>{len(cfg.aliases)}</b>  |  "
        f"Маршрутов: <b>{len(cfg.routes)}</b>"
    )
    await update.message.reply_text(
        text, reply_markup=_settings_main_kb(), parse_mode=ParseMode.HTML,
    )


async def _handle_settings_input(
        update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Обрабатывает текстовый ввод пользователя в режиме ожидания настроек."""
    user_id = update.effective_user.id
    state = SETTINGS_WAITING.pop(user_id, None)
    if not state:
        return
    cfg: ConfigManager = context.bot_data["config"]
    action = state["action"]
    chat_id = state["chat_id"]
    msg_id  = state["msg_id"]

    async def _back_to(new_text: str, kb: InlineKeyboardMarkup) -> None:
        """Редактирует сообщение-меню и удаляет сообщение пользователя."""
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=new_text, reply_markup=kb, parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        try:
            await update.message.delete()
        except Exception:
            pass

    text = text.strip()

    if action in ("col_num", "col_txt"):
        # Каждая строка = отдельная колонка
        labels = [l.strip() for l in text.splitlines() if l.strip()]
        ftype = "number" if action == "col_num" else "text"
        added, skipped = [], []
        for label in labels:
            if cfg.add_field(label, ftype):
                added.append(label)
            else:
                skipped.append(label)
        t, kb = _cols_text_and_kb(cfg)
        note = ""
        if added:
            note += "\n\n✅ Добавлены: " + ", ".join(f"<b>{html.escape(l)}</b>" for l in added)
        if skipped:
            note += "\n⚠️ Уже есть: " + ", ".join(f"<b>{html.escape(l)}</b>" for l in skipped)
        await _back_to(t + note, kb)

    elif action == "route":
        parts = text.split(None, 1)
        if len(parts) == 2:
            cfg.add_route(parts[0], parts[1])
            t, kb = _routes_text_and_kb(cfg)
            await _back_to(
                t + f"\n\n✅ Добавлен: «{html.escape(parts[0])}» → <b>{html.escape(parts[1])}</b>", kb,
            )
        else:
            SETTINGS_WAITING[user_id] = state   # вернуть ожидание
            await update.message.reply_text(
                "⚠️ Формат: <code>ключслово НазваниеЛиста</code>\n"
                "Пример: <code>финансы Финансы</code>",
                parse_mode=ParseMode.HTML,
            )

    elif action == "alias":
        parts = text.split(None, 1)
        if len(parts) == 2:
            cfg.add_alias(parts[0], parts[1])
            t, kb = _aliases_text_and_kb(cfg)
            await _back_to(
                t + f"\n\n✅ Добавлен: {html.escape(parts[0])} → <b>{html.escape(parts[1])}</b>", kb,
            )
        else:
            SETTINGS_WAITING[user_id] = state
            await update.message.reply_text(
                "⚠️ Формат: <code>слово Целевая Метка</code>\n"
                "Пример: <code>гортензия Закуп Цветов</code>",
                parse_mode=ParseMode.HTML,
            )

    elif action == "newsheet_name":
        name = text.strip().lstrip("#").strip()
        if not name or len(name) > 100:
            SETTINGS_WAITING[user_id] = state
            await update.message.reply_text(
                "⚠️ Название не может быть пустым или длиннее 100 символов."
            )
            return
        NEW_SHEET_PENDING[user_id] = {"name": name, "cols": [], "ts": time.time()}
        SETTINGS_WAITING[user_id] = {
            "action": "newsheet_cols",
            "chat_id": state["chat_id"],
            "msg_id": state["msg_id"],
        }
        await _back_to(
            f"📋 Лист: <b>{html.escape(name)}</b>\n\n"
            "Напиши столбцы через запятую:\n\n"
            "<i>Пример: Дата, Флорист, Наличные, Kaspi, Итого</i>",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data=f"nsx:{user_id}"),
            ]]),
        )

    elif action in ("newsheet_cols", "newsheet_col"):
        labels = [l.strip() for l in re.split(r"[,\n]+", text) if l.strip()]
        if not labels:
            SETTINGS_WAITING[user_id] = state
            await update.message.reply_text(
                "⚠️ Напиши хотя бы один столбец через запятую."
            )
            return
        state2 = NEW_SHEET_PENDING.get(user_id)
        if not state2:
            await update.message.reply_text("Сессия истекла. Запусти /newsheet заново.")
            return
        state2["cols"] = labels
        cols_preview = "\n".join(f"  • {html.escape(l)}" for l in labels)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Создать лист",      callback_data=f"nsc:{user_id}")],
            [InlineKeyboardButton("✏️ Изменить столбцы", callback_data=f"nse:{user_id}")],
            [InlineKeyboardButton("❌ Отмена",            callback_data=f"nsx:{user_id}")],
        ])
        await _back_to(
            f"📋 Лист: <b>{html.escape(state2['name'])}</b>\n\n"
            f"Столбцы ({len(labels)}):\n{cols_preview}\n\n"
            "Создать?",
            kb,
        )


async def cmd_newsheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    args = context.args or []
    name = " ".join(args).strip().lstrip("#").strip()
    user_id = update.effective_user.id

    if not name:
        await update.message.reply_text("Использование: /newsheet Название")
        return
    if len(name) > 100:
        await update.message.reply_text(
            f"❌ Название слишком длинное ({len(name)} симв.). Максимум — 100."
        )
        return

    NEW_SHEET_PENDING[user_id] = {"name": name, "cols": [], "ts": time.time()}
    msg = await update.message.reply_text(
        f"📋 Новый лист: <b>{html.escape(name)}</b>\n\n"
        "Напиши столбцы через запятую:\n\n"
        "<i>Пример: Дата, Флорист, Наличные, Kaspi, Расход, Итого</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data=f"nsx:{user_id}"),
        ]]),
    )
    SETTINGS_WAITING[user_id] = {
        "action": "newsheet_cols",
        "chat_id": update.message.chat_id,
        "msg_id": msg.message_id,
    }


async def cmd_addroute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Использование: /addroute ключслово НазваниеЛиста\nПример: /addroute финансы Финансы")
        return
    keyword = args[0]
    sheet_name = " ".join(args[1:])
    try:
        context.bot_data["config"].add_route(keyword, sheet_name)
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось сохранить: {html.escape(str(e))}")
        return
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
    count  = data["count"]

    def fmt(n: float) -> str:
        return f"{int(n):,}".replace(",", " ") + " ₸" if n >= 100 else str(int(n))

    # Группировка по key-паттернам — не зависит от локали меток
    _PAYMENT  = {"nalichka", "kaspi", "halyk", "perevod", "inostr_valuta"}
    _EXPENSES = {"dostavka", "zarplata", "ofis_rashody"}
    _CASH     = {"nalichka_nachalo", "ostatok_nalichnykh"}

    groups: dict[str, list[tuple[str, float]]] = {
        "💰 Выручка":  [],
        "📦 Расходы":  [],
        "💵 Наличные": [],
        "🎯 Заявки":   [],
        "🛒 Продажи":  [],
        "📌 Прочее":   [],
    }

    for f in cfg.fields:
        if f["type"] != "number":
            continue
        key, label = f["key"], f["label"]
        val = totals.get(label, 0)
        if key in _PAYMENT:
            groups["💰 Выручка"].append((label, val))
        elif key in _EXPENSES:
            groups["📦 Расходы"].append((label, val))
        elif key in _CASH:
            groups["💵 Наличные"].append((label, val))
        elif key.endswith("_zayavki"):
            groups["🎯 Заявки"].append((label, val))
        elif key.endswith("_prodazhi"):
            groups["🛒 Продажи"].append((label, val))
        else:
            groups["📌 Прочее"].append((label, val))

    income_total  = sum(v for _, v in groups["💰 Выручка"])
    expense_total = sum(v for _, v in groups["📦 Расходы"])

    lines = [f"📊 <b>{html.escape(sheet_name)}</b> — {days} дн. ({count} записей)\n"]

    for title, items in groups.items():
        non_zero = [(l, v) for l, v in items if v]
        if not non_zero:
            continue
        lines.append(f"\n{title}:")
        for label, val in non_zero:
            lines.append(f"  {html.escape(label)}: {fmt(val)}")
        group_sum = sum(v for _, v in non_zero)
        if len(non_zero) > 1:
            lines.append(f"  <b>Итого: {fmt(group_sum)}</b>")

    if income_total and expense_total:
        profit = income_total - expense_total
        lines.append(f"\n🏦 <b>Прибыль: {fmt(profit)}</b>")

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

    # Кнопки нижней клавиатуры
    if text == "📊 Статистика":
        await cmd_stats(update, context)
        return
    if text == "🔗 Таблица":
        await cmd_sheet(update, context)
        return
    if text == "⚙️ Настройки":
        await cmd_menu(update, context)
        return
    if text == "❓ Помощь":
        await cmd_help(update, context)
        return

    # Ожидание текстового ввода из меню настроек
    if user_id in SETTINGS_WAITING:
        await _handle_settings_input(update, context, text)
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

    # Заголовки целевого листа — для умного предпросмотра
    sheet_headers: list[str] | None = None
    if sheet_name != sheets.sheet_name:
        try:
            sheet_headers = await asyncio.to_thread(sheets.get_headers, sheet_name)
        except Exception:
            pass

    token = uuid.uuid4().hex[:12]
    PENDING[token] = {
        "data": parsed, "sheet": sheet_name,
        "sheet_headers": sheet_headers, "raw_text": text,
        "ts": time.time(),
    }
    try:
        await asyncio.to_thread(sheets.save_session, token, PENDING[token])
    except Exception as e:
        log.warning("save_session: %s", e)

    sheet_label = f" → <b>{html.escape(sheet_name)}</b>" if sheet_name != sheets.sheet_name else ""
    preview = format_preview(parsed, cfg.fields, sheet_headers)
    await status_msg.edit_text(
        f"Проверь данные{sheet_label}:\n\n{preview}",
        reply_markup=_make_keyboard(token),
        parse_mode=ParseMode.HTML,
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

    PENDING[token] = {
        "data": merged, "sheet": entry["sheet"],
        "sheet_headers": entry.get("sheet_headers"),
    }
    await status_msg.edit_text(
        f"Обновлено:\n\n{format_preview(merged, cfg.fields, entry.get('sheet_headers'))}",
        reply_markup=_make_keyboard(token),
        parse_mode=ParseMode.HTML,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    cfg: ConfigManager = context.bot_data["config"]
    try:
        await _handle_callback_inner(query, data, cfg, context)
    except Exception as e:
        log.exception("Callback error: %s", data)
        try:
            await query.edit_message_text(f"⚠️ Ошибка: {html.escape(str(e))}")
        except Exception:
            pass


async def _handle_callback_inner(query, data: str, cfg: "ConfigManager",
                                  context: ContextTypes.DEFAULT_TYPE) -> None:

    # ── Мастер создания нового листа ──────────────────────────────────────
    if data.startswith("nsc:"):        # создать лист
        uid = int(data.split(":", 1)[1])
        state = NEW_SHEET_PENDING.pop(uid, None)
        SETTINGS_WAITING.pop(uid, None)
        if not state:
            await query.edit_message_text("Сессия истекла. Запусти /newsheet заново.")
            return
        name = state["name"]
        cols: list[str] = state.get("cols", [])
        if not cols:
            await query.answer("Нет столбцов!", show_alert=True)
            NEW_SHEET_PENDING[uid] = state
            return
        sheets: SheetsClient = context.bot_data["sheets"]
        try:
            created = await asyncio.to_thread(sheets.create_sheet, name, cols)
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {html.escape(str(e))}")
            return
        if created:
            route_hint = name.lower()
            await query.edit_message_text(
                f"✅ Лист <b>{html.escape(name)}</b> создан!\n\n"
                f"<b>Столбцы:</b> {html.escape(', '.join(cols))}\n\n"
                f"Чтобы отчёты с «#{html.escape(route_hint)}» шли туда:\n"
                f"<code>/addroute {html.escape(route_hint)} {html.escape(name)}</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(
                f"Лист <b>{html.escape(name)}</b> уже существует.",
                parse_mode=ParseMode.HTML,
            )
        return

    if data.startswith("nse:"):        # изменить столбцы (ввести заново)
        uid = int(data.split(":", 1)[1])
        if uid not in NEW_SHEET_PENDING:
            await query.edit_message_text("Сессия истекла. Запусти /newsheet заново.")
            return
        SETTINGS_WAITING[uid] = {
            "action": "newsheet_cols",
            "chat_id": query.message.chat_id,
            "msg_id": query.message.message_id,
        }
        await query.edit_message_text(
            f"📋 Лист: <b>{html.escape(NEW_SHEET_PENDING[uid]['name'])}</b>\n\n"
            "Напиши столбцы заново через запятую:\n\n"
            "<i>Пример: Дата, Флорист, Наличные, Kaspi</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data=f"nsx:{uid}"),
            ]]),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("nsx:"):        # отмена
        uid = int(data.split(":", 1)[1])
        NEW_SHEET_PENDING.pop(uid, None)
        SETTINGS_WAITING.pop(uid, None)
        await query.edit_message_text("Отменено.")
        return

    # ── Меню настроек (m_*) ────────────────────────────────────────────────
    if data.startswith("m_"):
        cfg: ConfigManager = context.bot_data["config"]
        uid = query.from_user.id

        if data == "m_main":
            t = (
                "⚙️ <b>Настройки бота</b>\n\n"
                f"Полей: <b>{len(cfg.fields)}</b>  |  "
                f"Алиасов: <b>{len(cfg.aliases)}</b>  |  "
                f"Маршрутов: <b>{len(cfg.routes)}</b>"
            )
            await query.edit_message_text(t, reply_markup=_settings_main_kb(), parse_mode=ParseMode.HTML)

        elif data == "m_cols":
            t, kb = _cols_text_and_kb(cfg)
            await query.edit_message_text(t, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data.startswith("m_cdel:"):
            idx = int(data[7:])
            label = cfg.fields[idx]["label"] if idx < len(cfg.fields) else ""
            result = cfg.remove_field(label) if label else "not_found"
            t, kb = _cols_text_and_kb(cfg)
            suffix = f"\n\n✅ Удалена: <b>{html.escape(label)}</b>" if result == "ok" else ""
            await query.edit_message_text(t + suffix, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data in ("m_cadd_n", "m_cadd_t"):
            kind = "числовую" if data == "m_cadd_n" else "текстовую"
            action_key = "col_num" if data == "m_cadd_n" else "col_txt"
            SETTINGS_WAITING[uid] = {
                "action": action_key,
                "chat_id": query.message.chat_id,
                "msg_id": query.message.message_id,
            }
            await query.edit_message_text(
                f"✏️ Введи название новой <b>{kind}</b> колонки:\n\n"
                "<i>Просто напиши название в чат</i>",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="m_cols_cancel")
                ]]),
                parse_mode=ParseMode.HTML,
            )

        elif data == "m_cols_cancel":
            SETTINGS_WAITING.pop(uid, None)
            t, kb = _cols_text_and_kb(cfg)
            await query.edit_message_text(t, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data == "m_routes":
            t, kb = _routes_text_and_kb(cfg)
            await query.edit_message_text(t, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data.startswith("m_rdel:"):
            idx = int(data[7:])
            keys = list(cfg.routes.keys())
            kw = keys[idx] if idx < len(keys) else ""
            if kw:
                cfg.remove_route(kw)
            t, kb = _routes_text_and_kb(cfg)
            suffix = f"\n\n✅ Маршрут «{html.escape(kw)}» удалён." if kw else ""
            await query.edit_message_text(t + suffix, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data == "m_radd":
            SETTINGS_WAITING[uid] = {
                "action": "route",
                "chat_id": query.message.chat_id,
                "msg_id": query.message.message_id,
            }
            await query.edit_message_text(
                "✏️ Введи маршрут в формате:\n"
                "<code>ключслово НазваниеЛиста</code>\n\n"
                "Пример:\n<code>финансы Финансы</code>",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="m_routes_cancel")
                ]]),
                parse_mode=ParseMode.HTML,
            )

        elif data == "m_routes_cancel":
            SETTINGS_WAITING.pop(uid, None)
            t, kb = _routes_text_and_kb(cfg)
            await query.edit_message_text(t, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data == "m_aliases":
            t, kb = _aliases_text_and_kb(cfg)
            await query.edit_message_text(t, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data.startswith("m_adel:"):
            idx = int(data[7:])
            words = list(cfg.aliases.keys())
            word = words[idx] if idx < len(words) else ""
            if word:
                cfg.remove_alias(word)
            t, kb = _aliases_text_and_kb(cfg)
            suffix = f"\n\n✅ Алиас «{html.escape(word)}» удалён." if word else ""
            await query.edit_message_text(t + suffix, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data == "m_aadd":
            SETTINGS_WAITING[uid] = {
                "action": "alias",
                "chat_id": query.message.chat_id,
                "msg_id": query.message.message_id,
            }
            await query.edit_message_text(
                "✏️ Введи алиас в формате:\n"
                "<code>слово Целевая Метка</code>\n\n"
                "Пример:\n<code>гортензия Закуп Цветов</code>",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="m_aliases_cancel")
                ]]),
                parse_mode=ParseMode.HTML,
            )

        elif data == "m_aliases_cancel":
            SETTINGS_WAITING.pop(uid, None)
            t, kb = _aliases_text_and_kb(cfg)
            await query.edit_message_text(t, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data == "m_newsheet_menu":
            SETTINGS_WAITING[uid] = {
                "action": "newsheet_name",
                "chat_id": query.message.chat_id,
                "msg_id": query.message.message_id,
            }
            await query.edit_message_text(
                "📋 <b>Создание нового листа</b>\n\nНапиши название листа:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Назад", callback_data="m_main")
                ]]),
                parse_mode=ParseMode.HTML,
            )

        elif data == "m_close":
            SETTINGS_WAITING.pop(uid, None)
            await query.edit_message_text("Настройки закрыты.")

        return

    # ── Обычные кнопки отчёта (ok / edit / no) ────────────────────────────
    action, _, token = data.partition(":")

    if action == "edit":
        if token not in PENDING:
            await query.edit_message_text("Сессия истекла.")
            return
        EDIT_WAITING[query.from_user.id] = token
        entry_data = PENDING[token]
        preview = format_preview(entry_data["data"], cfg.fields, entry_data.get("sheet_headers"))
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
    sheets_client: SheetsClient = context.bot_data["sheets"]
    try:
        await asyncio.to_thread(sheets_client.delete_session, token)
    except Exception as e:
        log.warning("delete_session: %s", e)
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
    extra_expenses = parsed.get("extra_expenses", [])
    raw_text = entry.get("raw_text", "")

    try:
        row_num = await asyncio.to_thread(
            sheets.append_row, row, sheet_name, extra_expenses, raw_text
        )
    except Exception as e:
        log.exception("sheets append failed")
        await query.edit_message_text(f"Ошибка записи: {html.escape(str(e))}")
        return

    sheet_headers = entry.get("sheet_headers")
    await query.edit_message_text(
        f"Записал в строку {row_num} → *{sheet_name}*. ✅\n\n"
        f"{format_preview(parsed, cfg.fields, sheet_headers)}",
        parse_mode=ParseMode.HTML,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Handler error", exc_info=context.error)


# FIX #4: периодическая очистка протухших сессий без job-queue
def _do_cleanup() -> None:
    """Синхронная очистка — вызывается из async loop каждые 30 мин."""
    cutoff = time.time() - 3600
    expired_tokens = [t for t, v in PENDING.items() if v.get("ts", 0) < cutoff]
    for t in expired_tokens:
        PENDING.pop(t, None)
    stale_users = [u for u, t in EDIT_WAITING.items() if t not in PENDING]
    for u in stale_users:
        EDIT_WAITING.pop(u, None)
    stale_ns = [u for u, v in NEW_SHEET_PENDING.items()
                if time.time() - v.get("ts", time.time()) > 1800]
    for u in stale_ns:
        NEW_SHEET_PENDING.pop(u, None)
        SETTINGS_WAITING.pop(u, None)
    if expired_tokens or stale_users or stale_ns:
        log.info("Очистка сессий: PENDING-%d EDIT-%d NS-%d",
                 len(expired_tokens), len(stale_users), len(stale_ns))


async def _cleanup_loop(sheets: "SheetsClient") -> None:
    """Фоновая задача: чистим память и лист _sessions каждые 30 минут."""
    while True:
        await asyncio.sleep(1800)
        _do_cleanup()
        try:
            removed = await asyncio.to_thread(sheets.cleanup_sessions)
            if removed:
                log.info("Удалено %d истёкших сессий из Sheets", removed)
        except Exception as e:
            log.warning("cleanup_sessions: %s", e)


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

    # FIX #3: понятная ошибка при отсутствии credentials
    try:
        gs_client = _build_gs_client(creds_path)
    except FileNotFoundError as e:
        log.critical(
            "❌ Google credentials не найдены: %s\n"
            "Задайте GOOGLE_CREDENTIALS_JSON или положите credentials.json рядом с bot.py", e
        )
        raise SystemExit(1) from e
    except Exception as e:
        log.critical("❌ Ошибка авторизации Google: %s", e)
        raise SystemExit(1) from e

    # Автосоздание таблицы если SPREADSHEET_ID не задан
    spreadsheet_url = ""
    if not spreadsheet_id:
        log.info("SPREADSHEET_ID не задан — пробую создать новую таблицу…")
        bot_name = os.environ.get("BOT_NAME", "Expense Bot")
        try:
            spreadsheet_id, spreadsheet_url = create_new_spreadsheet(
                gs_client=gs_client,
                title=bot_name,
                folder_id=folder_id or None,
                share_email=owner_email or None,
            )
            os.environ["SPREADSHEET_ID"] = spreadsheet_id
            _try_save_spreadsheet_id_to_railway(spreadsheet_id)
        except Exception as e:
            log.error(
                "❌ Не удалось создать Google Таблицу: %s\n"
                "Создайте таблицу вручную:\n"
                "  1. Откройте https://sheets.new\n"
                "  2. Поделитесь с сервисным аккаунтом: %s\n"
                "  3. Задайте переменную SPREADSHEET_ID=<id из URL>",
                e,
                owner_email or "см. credentials.json → client_email",
            )
            # Продолжаем работу без таблицы — бот запустится, но /sheet сообщит об ошибке
    else:
        spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"

    sheets = SheetsClient(
        creds_path, spreadsheet_id or "placeholder", sheet_name,
        config=None, gs_client=gs_client,  # type: ignore
    )
    config = ConfigManager(spreadsheet_id or "placeholder", gs_client)
    sheets.config = config

    if spreadsheet_id:
        sheets.ensure_headers()
        try:
            existing_sessions = sheets.load_sessions()
            PENDING.update(existing_sessions)
        except Exception as e:
            log.warning("Не удалось загрузить сессии: %s", e)
    parser = ExpenseParser(api_key=groq_key, config=config)

    # Извлекаем email сервисного аккаунта для инструкций
    sa_email = ""
    creds_json_env = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    try:
        if creds_json_env:
            sa_email = json.loads(creds_json_env).get("client_email", "")
        elif os.path.exists(creds_path):
            sa_email = json.loads(open(creds_path).read()).get("client_email", "")
    except Exception:
        pass

    app = Application.builder().token(tg_token).build()
    app.bot_data.update({
        "parser": parser,
        "sheets": sheets,
        "config": config,
        "allowed_users": allowed_users,
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_url": spreadsheet_url,
        "service_account_email": sa_email,
    })

    async def _post_init(application: Application) -> None:
        asyncio.create_task(_cleanup_loop(application.bot_data["sheets"]))

    app.post_init = _post_init

    app.add_handler(CommandHandler("syncheaders",  cmd_syncheaders))
    app.add_handler(CommandHandler("resetconfig",  cmd_resetconfig))
    app.add_handler(CommandHandler("menu",        cmd_menu))
    app.add_handler(CommandHandler("sheet",       cmd_sheet))
    app.add_handler(CommandHandler("setsheet",    cmd_setsheet))
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
