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

# Session D-Bus on a FIXED socket + a notification daemon, so Chrome's
# showNotification() gets acknowledged. Without a visible notification Chrome
# REVOKES userVisibleOnly push subscriptions after the first push
# ("Unsubscribed due to error"). The fixed path lets the Python side hand the
# exact address to Chrome (chromedriver doesn't reliably propagate env).
if command -v dbus-daemon >/dev/null 2>&1; then
  rm -f /tmp/dbus-session
  dbus-daemon --session --nofork --nopidfile \
      --address=unix:path=/tmp/dbus-session >/tmp/dbus.log 2>&1 &
  export DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-session
  for i in $(seq 1 25); do
    [ -S /tmp/dbus-session ] && break
    sleep 0.2
  done
  if [ -n "${DISPLAY}" ] && command -v dunst >/dev/null 2>&1; then
    dunst >/tmp/dunst.log 2>&1 &
  fi
fi

exec python bot.py
