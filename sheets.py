"""
Клиент для работы с Google Sheets.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
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


def create_new_spreadsheet(
    gs_client: gspread.Client,
    title: str,
    folder_id: str | None = None,
    share_email: str | None = None,
) -> tuple[str, str]:
    """
    Создаёт новую Google Таблицу напрямую в указанной папке Drive.
    Это обходит квоту хранилища сервисного аккаунта.
    Возвращает (spreadsheet_id, url).
    """
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

    # Поделиться с владельцем
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

    def _ensure_enough_cols(self, ws: gspread.Worksheet, needed: int) -> None:
        """Расширяет лист если нужных колонок не хватает."""
        if ws.col_count < needed:
            new_cols = needed + 10  # +10 запас под extra_expenses
            ws.resize(rows=ws.row_count, cols=new_cols)
            log.info("Лист '%s' расширен до %d колонок", ws.title, new_cols)

    def ensure_headers(self, sheet_name: str | None = None) -> None:
        name = sheet_name or self.sheet_name
        ws = self._get_ws(name)
        existing = ws.row_values(1)
        headers = self.config.headers

        if not existing:
            # Новый лист — пишем все заголовки сразу
            self._ensure_enough_cols(ws, len(headers))
            ws.update("A1", [headers])
            ws.format("A1:Z1", {"textFormat": {"bold": True}})
            log.info("Заголовки записаны на листе '%s'.", name)
        else:
            # Лист уже есть — дописываем только отсутствующие заголовки
            existing_set = set(existing)
            missing = [h for h in headers if h not in existing_set]
            if missing:
                start_col = len(existing) + 1
                self._ensure_enough_cols(ws, start_col + len(missing) - 1)
                for i, h in enumerate(missing):
                    col = start_col + i
                    ws.update_cell(1, col, h)
                    ws.format(
                        gspread.utils.rowcol_to_a1(1, col),
                        {"textFormat": {"bold": True}},
                    )
                log.info("Добавлены заголовки на лист '%s': %s", name, missing)
                # Сбрасываем кэш чтобы sheet_headers в append_row был актуальным
                self._ws_cache.pop(name, None)

    def create_sheet(self, sheet_name: str,
                     headers: list[str] | None = None) -> bool:
        """Создаёт новый лист с заданными (или всеми) заголовками.
        Возвращает False если лист уже существует."""
        sh = self.client.open_by_key(self.spreadsheet_id)
        try:
            sh.worksheet(sheet_name)
            return False
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=30)
            hdrs = headers if headers is not None else self.config.headers
            ws.update("A1", [hdrs])
            ws.format("A1:Z1", {"textFormat": {"bold": True}})
            # FIX #7: инвалидируем старый кэш и сохраняем свежий объект
            self._ws_cache[sheet_name] = ws
            return True

    def sync_headers(self, sheet_name: str | None = None) -> dict:
        """Перезаписывает строку 1 листа ровно под текущий конфиг.
        Лишние старые колонки очищаются, недостающие добавляются.
        Данные в строках 2+ не трогаются (но могут сместиться — только для чистых листов!).
        Возвращает {"kept": [...], "cleared": [...], "added": [...]}."""
        name = sheet_name or self.sheet_name
        ws = self._get_ws(name)
        want = self.config.headers          # что должно быть
        have = ws.row_values(1)             # что есть сейчас

        want_set = set(want)
        have_set = set(h for h in have if h)

        kept    = [h for h in have if h in want_set]
        cleared = [h for h in have if h and h not in want_set]
        added   = [h for h in want if h not in have_set]

        # Расширяем если надо
        self._ensure_enough_cols(ws, len(want))

        # Пишем нужные заголовки в нужные позиции
        new_row = want + [""] * (max(len(have), len(want)) - len(want))
        ws.update("A1", [new_row[:max(len(have), len(want))]])

        # Форматируем жирным только занятые ячейки
        end_col = gspread.utils.rowcol_to_a1(1, len(want))
        ws.format(f"A1:{end_col}", {"textFormat": {"bold": True}})

        # Сбрасываем кэш
        self._ws_cache.pop(name, None)
        log.info("sync_headers '%s': kept=%d cleared=%d added=%d",
                 name, len(kept), len(cleared), len(added))
        return {"kept": kept, "cleared": cleared, "added": added}

    def invalidate_cache(self, sheet_name: str | None = None) -> None:
        """Сбрасывает кэш воркшита. Без аргументов — весь кэш."""
        if sheet_name:
            self._ws_cache.pop(sheet_name, None)
        else:
            self._ws_cache.clear()

    def _get_or_create_col(self, ws: gspread.Worksheet,
                           headers: list[str], name: str) -> int:
        if name in headers:
            return headers.index(name) + 1
        new_col = len(headers) + 1
        # Расширяем лист если новая колонка выходит за лимит
        self._ensure_enough_cols(ws, new_col)
        ws.update_cell(1, new_col, name)
        ws.format(gspread.utils.rowcol_to_a1(1, new_col),
                  {"textFormat": {"bold": True}})
        headers.append(name)
        return new_col

    def append_row(self, row: list[Any],
                   sheet_name: str | None = None) -> int:
        """Добавляет строку, сопоставляя значения по названию колонки.
        Доп. расходы хранятся в текстовом поле extras_text внутри row."""
        target = sheet_name or self.sheet_name
        self.ensure_headers(target)
        ws = self._get_ws(target)
        sheet_headers = ws.row_values(1)      # реальные заголовки этого листа
        config_headers = self.config.headers  # все метки из конфига (в том же порядке что row)

        # Строим словарь метка → значение из полного row
        value_map: dict[str, Any] = dict(zip(config_headers, row))

        # FIX #5: col_values(1) читает только первую колонку вместо всех данных
        next_row = len(ws.col_values(1)) + 1

        # Только те колонки, которые есть на этом листе
        cells = [
            gspread.Cell(next_row, col_idx + 1, value_map[hdr])
            for col_idx, hdr in enumerate(sheet_headers)
            if hdr in value_map
        ]

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
