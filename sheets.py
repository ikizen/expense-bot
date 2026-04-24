"""
Клиент для работы с Google Sheets.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from typing import Any, TYPE_CHECKING

import gspread
from google.oauth2.service_account import Credentials

if TYPE_CHECKING:
    from config_manager import ConfigManager

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _build_gs_client(creds_path: str) -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    elif os.path.exists(creds_path):
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    else:
        raise FileNotFoundError(
            f"credentials.json не найден: {creds_path}. "
            "Задай GOOGLE_CREDENTIALS_JSON или положи файл рядом с bot.py."
        )
    return gspread.authorize(creds)


class SheetsClient:
    def __init__(self, creds_path: str, spreadsheet_id: str,
                 sheet_name: str, config: "ConfigManager"):
        self.client = _build_gs_client(creds_path)
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.config = config
        self._ws_cache: dict[str, gspread.Worksheet] = {}

    def _get_ws(self, sheet_name: str) -> gspread.Worksheet:
        if sheet_name not in self._ws_cache:
            sh = self.client.open_by_key(self.spreadsheet_id)
            try:
                ws = sh.worksheet(sheet_name)
            except gspread.WorksheetNotFound:
                log.info("Лист '%s' не найден — создаю.", sheet_name)
                ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=30)
            self._ws_cache[sheet_name] = ws
        return self._ws_cache[sheet_name]

    def ensure_headers(self, sheet_name: str | None = None) -> None:
        name = sheet_name or self.sheet_name
        ws = self._get_ws(name)
        if not ws.row_values(1):
            ws.update("A1", [self.config.headers])
            ws.format("A1:Z1", {"textFormat": {"bold": True}})
            log.info("Заголовки записаны на листе '%s'.", name)

    def create_sheet(self, sheet_name: str) -> bool:
        """Создаёт новый лист. False если уже существует."""
        sh = self.client.open_by_key(self.spreadsheet_id)
        try:
            sh.worksheet(sheet_name)
            return False
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=30)
            ws.update("A1", [self.config.headers])
            ws.format("A1:Z1", {"textFormat": {"bold": True}})
            self._ws_cache[sheet_name] = ws
            return True

    def _get_or_create_col(self, ws: gspread.Worksheet,
                           headers: list[str], name: str) -> int:
        if name in headers:
            return headers.index(name) + 1
        new_col = len(headers) + 1
        ws.update_cell(1, new_col, name)
        ws.format(gspread.utils.rowcol_to_a1(1, new_col),
                  {"textFormat": {"bold": True}})
        headers.append(name)
        return new_col

    def append_row(self, row: list[Any],
                   extra_expenses: list[dict] | None = None,
                   sheet_name: str | None = None) -> int:
        target = sheet_name or self.sheet_name
        self.ensure_headers(target)
        ws = self._get_ws(target)
        headers = ws.row_values(1)

        if not extra_expenses:
            ws.append_row(row, value_input_option="USER_ENTERED")
            return len(ws.get_all_values())

        all_values = ws.get_all_values()
        next_row = len(all_values) + 1
        cells = [gspread.Cell(next_row, i + 1, v) for i, v in enumerate(row)]

        for item in extra_expenses:
            name = item.get("name", "").strip()
            amount = item.get("amount", 0)
            if not name:
                continue
            col = self._get_or_create_col(ws, headers, name)
            cells.append(gspread.Cell(next_row, col, amount))

        ws.update_cells(cells, value_input_option="USER_ENTERED")
        return next_row

    # ── Dashboard ──────────────────────────────────────────────────────────

    def get_stats(self, sheet_name: str | None = None, days: int = 7) -> dict:
        """Возвращает агрегаты за последние N дней."""
        ws = self._get_ws(sheet_name or self.sheet_name)
        rows = ws.get_all_values()
        if not rows:
            return {}

        headers = rows[0]
        cutoff = date.today() - timedelta(days=days - 1)
        totals: dict[str, float] = {}
        count = 0

        for row in rows[1:]:
            if not row or not row[0]:
                continue
            # Пробуем разобрать дату
            raw_date = row[0]
            parsed_date = None
            for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                try:
                    parsed_date = datetime.strptime(raw_date, fmt).date()
                    break
                except ValueError:
                    continue
            if parsed_date is None or parsed_date < cutoff:
                continue

            count += 1
            for i, val in enumerate(row):
                if i >= len(headers):
                    break
                col = headers[i]
                if col == headers[0]:  # date column
                    continue
                try:
                    n = float(str(val).replace(" ", "").replace(",", "."))
                    totals[col] = totals.get(col, 0) + n
                except (ValueError, TypeError):
                    pass  # text columns

        return {"days": days, "count": count, "totals": totals, "headers": headers}


# Нужен для обратной совместимости импорта в config_manager
from datetime import datetime
