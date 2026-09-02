FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-session
# ^ baked into the image so EVERY process (chromedriver, Chrome, its zygotes)
#   inherits them — undetected-chromedriver's detached launch doesn't reliably
#   propagate env set at runtime, and Chrome needs DBUS_SESSION_BUS_ADDRESS to
#   reach the notification daemon (else it revokes push subs after 1 push).

# Google Chrome + its runtime libs + Xvfb (for the optional headful mode)
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates fonts-liberation tini xvfb xauth \
        dbus dbus-x11 dunst libnotify-bin \
        libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
        libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 libx11-6 libxcomposite1 \
        libxdamage1 libxext6 libxfixes3 libxrandr2 libxkbcommon0 libxshmfence1 \
        libpango-1.0-0 libcairo2 xdg-utils \
 && wget -q -O /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -f /tmp/chrome.deb \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh && mkdir -p /app/data

# Runs as root: Chrome in a container needs --no-sandbox anyway (the code adds
# it automatically when running as root), and this avoids bind-mount ownership
# headaches on ./data.
EXPOSE 8080

ENTRYPOINT ["tini", "--", "./entrypoint.sh"]
