"""
Парсер текстового отчёта через Groq LLM.
Принимает свободный текст, возвращает структурированный словарь.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any

from groq import Groq

log = logging.getLogger(__name__)

# Схема полей, которые ждём от LLM.
# Порядок важен — он же используется при записи в Google Sheets.
FIELDS: list[tuple[str, str]] = [
    ("date",              "Дата"),
    ("kaspi",             "Каспи"),
    ("nalichka",          "Наличка"),
    ("halyk",             "Халык"),
    ("perevod",           "Перевод"),
    ("kassir",            "Кассир"),
    ("leads_instagram",   "Инстаграм"),
    ("leads_whatsapp",    "Ватсап"),
    ("leads_whatsapp_ads","Ватсап реклама"),
    ("leads_offline",     "Офлайн"),
    ("leads_regular",     "Постоянные клиенты"),
    ("sales_online",      "Продажи онлайн"),
    ("sales_offline",     "Продажи оффлайн"),
    ("couriers",          "Курьеры"),
    ("purchases",         "Закуп"),
    ("other_expenses",    "Прочие расходы"),
]

HEADERS: list[str] = [ru for _, ru in FIELDS]

SYSTEM_PROMPT = """Ты извлекаешь данные из дневного отчёта магазина в строгий JSON.

Поля (все обязательны, если значения нет — ставь 0 для чисел, пустую строку для текста):
- date: дата в формате YYYY-MM-DD. Если не указана — используй today (подставим в коде).
- kaspi, nalichka, halyk, perevod: суммы по кошелькам (числа, тенге).
- kassir: имя/ФИО того, кто был на кассе.
- leads_instagram, leads_whatsapp, leads_whatsapp_ads, leads_offline, leads_regular: количество лидов по источникам (целые числа).
- sales_online, sales_offline: количество продаж (целые числа).
- couriers, purchases, other_expenses: расходы (числа, тенге).

Правила:
1. Отвечай ТОЛЬКО валидным JSON. Никакого текста вокруг. Без markdown, без ```json.
2. Все числовые поля — числа (integer или float), не строки.
3. Распознавай сокращения: "касп" → kaspi, "нал" → nalichka, "хал" → halyk, "пер" → perevod,
   "инст"/"ig" → leads_instagram, "вц"/"wa" → leads_whatsapp, "реклама" рядом с ватсап → leads_whatsapp_ads,
   "офф"/"оффлайн" в контексте лидов → leads_offline, "пост"/"постоянники" → leads_regular,
   "он"/"онлайн" рядом с "продаж" → sales_online, "офф" рядом с "продаж" → sales_offline,
   "курьер" → couriers, "закуп"/"закупка" → purchases, "проч"/"разное" → other_expenses.
4. Суммы могут быть в формате "150к"/"150000"/"150 000" — приводи к обычному числу (150к = 150000).
5. Если в тексте явно нет какого-то блока — оставляй 0, НЕ выдумывай.
"""


def _today_iso() -> str:
    return date.today().isoformat()


def _coerce_number(v: Any) -> float | int:
    """Пытаемся привести значение к числу. Строки вида '150к' тоже поддерживаем."""
    if isinstance(v, (int, float)):
        return v
    if not isinstance(v, str):
        return 0
    s = v.strip().lower().replace(" ", "").replace(",", ".")
    mul = 1
    if s.endswith("к") or s.endswith("k"):
        mul = 1000
        s = s[:-1]
    if s.endswith("млн") or s.endswith("m"):
        mul = 1_000_000
        s = s.rstrip("млнm")
    try:
        n = float(s) * mul
        return int(n) if n.is_integer() else n
    except ValueError:
        return 0


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Приводим результат LLM к ожидаемой схеме."""
    out: dict[str, Any] = {}

    # Дата
    d = raw.get("date")
    if not d or not isinstance(d, str):
        d = _today_iso()
    else:
        # Пробуем распарсить на случай, если модель вернула другой формат.
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                d = datetime.strptime(d, fmt).date().isoformat()
                break
            except ValueError:
                continue
    out["date"] = d

    # Кассир (строка)
    out["kassir"] = str(raw.get("kassir", "") or "").strip()

    # Все остальные — числа
    for key, _ in FIELDS:
        if key in ("date", "kassir"):
            continue
        out[key] = _coerce_number(raw.get(key, 0))

    return out


class ExpenseParser:
    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile"):
        self.client = Groq(api_key=api_key)
        self.model = model

    def parse(self, text: str) -> dict[str, Any]:
        """Отдаёт словарь с ключами из FIELDS."""
        log.info("parsing text, len=%d", len(text))
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as e:
            log.error("LLM вернул невалидный JSON: %s\n%s", e, content)
            raise ValueError(f"Модель вернула невалидный JSON: {e}") from e

        return _normalize(raw)

    def row_for_sheet(self, parsed: dict[str, Any]) -> list[Any]:
        """Превращает словарь в строку для Google Sheets (в порядке FIELDS)."""
        return [parsed.get(key, "") for key, _ in FIELDS]


def format_preview(parsed: dict[str, Any]) -> str:
    """Человекочитаемый превью для подтверждения в Telegram."""
    lines = [f"*{ru}:* {parsed.get(key, '')}" for key, ru in FIELDS]
    return "\n".join(lines)
