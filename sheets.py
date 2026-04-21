"""
Клиент для записи отчётов в Google Sheets через service account.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from parser import HEADERS

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetsClient:
    def __init__(self, creds_path: str, spreadsheet_id: str, sheet_name: str):
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            info = json.loads(creds_json)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        elif os.path.exists(creds_path):
            creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        else:
            raise FileNotFoundError(
                f"credentials.json не найден по пути {creds_path}. "
                "Скачай его из Google Cloud Console → Service Accounts."
            )
        self.client = gspread.authorize(creds)
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self._worksheet: gspread.Worksheet | None = None

    def _get_worksheet(self, sheet_name: str) -> gspread.Worksheet:
        sh = self.client.open_by_key(self.spreadsheet_id)
        try:
            return sh.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            log.info("Лист '%s' не найден — создаю.", sheet_name)
            return sh.add_worksheet(title=sheet_name, rows=1000, cols=max(20, len(HEADERS)))

    @property
    def worksheet(self) -> gspread.Worksheet:
        if self._worksheet is None:
            self._worksheet = self._get_worksheet(self.sheet_name)
        return self._worksheet

    def ensure_headers(self, sheet_name: str | None = None) -> None:
        ws = self._get_worksheet(sheet_name or self.sheet_name)
        first_row = ws.row_values(1)
        if not first_row:
            ws.update("A1", [HEADERS])
            ws.format("A1:Z1", {"textFormat": {"bold": True}})
            log.info("Заголовки записаны на листе '%s'.", sheet_name or self.sheet_name)

    def _get_or_create_col(self, ws: gspread.Worksheet, headers: list[str], name: str) -> int:
        """Возвращает 1-based индекс колонки, создаёт если не существует."""
        if name in headers:
            return headers.index(name) + 1
        new_col = len(headers) + 1
        ws.update_cell(1, new_col, name)
        ws.format(
            gspread.utils.rowcol_to_a1(1, new_col),
            {"textFormat": {"bold": True}},
        )
        headers.append(name)
        log.info("Добавлена новая колонка: %s", name)
        return new_col

    def append_row(self, row: list[Any], extra_expenses: list[dict] | None = None, sheet_name: str | None = None) -> int:
        """Добавляет строку. extra_expenses динамически создаёт колонки при необходимости."""
        target = sheet_name or self.sheet_name
        self.ensure_headers(target)
        ws = self._get_worksheet(target)
        headers = ws.row_values(1)

        if not extra_expenses:
            ws.append_row(row, value_input_option="USER_ENTERED")
            return len(ws.get_all_values())

        # Нужны динамические колонки — пишем по ячейкам
        all_values = ws.get_all_values()
        next_row = len(all_values) + 1

        # Стандартные поля
        cell_updates = []
        for col_idx, value in enumerate(row, start=1):
            cell_updates.append(gspread.Cell(next_row, col_idx, value))

        # Дополнительные расходы
        for item in extra_expenses:
            name = item.get("name", "").strip()
            amount = item.get("amount", 0)
            if not name:
                continue
            col_idx = self._get_or_create_col(ws, headers, name)
            cell_updates.append(gspread.Cell(next_row, col_idx, amount))

        ws.update_cells(cell_updates, value_input_option="USER_ENTERED")
        return next_row

