"""
Клиент для работы с Google Sheets.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta, datetime
from typing import Any, TYPE_CHECKING

import requests as http_requests
import gspread
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

if TYPE_CHECKING:
    from config_manager import ConfigManager

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Минимальный запас колонок сверх нужных — чтобы лист не кончался
_COL_BUFFER = 20


def create_new_spreadsheet(
    gs_client: gspread.Client,
    title: str,
    folder_id: str | None = None,
    share_email: str | None = None,
) -> tuple[str, str]:
    creds = gs_client.auth
    if not creds.valid:
        creds.refresh(Request())

    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    body: dict = {
        "name": title,
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }
    if folder_id:
        body["parents"] = [folder_id]

    resp = http_requests.post(
        "https://www.googleapis.com/drive/v3/files",
        headers=headers,
        json=body,
    )
    resp.raise_for_status()
    sid = resp.json()["id"]
    url = f"https://docs.google.com/spreadsheets/d/{sid}"
    log.info("Создана таблица: %s", url)

    if share_email:
        try:
            sh = gs_client.open_by_key(sid)
            sh.share(share_email, perm_type="user", role="writer", notify=True)
            log.info("Таблица расшарена с %s", share_email)
        except Exception as e:
            log.warning("Не удалось поделиться с %s: %s", share_email, e)

    return sid, url


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
                 sheet_name: str, config: "ConfigManager",
                 gs_client: gspread.Client | None = None):
        self.client = gs_client or _build_gs_client(creds_path)
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.config = config
        self._ws_cache: dict[str, gspread.Worksheet] = {}

    # ── Получение листа ───────────────────────────────────────────────────

    def _get_ws(self, sheet_name: str) -> gspread.Worksheet:
        """Возвращает объект листа. Кэш используется только для быстрых операций записи."""
        if sheet_name not in self._ws_cache:
            sh = self.client.open_by_key(self.spreadsheet_id)
            try:
                ws = sh.worksheet(sheet_name)
            except gspread.WorksheetNotFound:
                log.info("Лист '%s' не найден — создаю.", sheet_name)
                # Создаём сразу с запасом колонок
                ws = sh.add_worksheet(
                    title=sheet_name, rows=1000,
                    cols=len(self.config.headers) + _COL_BUFFER,
                )
            self._ws_cache[sheet_name] = ws
        return self._ws_cache[sheet_name]

    def _api_ws(self, sheet_name: str) -> tuple[gspread.Spreadsheet, gspread.Worksheet]:
        """Всегда делает свежий запрос к API. Возвращает (spreadsheet, worksheet).
        Создаёт лист если не существует. Использовать перед resize/write заголовков."""
        sh = self.client.open_by_key(self.spreadsheet_id)
        try:
            ws = sh.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            log.info("Лист '%s' не найден — создаю.", sheet_name)
            ws = sh.add_worksheet(
                title=sheet_name, rows=1000,
                cols=len(self.config.headers) + _COL_BUFFER,
            )
        return sh, ws

    def _expand_cols(self, sh: gspread.Spreadsheet,
                     ws: gspread.Worksheet, needed: int) -> gspread.Worksheet:
        """Расширяет лист до needed+_COL_BUFFER колонок если нужно.
        Никогда не уменьшает. Возвращает свежий ws после resize."""
        target = needed + _COL_BUFFER
        if ws.col_count < target:
            ws.resize(rows=ws.row_count, cols=target)
            log.info("Лист '%s' расширен до %d колонок", ws.title, target)
            # Получаем свежий объект с актуальным col_count после resize
            ws = sh.worksheet(ws.title)
        return ws

    def invalidate_cache(self, sheet_name: str | None = None) -> None:
        if sheet_name:
            self._ws_cache.pop(sheet_name, None)
        else:
            self._ws_cache.clear()

    # ── Заголовки ─────────────────────────────────────────────────────────

    def ensure_headers(self, sheet_name: str | None = None) -> None:
        """Добавляет недостающие заголовки в строку 1. Расширяет лист если нужно.
        Не-фатально — ошибка заголовков не прерывает запись данных."""
        name = sheet_name or self.sheet_name
        try:
            # Всегда свежий запрос к API — col_count из кэша ненадёжен
            sh, ws = self._api_ws(name)
            existing = ws.row_values(1)
            want = self.config.headers

            if not existing:
                new_vals = want
            else:
                existing_set = set(h for h in existing if h)
                missing = [h for h in want if h not in existing_set]
                if not missing:
                    # Заголовки уже на месте — обновляем кэш свежим объектом
                    self._ws_cache[name] = ws
                    return
                new_vals = list(existing) + missing

            # Расширяем лист (если нужно) и получаем свежий ws
            ws = self._expand_cols(sh, ws, len(new_vals))

            ws.update("A1", [new_vals], value_input_option="RAW")
            self._ws_cache[name] = ws
            log.info("ensure_headers '%s': итого %d колонок", name, len(new_vals))
        except Exception as e:
            log.error("ensure_headers '%s' не удалось: %s", name, e)
            self._ws_cache.pop(name, None)  # сбрасываем кэш при ошибке

    def sync_headers(self, sheet_name: str | None = None) -> dict:
        """Перезаписывает строку 1 точно под текущий конфиг (убирает старые колонки).
        Возвращает {"kept", "cleared", "added"}."""
        name = sheet_name or self.sheet_name
        sh, ws = self._api_ws(name)
        want = self.config.headers
        have = ws.row_values(1)

        want_set = set(want)
        have_set = set(h for h in have if h)
        kept    = [h for h in have if h in want_set]
        cleared = [h for h in have if h and h not in want_set]
        added   = [h for h in want if h not in have_set]

        # Строка 1: нужные заголовки + пустые ячейки поверх старых лишних
        n = max(len(have), len(want))
        new_row = (want + [""] * n)[:n]

        ws = self._expand_cols(sh, ws, n)
        ws.update("A1", [new_row], value_input_option="RAW")
        self._ws_cache.pop(name, None)

        log.info("sync_headers '%s': kept=%d cleared=%d added=%d",
                 name, len(kept), len(cleared), len(added))
        return {"kept": kept, "cleared": cleared, "added": added}

    def create_sheet(self, sheet_name: str,
                     headers: list[str] | None = None) -> bool:
        """Создаёт новый лист с заданными заголовками.
        Возвращает False если лист уже существует."""
        sh = self.client.open_by_key(self.spreadsheet_id)
        try:
            sh.worksheet(sheet_name)
            return False
        except gspread.WorksheetNotFound:
            hdrs = headers if headers is not None else self.config.headers
            ws = sh.add_worksheet(
                title=sheet_name, rows=1000,
                cols=len(hdrs) + _COL_BUFFER,
            )
            ws.update("A1", [hdrs], value_input_option="RAW")
            self._ws_cache[sheet_name] = ws
            return True

    # ── Запись строки ─────────────────────────────────────────────────────

    def append_row(self, row: list[Any],
                   sheet_name: str | None = None) -> int:
        """Добавляет строку данных. Перед записью проверяет/дополняет заголовки."""
        target = sheet_name or self.sheet_name
        self.ensure_headers(target)

        ws = self._get_ws(target)
        sheet_headers = ws.row_values(1)
        config_headers = self.config.headers
        value_map: dict[str, Any] = dict(zip(config_headers, row))

        next_row = len(ws.col_values(1)) + 1

        cells = [
            gspread.Cell(next_row, col_idx + 1, value_map[hdr])
            for col_idx, hdr in enumerate(sheet_headers)
            if hdr in value_map
        ]

        ws.update_cells(cells, value_input_option="USER_ENTERED")
        return next_row

    # ── Dashboard ─────────────────────────────────────────────────────────

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
                if col == headers[0]:
                    continue
                try:
                    n = float(str(val).replace(" ", "").replace(",", "."))
                    totals[col] = totals.get(col, 0) + n
                except (ValueError, TypeError):
                    pass

        return {"days": days, "count": count, "totals": totals, "headers": headers}
