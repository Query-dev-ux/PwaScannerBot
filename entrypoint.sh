#!/bin/sh
set -e

# HEADLESS=false -> run Chrome headful inside a virtual display (better against
# client-side anti-bot). Otherwise run plain headless.
if [ "${HEADLESS}" = "false" ]; then
  # Start Xvfb directly (no xvfb-run / xauth dependency). tini (PID 1) reaps it.
  rm -f /tmp/.X99-lock
  Xvfb :99 -screen 0 1280x2000x24 -ac -nolisten tcp >/tmp/xvfb.log 2>&1 &
  export DISPLAY=:99
  for i in $(seq 1 25); do
    [ -e /tmp/.X11-unix/X99 ] && break
    sleep 0.2
  done
fi

exec python bot.py
