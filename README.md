# Telegram-бот учёта отчётов магазина

Принимает текстовый отчёт в Telegram, через Groq LLM разбирает его на поля и добавляет строку в Google-таблицу.

## Колонки таблицы

```
Дата | Каспи | Наличка | Халык | Перевод | Кассир |
Инстаграм | Ватсап | Ватсап реклама | Офлайн | Постоянные клиенты |
Продажи онлайн | Продажи оффлайн |
Курьеры | Закуп | Прочие расходы
```

Бот сам создаёт заголовки при первом запуске, если лист пустой.

## 1. Что понадобится

1. Python 3.11+ (для локального запуска) или Docker.
2. Telegram bot token — получаешь у [@BotFather](https://t.me/BotFather): команда `/newbot`.
3. **Новый** Groq API key — старый скомпрометирован, зайди в [console.groq.com/keys](https://console.groq.com/keys), удали старый и создай новый.
4. Service account JSON для Google Sheets (`credentials.json`) — см. раздел 2.
5. Свой Telegram user id — напиши [@userinfobot](https://t.me/userinfobot), он пришлёт.

## 2. Настройка Google Sheets service account

Это одноразовая процедура. Если у тебя уже всё готово — пропускай.

1. Открой [Google Cloud Console](https://console.cloud.google.com/), создай проект (или возьми существующий).
2. В меню слева: **APIs & Services → Library** → найди и включи:
   - `Google Sheets API`
   - `Google Drive API`
3. **APIs & Services → Credentials → Create Credentials → Service account**.
4. Дай имя (например, `expense-bot`), жми **Done**.
5. Открой созданный service account → вкладка **Keys → Add Key → JSON**. Скачается файл — переименуй его в `credentials.json` и положи рядом с `bot.py`.
6. В файле будет строка `"client_email": "expense-bot@....iam.gserviceaccount.com"`. Скопируй этот email.
7. Открой свою Google-таблицу → **Settings / Share** → добавь этот email как **Editor**.

## 3. Конфигурация

```bash
cp .env.example .env
```

Открой `.env` и заполни:

- `TELEGRAM_BOT_TOKEN` — от BotFather.
- `GROQ_API_KEY` — новый ключ от Groq.
- `SPREADSHEET_ID` — из URL таблицы: `https://docs.google.com/spreadsheets/d/<ID>/edit`.
- `SHEET_NAME` — название вкладки внутри таблицы (по умолчанию `Отчёты`).
- `ALLOWED_USER_IDS` — Telegram id через запятую, кому разрешить пользоваться. Оставишь пустым — пустишь всех.

## 4. Запуск локально

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

Бот переходит в режим long polling — держи окно открытым или запусти через `tmux`/`screen`.

## 5. Запуск в Docker

```bash
docker compose up -d --build
docker compose logs -f
```

Остановить:

```bash
docker compose down
```

Для автозапуска на VPS с systemd можно обойтись без Docker — смотри `systemd`-юнит ниже.

## 6. Как пользоваться

1. Напиши боту `/start` — он пришлёт пример формата.
2. Пришли отчёт свободным текстом:

   ```
   21.04.2026
   Каспи 120к, нал 45000, халык 80к, перевод 30к.
   На кассе был Ерлан.
   Лиды: инст 12, вц 7, реклама вц 3, офлайн 4, постоянные 5.
   Продажи: онлайн 9, офлайн 14.
   Расходы: курьеры 8000, закуп 60к, прочее 3500.
   ```

3. Бот разберёт и покажет превью. Жми **✅ Записать** — строка улетит в таблицу. Не сошлось — **❌ Отмена** и перешли заново.

## 7. systemd-юнит (для VPS без Docker)

Положи файл `/etc/systemd/system/expense-bot.service`:

```ini
[Unit]
Description=Expense Telegram Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/expense-bot
ExecStart=/opt/expense-bot/.venv/bin/python bot.py
Restart=always
RestartSec=5
User=www-data

[Install]
WantedBy=multi-user.target
```

Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now expense-bot
sudo journalctl -u expense-bot -f
```

## 8. Что можно улучшить

- Добавить команду `/stats` с агрегатами за период.
- Хранить `PENDING` в Redis, чтобы бот не терял подтверждения при рестарте.
- Добавить редактирование строки: бот возвращает номер — при ответе на это сообщение с правкой обновлять строку.
- Валидацию по кассирам (whitelist имён).

## ⚠️ Безопасность

- `credentials.json` и `.env` **никогда** не коммить в git — они уже в `.gitignore`.
- API-ключ Groq, который ты прислал в чате, **нужно заменить** — считай его скомпрометированным.
- Обязательно заполни `ALLOWED_USER_IDS`, иначе писать отчёты сможет любой, кто найдёт твоего бота.
