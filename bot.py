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
    sheets._ws_cache.clear()

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
        "/sheet — ссылка на текущую таблицу"
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

    elif action == "newsheet_col":
        # Добавить столбцы в список нового листа — каждая строка = отдельный столбец
        labels = [l.strip() for l in text.splitlines() if l.strip()]
        if user_id in NEW_SHEET_PENDING and labels:
            NEW_SHEET_PENDING[user_id].setdefault("new_cols", []).extend(labels)
            state2 = NEW_SHEET_PENDING[user_id]
            added = ", ".join(f"<b>{html.escape(l)}</b>" for l in labels)
            await _back_to(
                f"📋 Новый лист: <b>{html.escape(state2['name'])}</b>\n\n"
                f"Выбери столбцы:\n\n✅ Добавлено: {added}",
                _newsheet_keyboard(user_id, cfg),
            )
        else:
            await update.message.reply_text("⚠️ Название не может быть пустым.")


# ── Мастер создания нового листа ──────────────────────────────────────────

def _newsheet_keyboard(user_id: int, cfg: ConfigManager) -> InlineKeyboardMarkup:
    """Клавиатура выбора столбцов для нового листа."""
    state = NEW_SHEET_PENDING.get(user_id, {})
    selected: set[str] = state.get("selected", set())
    new_cols: list[str] = state.get("new_cols", [])
    fields = cfg.fields

    # Существующие поля — по 2 в ряд
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for i, f in enumerate(fields):
        label = f["label"]
        mark = "✅" if label in selected else "☐"
        btn = InlineKeyboardButton(
            f"{mark} {label}",
            callback_data=f"nst:{user_id}:{i}",  # индекс, не ключ
        )
        pair.append(btn)
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    # Новые пользовательские столбцы — каждый с кнопкой ➖ удалить
    for idx, col in enumerate(new_cols):
        rows.append([
            InlineKeyboardButton(f"✅ {col}", callback_data="noop"),
            InlineKeyboardButton("➖", callback_data=f"nsdc:{user_id}:{idx}"),
        ])

    selected_count = len(selected) + len(new_cols)
    total_count = len(fields) + len(new_cols)
    rows.append([
        InlineKeyboardButton("➕ Добавить столбец", callback_data=f"nsa:{user_id}"),
    ])
    rows.append([
        InlineKeyboardButton(
            f"✅ Создать лист ({selected_count}/{total_count})",
            callback_data=f"nsc:{user_id}",
        ),
        InlineKeyboardButton("❌ Отмена", callback_data=f"nsx:{user_id}"),
    ])
    return InlineKeyboardMarkup(rows)


