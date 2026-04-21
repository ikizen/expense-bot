"""
Парсер текстового отчёта через Groq LLM.
Принимает свободный текст, возвращает структурированный словарь.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from groq import Groq

log = logging.getLogger(__name__)

# Фиксированные поля. Порядок = порядок колонок в Sheets.
FIELDS: list[tuple[str, str]] = [
    ("date",               "Дата"),
    ("kaspi",              "Каспи"),
    ("nalichka",           "Наличка"),
    ("halyk",              "Халык"),
    ("perevod",            "Перевод"),
    ("kassir",             "Кассир"),
    ("leads_instagram",    "Инстаграм"),
    ("leads_whatsapp",     "Ватсап"),
    ("leads_whatsapp_ads", "Ватсап реклама"),
    ("leads_offline",      "Офлайн"),
    ("leads_regular",      "Постоянные клиенты"),
    ("sales_online",       "Продажи онлайн"),
    ("sales_offline",      "Продажи оффлайн"),
    ("couriers",           "Курьеры"),
    ("purchases",          "Закуп"),
    ("other_expenses",     "Прочие расходы"),
]

HEADERS: list[str] = [ru for _, ru in FIELDS]

SYSTEM_PROMPT = """Ты извлекаешь данные из дневного отчёта магазина в строгий JSON.

Поля (все обязательны, если значения нет — ставь 0 для чисел, пустую строку для текста):
- date: дата в формате YYYY-MM-DD. Если не указана — используй сегодня.
- kaspi, nalichka, halyk, perevod: суммы по кошелькам (числа, тенге).
- kassir: имя/ФИО того, кто был на кассе.
- leads_instagram, leads_whatsapp, leads_whatsapp_ads, leads_offline, leads_regular: количество лидов по источникам (целые числа).
- sales_online, sales_offline: количество продаж (целые числа).
- couriers, purchases, other_expenses: расходы (числа, тенге).
- extra_expenses: список ДОПОЛНИТЕЛЬНЫХ расходов, которые не подходят ни под одну из трёх категорий выше.
  Формат: [{"name": "Название", "amount": число}, ...]. Если нет — пустой список [].

Правила:
1. Отвечай ТОЛЬКО валидным JSON. Никакого текста вокруг. Без markdown, без ```json.
2. Все числовые поля — числа (integer или float), не строки.
3. Распознавай сокращения: "касп" → kaspi, "нал" → nalichka, "хал" → halyk, "пер" → perevod,
   "инст"/"ig" → leads_instagram, "вц"/"wa" → leads_whatsapp, "реклама" рядом с ватсап → leads_whatsapp_ads,
   "офф"/"оффлайн" в контексте лидов → leads_offline, "пост"/"постоянники" → leads_regular,
   "он"/"онлайн" рядом с "продаж" → sales_online, "офф" рядом с "продаж" → sales_offline,
   "курьер" → couriers, "закуп"/"закупка" → purchases, "проч"/"разное" → other_expenses.
4. Суммы могут быть в формате "150к"/"150000"/"150 000" — приводи к числу (150к = 150000).
5. Если расход явно называется чем-то конкретным (аренда, зарплата, реклама, налог и т.д.)
   и не является курьерами/закупом/прочим — помещай в extra_expenses.
6. Если в тексте явно нет какого-то поля — оставляй 0, НЕ выдумывай.
"""


def _today_iso() -> str:
    return date.today().isoformat()


def _coerce_number(v: Any) -> float | int:
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
    out: dict[str, Any] = {}

    # Дата
    d = raw.get("date")
    if not d or not isinstance(d, str):
        d = _today_iso()
    else:
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                d = datetime.strptime(d, fmt).date().isoformat()
                break
            except ValueError:
                continue
    out["date"] = d

    # Кассир
    out["kassir"] = str(raw.get("kassir", "") or "").strip()

    # Стандартные числовые поля
    for key, _ in FIELDS:
        if key in ("date", "kassir"):
            continue
        out[key] = _coerce_number(raw.get(key, 0))

    # Дополнительные расходы
    extra = raw.get("extra_expenses", [])
    if not isinstance(extra, list):
        extra = []
    cleaned = []
    for item in extra:
        if isinstance(item, dict) and item.get("name"):
            cleaned.append({
                "name": str(item["name"]).strip(),
                "amount": _coerce_number(item.get("amount", 0)),
            })
    out["extra_expenses"] = cleaned

    return out


def format_preview(parsed: dict[str, Any]) -> str:
    lines = [f"*{ru}:* {parsed.get(key, '')}" for key, ru in FIELDS]
    extra = parsed.get("extra_expenses", [])
    if extra:
        lines.append("\n*Дополнительные расходы:*")
        for item in extra:
            lines.append(f"  • {item['name']}: {item['amount']}")
    return "\n".join(lines)


class ExpenseParser:
    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile"):
        self.client = Groq(api_key=api_key)
        self.model = model

    def _call_llm(self, text: str) -> dict[str, Any]:
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
            return json.loads(content)
        except json.JSONDecodeError as e:
            log.error("LLM вернул невалидный JSON: %s\n%s", e, content)
            raise ValueError(f"Модель вернула невалидный JSON: {e}") from e

    def parse(self, text: str) -> dict[str, Any]:
        log.info("parsing text, len=%d", len(text))
        return _normalize(self._call_llm(text))

    def parse_correction(self, original: dict[str, Any], correction_text: str) -> dict[str, Any]:
        """Парсит поправку и мёрджит поверх оригинала — перезаписывает только упомянутые поля."""
        raw_correction = _normalize(self._call_llm(correction_text))
        merged = dict(original)

        for key, _ in FIELDS:
            if key == "date":
                # Обновляем дату только если она явно упомянута в поправке
                if raw_correction["date"] != _today_iso():
                    merged["date"] = raw_correction["date"]
            elif key == "kassir":
                if raw_correction["kassir"]:
                    merged["kassir"] = raw_correction["kassir"]
            else:
                if raw_correction.get(key, 0) != 0:
                    merged[key] = raw_correction[key]

        # Мёрджим extra_expenses по имени
        correction_extra = raw_correction.get("extra_expenses", [])
        if correction_extra:
            existing = {e["name"]: e for e in merged.get("extra_expenses", [])}
            for item in correction_extra:
                existing[item["name"]] = item
            merged["extra_expenses"] = list(existing.values())

        return merged

    def row_for_sheet(self, parsed: dict[str, Any]) -> list[Any]:
        return [parsed.get(key, "") for key, _ in FIELDS]
