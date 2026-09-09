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

# Chrome + a byte-for-byte matching chromedriver from Chrome-for-Testing, plus
# Chrome's runtime libs + Xvfb (for the optional headful mode).
#
# We pull BOTH binaries from the same CfT "Stable" manifest so their versions
# can never drift apart. The old setup installed google-chrome-stable_current
# and let undetected-chromedriver fetch a driver at runtime — the day Chrome
# rolled to a version the CfT driver channel hadn't published yet, every
# restore died with "This version of ChromeDriver only supports Chrome N".
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates jq unzip fonts-liberation tini xvfb xauth \
        dbus dbus-x11 dunst libnotify-bin \
        libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
        libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 libx11-6 libxcomposite1 \
        libxdamage1 libxext6 libxfixes3 libxrandr2 libxkbcommon0 libxshmfence1 \
        libpango-1.0-0 libcairo2 libu2f-udev libvulkan1 xdg-utils \
 && set -eux \
 && CFT="$(wget -qO- https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json)" \
 && CHROME_URL="$(echo "$CFT" | jq -r '.channels.Stable.downloads.chrome[]       | select(.platform=="linux64") | .url')" \
 && DRIVER_URL="$(echo "$CFT" | jq -r '.channels.Stable.downloads.chromedriver[] | select(.platform=="linux64") | .url')" \
 && CFT_VERSION="$(echo "$CFT" | jq -r '.channels.Stable.version')" \
 && echo "Chrome for Testing Stable = $CFT_VERSION" \
 && wget -q -O /tmp/chrome.zip "$CHROME_URL" \
 && wget -q -O /tmp/chromedriver.zip "$DRIVER_URL" \
 && unzip -q /tmp/chrome.zip -d /opt \
 && unzip -q /tmp/chromedriver.zip -d /opt \
 && ln -sf /opt/chrome-linux64/chrome            /usr/local/bin/google-chrome \
 && ln -sf /opt/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
 && chmod +x /opt/chrome-linux64/chrome /opt/chromedriver-linux64/chromedriver \
 && printf '%s\n' "$CFT_VERSION" > /opt/CHROME_VERSION \
 && rm -f /tmp/chrome.zip /tmp/chromedriver.zip \
 && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/local/bin/google-chrome \
    CHROMEDRIVER_BIN=/usr/local/bin/chromedriver

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
