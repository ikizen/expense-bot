"""
Клиент для работы с Google Sheets.
"""
from __future__ import annotations

import json
import logging
import os
import time
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

_COL_BUFFER = 20
_SESSIONS_SHEET = "_sessions"


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

    # ── Базовые хелперы ───────────────────────────────────────────────────

    def _sh(self) -> gspread.Spreadsheet:
        return self.client.open_by_key(self.spreadsheet_id)

    def _ws(self, sheet_name: str) -> gspread.Worksheet:
        """Возвращает лист. Создаёт если не существует. Всегда свежий запрос."""
        sh = self._sh()
        try:
            return sh.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            log.info("Лист '%s' не найден — создаю.", sheet_name)
            return sh.add_worksheet(
                title=sheet_name, rows=1000,
                cols=len(self.config.headers) + _COL_BUFFER,
            )

    def _ensure_cols(self, ws: gspread.Worksheet, needed: int) -> gspread.Worksheet:
        """Расширяет лист до needed+_COL_BUFFER. Никогда не уменьшает.
        Возвращает свежий ws после resize (grid ID меняется на сервере)."""
        target = needed + _COL_BUFFER
        if ws.col_count < target:
            ws.resize(rows=ws.row_count, cols=target)
            ws = self._sh().worksheet(ws.title)
            log.info("Лист '%s' расширен до %d колонок", ws.title, target)
        return ws

    # ── Заголовки ─────────────────────────────────────────────────────────

    def ensure_headers(self, sheet_name: str | None = None) -> None:
        """Добавляет недостающие заголовки. Не-фатально."""
        name = sheet_name or self.sheet_name
        try:
            ws = self._ws(name)
            existing = ws.row_values(1)
            want = self.config.headers

            if not existing:
                new_vals = want
            else:
                existing_set = {h for h in existing if h}
                missing = [h for h in want if h not in existing_set]
                if not missing:
                    return
                new_vals = list(existing) + missing

            ws = self._ensure_cols(ws, len(new_vals))
            ws.update("A1", [new_vals], value_input_option="RAW")
            log.info("ensure_headers '%s': %d колонок", name, len(new_vals))
        except Exception as e:
            log.error("ensure_headers '%s': %s", name, e)

    def sync_headers(self, sheet_name: str | None = None) -> dict:
        """Перезаписывает строку 1 точно под текущий конфиг."""
        name = sheet_name or self.sheet_name
        ws = self._ws(name)
        want = self.config.headers
        have = ws.row_values(1)

        want_set = set(want)
        have_set = {h for h in have if h}
        kept    = [h for h in have if h in want_set]
        cleared = [h for h in have if h and h not in want_set]
        added   = [h for h in want if h not in have_set]

        n = max(len(have), len(want))
        new_row = (want + [""] * n)[:n]

        ws = self._ensure_cols(ws, n)
        ws.update("A1", [new_row], value_input_option="RAW")
        log.info("sync_headers '%s': kept=%d cleared=%d added=%d",
                 name, len(kept), len(cleared), len(added))
        return {"kept": kept, "cleared": cleared, "added": added}

    def create_sheet(self, sheet_name: str,
                     headers: list[str] | None = None) -> bool:
        """Создаёт новый лист. False если уже существует."""
        sh = self._sh()
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
            return True

    # ── Запись строки ─────────────────────────────────────────────────────

    def append_row(self, row: list[Any],
                   sheet_name: str | None = None) -> int:
        """Добавляет строку данных. Возвращает номер записанной строки."""
        target = sheet_name or self.sheet_name
        ws = self._ws(target)

        sheet_headers = ws.row_values(1)
        if not sheet_headers:
            self.ensure_headers(target)
            ws = self._ws(target)
            sheet_headers = ws.row_values(1)

        config_headers = self.config.headers
        value_map = dict(zip(config_headers, row))
        row_values = [value_map.get(hdr, "") for hdr in sheet_headers]

        # get_all_values даёт точный счётчик строк (включая заголовок)
        all_data = ws.get_all_values()
        next_row = len(all_data) + 1
        ws.update(f"A{next_row}", [row_values], value_input_option="USER_ENTERED")
        return next_row

    # ── Сессии (PENDING, пережившие рестарт) ──────────────────────────────

    def _sessions_ws(self) -> gspread.Worksheet:
        sh = self._sh()
        try:
            return sh.worksheet(_SESSIONS_SHEET)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(_SESSIONS_SHEET, rows=500, cols=5)
            ws.update("A1", [["token", "sheet", "ts", "data_json"]],
                      value_input_option="RAW")
            return ws

    def load_sessions(self) -> dict:
        """Загружает активные сессии из листа _sessions → {token: entry}."""
        try:
            ws = self._sessions_ws()
            rows = ws.get_all_values()
            if len(rows) <= 1:
                return {}
            cutoff = time.time() - 3600
            result: dict = {}
            for row in rows[1:]:
                if len(row) < 4 or not row[0]:
                    continue
                token, sheet, ts_str, data_json = row[0], row[1], row[2], row[3]
                try:
                    ts = float(ts_str)
                    if ts < cutoff:
                        continue
                    data = json.loads(data_json)
                    result[token] = {"data": data, "sheet": sheet, "ts": ts}
                except Exception:
                    pass
            log.info("Загружено %d сессий из Sheets", len(result))
            return result
        except Exception as e:
            log.warning("load_sessions: %s", e)
            return {}

    def save_session(self, token: str, entry: dict) -> None:
        """Сохраняет сессию в лист _sessions."""
        try:
            ws = self._sessions_ws()
            ws.append_row([
                token,
                entry.get("sheet", ""),
                str(entry.get("ts", time.time())),
                json.dumps(entry.get("data", {}), ensure_ascii=False),
            ], value_input_option="RAW")
        except Exception as e:
            log.warning("save_session '%s': %s", token, e)

    def delete_session(self, token: str) -> None:
        """Удаляет строку сессии из _sessions."""
        try:
            ws = self._sessions_ws()
            cell = ws.find(token, in_column=1)
            if cell:
                ws.delete_rows(cell.row)
        except Exception as e:
            log.warning("delete_session '%s': %s", token, e)

    def cleanup_sessions(self) -> int:
        """Удаляет истёкшие строки из _sessions. Возвращает кол-во удалённых."""
        try:
            ws = self._sessions_ws()
            rows = ws.get_all_values()
            if len(rows) <= 1:
                return 0
            cutoff = time.time() - 3600
            to_delete: list[int] = []
            for i, row in enumerate(rows[1:], start=2):
                if not row or not row[0]:
                    to_delete.append(i)
                    continue
                try:
                    ts = float(row[2]) if len(row) > 2 else 0
                    if ts < cutoff:
                        to_delete.append(i)
                except (ValueError, IndexError):
                    to_delete.append(i)
            for idx in reversed(to_delete):
                ws.delete_rows(idx)
            return len(to_delete)
        except Exception as e:
            log.warning("cleanup_sessions: %s", e)
            return 0

    # ── Dashboard ─────────────────────────────────────────────────────────

    def get_stats(self, sheet_name: str | None = None, days: int = 7) -> dict:
        """Возвращает агрегаты за последние N дней."""
        ws = self._ws(sheet_name or self.sheet_name)
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
