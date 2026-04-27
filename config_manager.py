"""
Управление конфигурацией бота.
Настройки хранятся в листе _config Google Sheets — переживают перезапуски Railway.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import gspread

log = logging.getLogger(__name__)

CONFIG_SHEET = "_config"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "fields": [
        {"key": "date",               "label": "Дата",                "type": "date"},
        {"key": "kaspi",              "label": "Каспи",               "type": "number"},
        {"key": "nalichka",           "label": "Наличка",             "type": "number"},
        {"key": "halyk",              "label": "Халык",               "type": "number"},
        {"key": "perevod",            "label": "Перевод",             "type": "number"},
        {"key": "kassir",             "label": "Кассир",              "type": "text"},
        {"key": "leads_instagram",    "label": "Инстаграм",           "type": "number"},
        {"key": "leads_whatsapp",     "label": "Ватсап",              "type": "number"},
        {"key": "leads_whatsapp_ads", "label": "Ватсап реклама",      "type": "number"},
        {"key": "leads_offline",      "label": "Офлайн лиды",         "type": "number"},
        {"key": "leads_regular",      "label": "Постоянные клиенты",  "type": "number"},
        {"key": "sales_online",       "label": "Продажи онлайн",      "type": "number"},
        {"key": "sales_offline",      "label": "Продажи оффлайн",     "type": "number"},
        {"key": "couriers",           "label": "Курьеры",             "type": "number"},
        {"key": "purchases",          "label": "Закуп",               "type": "number"},
        {"key": "other_expenses",     "label": "Прочие расходы",      "type": "number"},
    ],
    "aliases": {},          # слово_нижн_регистр → целевая метка
    "triggers": ["отчет", "отчёт"],   # слова-триггеры в тексте
    "routes": {},           # ключевое_слово → название листа
}

# Поля, которые нельзя удалить
PROTECTED_KEYS = {"date", "kassir"}


class ConfigManager:
    def __init__(self, spreadsheet_id: str, gs_client: gspread.Client):
        self._spreadsheet_id = spreadsheet_id
        self._gs = gs_client
        self._cfg: dict[str, Any] = {}
        # FIX #16: кэш скомпилированного паттерна триггеров
        self._trigger_pattern_cache: str | None = None
        self.load()

    # ── Загрузка / сохранение ──────────────────────────────────────────────

    def load(self) -> None:
        try:
            sh = self._gs.open_by_key(self._spreadsheet_id)
            try:
                ws = sh.worksheet(CONFIG_SHEET)
                raw = ws.acell("A1").value
                if raw:
                    self._cfg = json.loads(raw)
                    self._trigger_pattern_cache = None  # инвалидируем кэш
                    log.info("Конфиг загружен из Google Sheets")
                    return
            except gspread.WorksheetNotFound:
                pass
        except Exception as e:
            log.warning("Не удалось загрузить конфиг: %s", e)
        # Первый запуск — используем дефолт
        self._cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        self.save()

    def save(self) -> None:
        """FIX #2: raise при ошибке сохранения — вызывающий код узнает о проблеме."""
        self._trigger_pattern_cache = None  # инвалидируем кэш при каждом сохранении
        try:
            sh = self._gs.open_by_key(self._spreadsheet_id)
            try:
                ws = sh.worksheet(CONFIG_SHEET)
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(CONFIG_SHEET, rows=5, cols=2)
            ws.update("A1", [[json.dumps(self._cfg, ensure_ascii=False, indent=2)]])
            log.info("Конфиг сохранён")
        except Exception as e:
            log.error("Не удалось сохранить конфиг: %s", e)
            raise  # FIX #2: пробрасываем дальше — молча не теряем данные

    # ── Свойства ───────────────────────────────────────────────────────────

    @property
    def fields(self) -> list[dict]:
        return self._cfg.get("fields", DEFAULT_CONFIG["fields"])

    @property
    def headers(self) -> list[str]:
        return [f["label"] for f in self.fields]

    @property
    def aliases(self) -> dict[str, str]:
        return self._cfg.get("aliases", {})

    @property
    def triggers(self) -> list[str]:
        return self._cfg.get("triggers", DEFAULT_CONFIG["triggers"])

    @property
    def routes(self) -> dict[str, str]:
        return self._cfg.get("routes", {})

    def trigger_pattern(self) -> str:
        # FIX #16: кэшируем паттерн — не пересобираем на каждое сообщение
        if self._trigger_pattern_cache is None:
            self._trigger_pattern_cache = "|".join(re.escape(t) for t in self.triggers)
        return self._trigger_pattern_cache

    def detect_sheet(self, text: str, default: str) -> str:
        """Возвращает название листа по ключевым словам в тексте."""
        for keyword, sheet in self.routes.items():
            if re.search(re.escape(keyword), text, re.IGNORECASE):
                return sheet
        return default

    # ── Поля ──────────────────────────────────────────────────────────────

    def add_field(self, label: str, field_type: str = "number") -> bool:
        """Добавляет поле. False если уже существует."""
        key = re.sub(r"[^a-zа-яё0-9]+", "_", label.lower()).strip("_")
        for f in self.fields:
            if f["label"].lower() == label.lower():
                return False
        self._cfg["fields"].append({"key": key, "label": label, "type": field_type})
        self.save()
        return True

    def remove_field(self, label: str) -> str:
        """Удаляет поле. Возвращает 'ok', 'protected' или 'not_found'."""
        for i, f in enumerate(self._cfg["fields"]):
            if f["label"].lower() == label.lower():
                if f["key"] in PROTECTED_KEYS:
                    return "protected"
                self._cfg["fields"].pop(i)
                self.save()
                return "ok"
        return "not_found"

    # ── Алиасы ────────────────────────────────────────────────────────────

    def add_alias(self, word: str, target: str) -> None:
        self._cfg.setdefault("aliases", {})[word.lower()] = target
        self.save()

    def remove_alias(self, word: str) -> bool:
        aliases = self._cfg.get("aliases", {})
        if word.lower() in aliases:
            del aliases[word.lower()]
            self.save()
            return True
        return False

    def apply_aliases(self, text: str) -> str:
        """Заменяет слова-алиасы в тексте на целевые метки."""
        for word, target in self.aliases.items():
            text = re.sub(re.escape(word), target, text, flags=re.IGNORECASE)
        return text

    # ── Маршруты ──────────────────────────────────────────────────────────

    def add_route(self, keyword: str, sheet_name: str) -> None:
        self._cfg.setdefault("routes", {})[keyword.lower()] = sheet_name
        self.save()

    def remove_route(self, keyword: str) -> bool:
        routes = self._cfg.get("routes", {})
        if keyword.lower() in routes:
            del routes[keyword.lower()]
            self.save()
            return True
        return False
