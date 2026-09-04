# Развёртывание PwaScannerBot на своём сервере

Рекомендуемый способ — Docker. Бот сам тянет Google Chrome внутрь образа,
undetected-chromedriver докачивает подходящий драйвер при первом запуске.

## 1. Требования к серверу

| | Минимум (1 сессия, тест) | Рабочий (5–8 сессий) |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 ГБ | 8–12 ГБ |
| Диск | 40 ГБ SSD | 80 ГБ SSD |

- **KVM VPS с root** (не OpenVZ — Chrome не стартует), x86-64, Ubuntu 22.04/24.04
- Одна активная сессия сбора = 1 процесс Chrome на всё время сбора ≈ **0.7–1 ГБ RAM**
- Гео сервера роли не играет — весь трафик идёт через SOCKS5-прокси из `proxies.json`
- Нужен **домен, направленный на IP сервера** (A-запись) — «живой браузер»
  отдаётся через Caddy по HTTPS/WSS на портах 80/443. Без домена нормальный
  TLS-сертификат не выпустить (см. раздел 7 — почему это не опционально)

## 2. Установка Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # затем перелогиниться
```

## 3. Код и конфиг

```bash
git clone https://github.com/Query-dev-ux/PwaScannerBot.git
cd PwaScannerBot

cp .env.example .env
cp proxies.example.json proxies.json
```

Отредактируй **`.env`**:

```ini
BOT_TOKEN=<токен от @BotFather>

# Домен, у которого A-запись указывает на IP этого сервера.
# Caddy сам получит для него сертификат Let's Encrypt при первом старте.
PUBLIC_DOMAIN=bot.example.com
PUBLIC_URL=https://bot.example.com
WEBCONTROL_HOST=0.0.0.0
WEBCONTROL_PORT=8080

HEADLESS=true            # см. раздел 6
COLLECT_DAYS=7
MAX_SESSIONS=4           # ≈ (RAM_ГБ - 2). Больше — OOM.
```

Отредактируй **`proxies.json`** — впиши реальные SOCKS5-прокси
(`socks5://user:pass@host:port`). Гео прокси должно совпадать с гео оффера.

## 4. Запуск

```bash
docker compose up -d --build
docker compose logs -f
```

Порты в фаерволе (80/443 — Caddy; 8080 наружу открывать не нужно, бот
слушает его только внутри docker-сети):

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

Проверка: `https://ВАШ_ДОМЕН/healthz` → `ok` (первый запрос может занять
несколько секунд — Caddy в этот момент выпускает сертификат).

## 5. Обновление

```bash
git pull
docker compose up -d --build
```

Данные (SQLite + профили браузеров) лежат в `./data` и переживают пересборку.

## 6. Headless vs headful

`HEADLESS=true` — проще, меньше RAM, проходит текущую anti-bot проверку воронки.

Если конкретное казино палит headless на этапе регистрации — поставь
`HEADLESS=false`. Контейнер сам поднимет Chrome под виртуальным дисплеем
(Xvfb уже в образе, `entrypoint.sh` разруливает). RAM почти не меняется.

## 7. Безопасность и HTTPS «живого браузера»

Страница управления защищена **только случайным токеном в ссылке**. Кто угодно
с этой ссылкой управляет браузером сессии (там будут твои казино-аккаунты) —
не постить ссылку никуда, она одноразовая на сессию.

Трафик идёт через **Caddy** (HTTPS/WSS, автоматический Let's Encrypt) — это
не опционально для «живого браузера»: на голом `http://IP:8080` некоторые
сети (мобильный оператор телефона, домашний роутер с DPI) молча вырезают
заголовок `Upgrade` из WebSocket-запроса, ломая стрим экрана без единой
видимой ошибки — только текст статуса «connecting…», который никогда не
меняется на «live». Шифрование делает это невозможным: посреднику просто
нечего вырезать внутри TLS-туннеля. Проверено на реальном отказе на iPhone.

## 8. Важные ограничения

- Перезапуск контейнера / ребут сервера — активные сессии сбора **переживаются**:
  `restore()` заново поднимает Chrome по каждой `collecting`-сессии из БД
  (профиль в `data/sessions/<id>/` сохраняет SW и подписку на push). Будет
  короткий разрыв в доставке пушей на время рестарта, но сессия не теряется.
- Профили в `data/sessions/<id>/` не чистятся сами — раз в неделю:
  ```bash
  find data/sessions -maxdepth 1 -type d -mtime +8 -exec rm -rf {} +
  ```
- `MAX_SESSIONS` держи под RAM, иначе OOM-killer тихо прибьёт Chrome.

## Без Docker (systemd)

```bash
sudo apt install -y python3.12-venv google-chrome-stable xvfb
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && cp proxies.example.json proxies.json   # заполнить

sudo tee /etc/systemd/system/pwabot.service <<'EOF'
[Unit]
Description=PwaScannerBot
After=network-online.target

[Service]
WorkingDirectory=/opt/PwaScannerBot
ExecStart=/opt/PwaScannerBot/.venv/bin/python bot.py
Restart=on-failure
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now pwabot
journalctl -u pwabot -f
```
