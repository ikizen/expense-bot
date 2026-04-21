"""
Клиент для записи отчётов в Google Sheets через service account.
"""
from __future__ import annotations

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
        if not os.path.exists(creds_path):
            raise FileNotFoundError(
                f"credentials.json не найден по пути {creds_path}. "
                "Скачай его из Google Cloud Console → Service Accounts."
            )
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        self.client = gspread.authorize(creds)
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self._worksheet: gspread.Worksheet | None = None

    @property
    def worksheet(self) -> gspread.Worksheet:
        if self._worksheet is None:
            sh = self.client.open_by_key(self.spreadsheet_id)
            try:
                ws = sh.worksheet(self.sheet_name)
            except gspread.WorksheetNotFound:
                log.info("Лист '%s' не найден — создаю.", self.sheet_name)
                ws = sh.add_worksheet(title=self.sheet_name, rows=1000, cols=max(20, len(HEADERS)))
            self._worksheet = ws
        return self._worksheet

    def ensure_headers(self) -> None:
        """Ставит заголовки, если первая строка пустая."""
        ws = self.worksheet
        first_row = ws.row_values(1)
        if not first_row:
            ws.update("A1", [HEADERS])
            ws.format("A1:Z1", {"textFormat": {"bold": True}})
            log.info("Заголовки записаны.")

    def append_row(self, row: list[Any]) -> int:
        """Добавляет строку в конец листа. Возвращает номер новой строки."""
        self.ensure_headers()
        ws = self.worksheet
        ws.append_row(row, value_input_option="USER_ENTERED")
        # gspread не возвращает номер строки — берём текущую длину.
        return len(ws.get_all_values())
