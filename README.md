# PwaScannerBot

Телеграм-бот (aiogram 3.x), который через SOCKS5-прокси проходит PWA-воронку
(fake-store / колесо / гейт), достаёт ссылку, открывающуюся **внутри**
установленного PWA, и собирает web-push уведомления по стадиям
(после установки → после регистрации → после депозита).

## Как работает

1. **🔍 Scan** → выбор прокси сервиса → ссылка на PWA
2. Бот в `undetected-chromedriver` (эмуляция реального Android-Chrome — UA +
   согласованные Sec-CH-UA, таймзона/локаль/гео под прокси — обходит жёсткий
   anti-bot воронок):
   - открывает сайт, проходит лоадер/воронку, ловит `beforeinstallprompt`
   - читает manifest, проверяет service worker
   - открывает `start_url` в режиме standalone → достаёт финальный редирект
     (реальный оффер с трекинг-параметрами) = **ссылка внутри PWA**
3. Отдаёт карточку: `Название PWA / Ссылка на PWA / Ссылка внутри PWA`.
   Сессия пока **не сохранена**.
4. **🔔 Включить сбор пушей** → сессия сохраняется, стартует наблюдатель
   (CDP `BackgroundService`), стадия = «после установки»
5. **🖥 Открыть браузер сессии** — ссылка на веб-страницу с живым экраном
   сессии (CDP screencast) + управление мышью/клавиатурой. Регистрируешься и
   вносишь депозит прямо в браузере сессии, затем жмёшь
   **✅ Зарегистрировался** / **✅ Внёс депозит** — стадия переключается.
6. Пуши копятся `COLLECT_DAYS` дней, тегаются текущей стадией.
7. По таймеру (или **⏹ Стоп + архив**) — zip с `pushes.json` + `pushes.txt`,
   пуши сгруппированы по стадиям.

## Локальный запуск

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate — Linux
pip install -r requirements.txt

copy .env.example .env            # впиши BOT_TOKEN
copy proxies.example.json proxies.json   # впиши реальные SOCKS5-прокси

python bot.py
```

Нужен установленный Google Chrome. `undetected-chromedriver` сам скачает драйвер.

## Сервер / Docker

См. [DEPLOY.md](DEPLOY.md).

## Конфиг (`.env`)

| ключ | назначение |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `PUBLIC_URL` | как обращаться к «живому браузеру» извне (`http://IP:8080`) |
| `WEBCONTROL_HOST` / `WEBCONTROL_PORT` | адрес веб-сервера управления (`0.0.0.0` / `8080`) |
| `HEADLESS` | `true` по умолчанию; `false` + xvfb на сервере, если казино палит headless |
| `COLLECT_DAYS` | срок сбора пушей (7) |
| `MAX_SESSIONS` | одновременных сессий (держать под RAM: ≈ `RAM_ГБ − 2`) |
| `QA_TOKEN` | если у воронки есть backdoor `?qa=` |

## Прокси

`proxies.json` — список объектов. Схему можно опустить (подставится `socks5`):

```json
[{ "name": "PE", "server": "socks5://user:pass@host:port", "username": "", "password": "" }]
```

Chromium не умеет авторизацию на SOCKS — на каждую сессию поднимается локальный
форвардер `pproxy` на `127.0.0.1:<rand>`, он несёт креды на апстрим.

## Ограничения

- Нативный диалог установки PWA не подтверждается автоматически (`appinstalled`
  часто `false`) — для сбора пушей не критично, SW регистрируется при загрузке.
- Перезапуск процесса = активные сессии теряются (нет `restore()`).
- Веб-доступ к браузеру защищён только токеном в ссылке — см. DEPLOY.md §7.
- Push-наблюдатель проверен на подключение/подписку; на живом web-push от
  конкретного казино — тестировать отдельно.
- Инструмент — для мониторинга уведомлений сервисов, которые вы вправе
  мониторить. Соблюдайте законы и ToS сайтов.

## Структура

```
bot.py                              вход: Bot/Dispatcher, планировщик, веб-сервер
app/config.py                       настройки (.env)
app/db.py                           SQLite: sessions + pushes (+ stage)
app/proxies.py                      загрузка/парсинг прокси
app/keyboards.py  app/states.py     клавиатуры, FSM
app/handlers/start.py               /start, Scan, Сессии
app/handlers/flow.py                прокси→url→скан→сбор→стадии→живой браузер
app/services/session_manager.py     жизненный цикл сессии, undetected-chromedriver
app/services/local_proxy.py         pproxy-форвардер для авторизованных SOCKS5
app/services/cdp_bridge.py          CDP ws: push observer + screencast + input
app/services/webcontrol.py          aiohttp: страница «живого браузера»
app/services/packer.py              zip-архив с пушами по стадиям
```
