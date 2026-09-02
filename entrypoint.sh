#!/bin/sh
set -e

# DISPLAY and DBUS_SESSION_BUS_ADDRESS are baked into the image (Dockerfile ENV)
# so every process — including Chrome and its zygotes — inherits them. Here we
# only bring up the servers they point at.

if [ "${HEADLESS}" = "false" ]; then
  rm -f /tmp/.X99-lock
  Xvfb :99 -screen 0 1280x2000x24 -ac -nolisten tcp >/tmp/xvfb.log 2>&1 &
  for i in $(seq 1 25); do
    [ -e /tmp/.X11-unix/X99 ] && break
    sleep 0.2
  done
else
  unset DISPLAY   # headless Chrome must not try to reach an X server
fi

# Session D-Bus on the fixed socket + a notification daemon. Without a
# displayable notification Chrome REVOKES userVisibleOnly push subscriptions
# after the first push ("Unsubscribed due to error").
if command -v dbus-daemon >/dev/null 2>&1; then
  rm -f /tmp/dbus-session
  dbus-daemon --session --nofork --nopidfile \
      --address=unix:path=/tmp/dbus-session >/tmp/dbus.log 2>&1 &
  for i in $(seq 1 25); do
    [ -S /tmp/dbus-session ] && break
    sleep 0.2
  done
  if [ "${HEADLESS}" != "false" ]; then
    :
  elif command -v dunst >/dev/null 2>&1; then
    dunst >/tmp/dunst.log 2>&1 &
  fi
fi

exec python bot.py