async def cmd_newsheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context.bot_data["allowed_users"]):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /newsheet Название")
        return
    name = " ".join(args).strip()
    # FIX #10: Google Sheets ограничивает имя листа 100 символами
    if not name:
        await update.message.reply_text("❌ Название не может быть пустым.")
        return
    if len(name) > 100:
        await update.message.reply_text(
            f"❌ Название слишком длинное ({len(name)} символов). Максимум — 100."
        )
        return
    cfg: ConfigManager = context.bot_data["config"]
    user_id = update.effective_user.id

    # Инициализируем состояние — все столбцы выбраны по умолчанию
    NEW_SHEET_PENDING[user_id] = {
        "name": name,
        "selected": {f["label"] for f in cfg.fields},
        "new_cols": [],   # новые столбцы добавленные прямо здесь
        "ts": time.time(),  # FIX #4/#21: для TTL-очистки
    }

    await update.message.reply_text(
        f"📋 Новый лист: <b>{html.escape(name)}</b>\n\n"
        "Выбери столбцы (все выбраны — нажимай чтобы убрать лишние):",
        reply_markup=_newsheet_keyboard(user_id, cfg),
        parse_mode=ParseMode.HTML,
    )


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

    token = uuid.uuid4().hex[:12]
    # FIX #4: сохраняем время создания для TTL-очистки
    PENDING[token] = {"data": parsed, "sheet": sheet_name, "ts": time.time()}

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
    if data.startswith("nst:"):        # toggle столбца
        _, uid_str, idx_str = data.split(":", 2)
        uid = int(uid_str)
        if uid not in NEW_SHEET_PENDING:
            await query.edit_message_text("Сессия истекла. Запусти /newsheet заново.")
            return
        state = NEW_SHEET_PENDING[uid]
        idx = int(idx_str)
        label = cfg.fields[idx]["label"] if idx < len(cfg.fields) else None
        if label:
            if label in state["selected"]:
                state["selected"].discard(label)
            else:
                state["selected"].add(label)
        await query.edit_message_reply_markup(
            reply_markup=_newsheet_keyboard(uid, cfg)
        )
        return

    if data.startswith("nsc:"):        # создать лист
        uid = int(data.split(":", 1)[1])
        state = NEW_SHEET_PENDING.pop(uid, None)
        if not state:
            await query.edit_message_text("Сессия истекла. Запусти /newsheet заново.")
            return
        name = state["name"]
        selected_labels = state["selected"]
        new_cols: list[str] = state.get("new_cols", [])
        if not selected_labels and not new_cols:
            await query.answer("Выбери хотя бы один столбец!", show_alert=True)
            NEW_SHEET_PENDING[uid] = state
            return

        # Сортируем по порядку конфига, затем новые столбцы в конце
        ordered = [f["label"] for f in cfg.fields if f["label"] in selected_labels]
        ordered += new_cols   # новые столбцы идут после стандартных
        sheets: SheetsClient = context.bot_data["sheets"]
        try:
            created = await asyncio.to_thread(sheets.create_sheet, name, ordered)
        except Exception as e:
            await query.edit_message_text(f"Ошибка: {html.escape(str(e))}")
            return
        if created:
            cols_text = " | ".join(ordered)
            await query.edit_message_text(
                f"✅ Лист <b>{html.escape(name)}</b> создан!\n\n"
                f"<b>Столбцы:</b> {html.escape(cols_text)}\n\n"
                f"Чтобы отправлять отчёты туда:\n"
                f"/addroute ключевоеслово {html.escape(name)}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(
                f"Лист <b>{html.escape(name)}</b> уже существует.",
                parse_mode=ParseMode.HTML,
            )
        return

    if data.startswith("nsx:"):        # отмена
        uid = int(data.split(":", 1)[1])
        NEW_SHEET_PENDING.pop(uid, None)
        SETTINGS_WAITING.pop(uid, None)
        await query.edit_message_text("Отменено.")
        return

    if data.startswith("nsa:"):        # добавить новый столбец
        uid = int(data.split(":", 1)[1])
        if uid not in NEW_SHEET_PENDING:
            await query.edit_message_text("Сессия истекла. Запусти /newsheet заново.")
            return
        SETTINGS_WAITING[uid] = {
            "action": "newsheet_col",
            "chat_id": query.message.chat_id,
            "msg_id": query.message.message_id,
        }
        await query.edit_message_text(
            f"📋 Лист: <b>{html.escape(NEW_SHEET_PENDING[uid]['name'])}</b>\n\n"
            "✏️ Введи название нового столбца:\n"
            "<i>Просто напиши в чат</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data=f"nsac:{uid}"),
            ]]),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("nsac:"):       # отмена ввода столбца
        uid = int(data.split(":", 1)[1])
        SETTINGS_WAITING.pop(uid, None)
        if uid in NEW_SHEET_PENDING:
            cfg2: ConfigManager = context.bot_data["config"]
            state2 = NEW_SHEET_PENDING[uid]
            await query.edit_message_text(
                f"📋 Новый лист: <b>{html.escape(state2['name'])}</b>\n\n"
                "Выбери столбцы:",
                reply_markup=_newsheet_keyboard(uid, cfg2),
                parse_mode=ParseMode.HTML,
            )
        return

    if data.startswith("nsdc:"):       # удалить новый столбец из списка
        _, uid_str, idx_str = data.split(":", 2)
        uid = int(uid_str)
        idx = int(idx_str)
        if uid in NEW_SHEET_PENDING:
            cols = NEW_SHEET_PENDING[uid].get("new_cols", [])
            if 0 <= idx < len(cols):
                cols.pop(idx)
            cfg3: ConfigManager = context.bot_data["config"]
            state3 = NEW_SHEET_PENDING[uid]
            await query.edit_message_reply_markup(
                reply_markup=_newsheet_keyboard(uid, cfg3)
            )
        return

    if data == "noop":
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
            await query.edit_message_text(
                "Напиши команду:\n<code>/newsheet Название</code>\n\n"
                "Например: <code>/newsheet Финансы</code>\n\n"
                "После этого выберешь столбцы кнопками.",
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


# FIX #4: периодическая очистка протухших сессий (TTL = 1 час)
async def cleanup_stale_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
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

    # FIX #4: очищаем протухшие сессии каждые 30 минут
    app.job_queue.run_repeating(cleanup_stale_sessions, interval=1800, first=1800)

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
