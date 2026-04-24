"""
Парсер текстового отчёта через Groq LLM.
Использует динамические поля из ConfigManager.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, TYPE_CHECKING

from groq import Groq

if TYPE_CHECKING:
    from config_manager import ConfigManager

log = logging.getLogger(__name__)


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
        mul, s = 1000, s[:-1]
    elif s.endswith("млн"):
        mul, s = 1_000_000, s[:-3]
    try:
        n = float(s) * mul
        return int(n) if n.is_integer() else n
    except ValueError:
        return 0


def _build_system_prompt(fields: list[dict]) -> str:
    lines = [
        "Ты извлекаешь данные из отчёта в строгий JSON.\n",
        "Поля (все обязательны; если нет значения — 0 для чисел, пустая строка для текста):",
    ]
    for f in fields:
        t = f["type"]
        key = f["key"]
        label = f["label"]
        if t == "date":
            lines.append(f'- {key}: дата в формате YYYY-MM-DD (если не указана — сегодня)')
        elif t == "text":
            lines.append(f'- {key}: {label} (строка)')
        else:
            lines.append(f'- {key}: {label} (число, тенге или штуки)')

    lines += [
        '- extra_expenses: доп. расходы не попавшие ни в одно поле выше.',
        '  Формат: [{\"name\": \"Название\", \"amount\": число}]. Если нет — [].',
        "",
        "Правила:",
        "1. Отвечай ТОЛЬКО валидным JSON без markdown.",
        "2. Суммы '150к'/'150 000' → число (150к = 150000).",
        "3. Все числовые поля — числа, не строки.",
        "4. Не выдумывай данные которых нет в тексте — ставь 0.",
        "5. Название в extra_expenses — пиши с заглавной буквы.",
    ]
    return "\n".join(lines)


def _normalize(raw: dict[str, Any], fields: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields:
        key, ftype = f["key"], f["type"]
        if ftype == "date":
            d = raw.get(key)
            if not d or not isinstance(d, str):
                d = _today_iso()
            else:
                for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
                    try:
                        d = datetime.strptime(d, fmt).date().isoformat()
                        break
                    except ValueError:
                        continue
            out[key] = d
        elif ftype == "text":
            out[key] = str(raw.get(key, "") or "").strip()
        else:
            out[key] = _coerce_number(raw.get(key, 0))

    # Дополнительные расходы
    extra = raw.get("extra_expenses", [])
    cleaned = []
    for item in (extra if isinstance(extra, list) else []):
        if isinstance(item, dict) and item.get("name"):
            name = str(item["name"]).strip()
            name = name[0].upper() + name[1:] if name else name
            cleaned.append({"name": name, "amount": _coerce_number(item.get("amount", 0))})
    out["extra_expenses"] = cleaned
    return out


def format_preview(parsed: dict[str, Any], fields: list[dict]) -> str:
    lines = [f"*{f['label']}:* {parsed.get(f['key'], '')}" for f in fields]
    extra = parsed.get("extra_expenses", [])
    if extra:
        lines.append("\n*Доп. расходы:*")
        for item in extra:
            lines.append(f"  • {item['name']}: {item['amount']:,}")
    return "\n".join(lines)


class ExpenseParser:
    def __init__(self, api_key: str, config: "ConfigManager",
                 model: str = "llama-3.3-70b-versatile"):
        self.client = Groq(api_key=api_key)
        self.model = model
        self.config = config

    def _call_llm(self, text: str) -> dict[str, Any]:
        prompt = _build_system_prompt(self.config.fields)
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user",   "content": text},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"Модель вернула невалидный JSON: {e}") from e

    def parse(self, text: str) -> dict[str, Any]:
        text = self.config.apply_aliases(text)
        log.info("parsing text, len=%d", len(text))
        return _normalize(self._call_llm(text), self.config.fields)

    def parse_correction(self, original: dict[str, Any], correction: str) -> dict[str, Any]:
        correction = self.config.apply_aliases(correction)
        raw = _normalize(self._call_llm(correction), self.config.fields)
        merged = dict(original)

        for f in self.config.fields:
            key, ftype = f["key"], f["type"]
            if ftype == "date":
                if raw.get(key) and raw[key] != _today_iso():
                    merged[key] = raw[key]
            elif ftype == "text":
                if raw.get(key):
                    merged[key] = raw[key]
            else:
                if raw.get(key, 0) != 0:
                    merged[key] = raw[key]

        correction_extra = raw.get("extra_expenses", [])
        if correction_extra:
            existing = {e["name"]: e for e in merged.get("extra_expenses", [])}
            for item in correction_extra:
                existing[item["name"]] = item
            merged["extra_expenses"] = list(existing.values())
        return merged

    def row_for_sheet(self, parsed: dict[str, Any]) -> list[Any]:
        return [parsed.get(f["key"], "") for f in self.config.fields]
