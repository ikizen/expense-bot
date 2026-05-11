"""
Парсер текстового отчёта через Groq LLM.
Использует динамические поля из ConfigManager.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime
from typing import Any, TYPE_CHECKING

from groq import Groq, RateLimitError

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
    today = date.today().isoformat()
    current_year = date.today().year
    lines = [
        f"Сегодня: {today}. Ты извлекаешь данные из отчёта в строгий JSON.\n",
        "Поля (все обязательны; если нет значения — 0 для чисел, пустая строка для текста):",
    ]
    for f in fields:
        t = f["type"]
        key = f["key"]
        label = f["label"]
        if t == "date":
            lines.append(
                f'- {key}: дата в формате YYYY-MM-DD. '
                f'Если год не указан — используй {current_year}. '
                f'Если дата не указана вообще — используй {today}.'
            )
        elif t == "text":
            lines.append(f'- {key}: {label} (строка)')
        else:
            lines.append(f'- {key}: {label} (число, тенге или штуки)')

    lines += [
        '- extra_expenses: доп. расходы не попавшие ни в одно поле выше.',
        '  Формат: [{"name": "Название", "amount": число}]. Если нет — [].',
        "",
        "Правила:",
        "1. Отвечай ТОЛЬКО валидным JSON без markdown.",
        "2. Суммы '150к'/'150 000' → число (150к = 150000).",
        "3. Все числовые поля — числа, не строки.",
        "4. Не выдумывай данные которых нет в тексте — ставь 0.",
        "5. Название в extra_expenses — пиши с заглавной буквы.",
        "6. Если одно название расхода встречается несколько раз — суммируй суммы в один элемент.",
        "7. '—', '-', 'нет' означают 0 или пустую строку.",
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
                # FIX #1: если ни один формат не сработал — fallback к today
                parsed_ok = False
                for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
                    try:
                        d = datetime.strptime(d, fmt).date().isoformat()
                        parsed_ok = True
                        break
                    except ValueError:
                        continue
                if not parsed_ok:
                    log.warning("Не удалось распознать дату %r — использую сегодня", d)
                    d = _today_iso()
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

    # Схлопываем дубли extra_expenses (суммируем одинаковые name)
    seen: dict[str, dict] = {}
    for item in cleaned:
        name = item["name"]
        if name in seen:
            seen[name]["amount"] += item["amount"]
        else:
            seen[name] = dict(item)
    out["extra_expenses"] = list(seen.values())

    # Текстовая копия — "Сирень: 28 000; Таргет: 50 000"
    out["extras_text"] = _extras_to_text(out["extra_expenses"])
    return out


def _extras_to_text(extras: list[dict]) -> str:
    """Форматирует доп. расходы в одну строку для ячейки таблицы."""
    if not extras:
        return ""
    parts = []
    for item in extras:
        amount_str = f"{item['amount']:,}".replace(",", " ")
        parts.append(f"{item['name']}: {amount_str}")
    return "; ".join(parts)


def format_preview(parsed: dict[str, Any], fields: list[dict],
                   sheet_headers: list[str] | None = None) -> str:
    """Формирует текст предпросмотра.
    Дата показывается всегда; числа и текст — только если не пустые/не ноль.
    sheet_headers игнорируется — показываем всё ненулевое (оно будет записано)."""
    lines = []
    for f in fields:
        if f["key"] == "extras_text":
            continue
        val = parsed.get(f["key"], "")
        # Дата — всегда, остальное — только если есть значение
        if f["key"] != "date":
            if f["type"] == "number" and not val:
                continue
            if f["type"] == "text" and not str(val).strip():
                continue
        lines.append(f"*{f['label']}:* {val}")

    extra = parsed.get("extra_expenses", [])
    if extra:
        lines.append("\n*Доп. расходы:*")
        for item in extra:
            amount_str = f"{item['amount']:,}".replace(",", " ")
            lines.append(f"  • {item['name']}: {amount_str}")

    return "\n".join(lines) or "_(данные не распознаны)_"


class ExpenseParser:
    def __init__(self, api_key: str, config: "ConfigManager",
                 model: str | None = None):
        # FIX #11: timeout 30 сек; FIX #15: модель из env-переменной
        self.client = Groq(api_key=api_key, timeout=30.0)
        self.model = model or os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.config = config

    def _call_llm(self, text: str, retries: int = 2) -> dict[str, Any]:
        """FIX #6: retry при rate-limit с экспоненциальной задержкой."""
        prompt = _build_system_prompt(self.config.fields)
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
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
            except RateLimitError as e:
                last_err = e
                if attempt < retries:
                    wait = 5 * (attempt + 1)
                    log.warning("Groq rate-limit (попытка %d/%d), жду %dс…", attempt + 1, retries + 1, wait)
                    time.sleep(wait)
                else:
                    raise ValueError("Groq перегружен, попробуй через минуту.") from e
            except Exception:
                raise
        raise last_err  # type: ignore[misc]

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
        merged["extras_text"] = _extras_to_text(merged.get("extra_expenses", []))
        return merged

    def row_for_sheet(self, parsed: dict[str, Any]) -> list[Any]:
        return [parsed.get(f["key"], "") for f in self.config.fields]
