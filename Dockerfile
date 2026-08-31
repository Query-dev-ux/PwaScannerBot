FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# Google Chrome + its runtime libs + Xvfb (for the optional headful mode)
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates fonts-liberation tini xvfb \
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

# non-root; undetected-chromedriver caches the driver under $HOME
RUN chmod +x entrypoint.sh \
 && useradd -m -u 10001 app \
 && mkdir -p /app/data \
 && chown -R app:app /app
USER app

EXPOSE 8080

ENTRYPOINT ["tini", "--", "./entrypoint.sh"]
