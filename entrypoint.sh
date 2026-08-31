#!/bin/sh
set -e

# HEADLESS=false -> run Chrome headful inside a virtual display (better against
# client-side anti-bot). Otherwise run plain headless.
if [ "${HEADLESS}" = "false" ]; then
  exec xvfb-run -a --server-args="-screen 0 1280x2000x24 -ac -nolisten tcp" \
       python bot.py
fi

exec python bot.py
