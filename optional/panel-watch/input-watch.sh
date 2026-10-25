#!/bin/bash
# Writes the current epoch time to STATE_FILE on every real keyboard/mouse/touch event.
# Runs as root (system service) so it needs no group/ACL fiddling for /dev/input access.
STATE_DIR=/run/panel-watch
STATE_FILE="$STATE_DIR/last_input"
mkdir -p "$STATE_DIR"
chmod 755 "$STATE_DIR"
date +%s > "$STATE_FILE"
chmod 644 "$STATE_FILE"

libinput debug-events 2>/dev/null | while read -r line; do
  case "$line" in
    *KEYBOARD_KEY*|*POINTER_MOTION*|*POINTER_BUTTON*|*POINTER_AXIS*|*GESTURE_*|*TOUCH_DOWN*|*TOUCH_MOTION*)
      date +%s > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
      ;;
  esac
done
