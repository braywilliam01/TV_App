#!/bin/bash
# Laptop panel (eDP-1) power rule -- no sleep/suspend/logout involved anywhere here,
# this only ever calls kscreen-doctor to turn the physical panel on/off:
#
#   lid closed                        -> panel off, always, immediately
#   lid open, no HDMI                 -> panel on, always, no timeout
#   lid open, HDMI connected 30s+     -> panel on for 2 min after the last real input
#                                        (opening the lid itself counts as input)
#   lid open, HDMI just connected     -> panel on (still inside the 30s settle window)
LID=/proc/acpi/button/lid/LID0/state
HDMI=/sys/class/drm/card1-HDMI-A-1/status
LAST_INPUT_FILE=/run/panel-watch/last_input   # written by the root-level input-watch.service
HDMI_CHECK_EVERY=30     # seconds; HDMI/idle status is only re-read this often
IDLE_TIMEOUT=120        # seconds of no input before the panel can turn off (HDMI branch only)

lid_closed() { grep -q closed "$LID" 2>/dev/null; }
hdmi_connected() { [ "$(cat "$HDMI" 2>/dev/null)" = "connected" ]; }
real_last_input() { cat "$LAST_INPUT_FILE" 2>/dev/null || echo 0; }

state=""              # last applied panel state: "on" or "off"
hdmi_since=""         # epoch when HDMI was first seen connected, "" if not connected
lid_was_closed=1      # bootstrap as "was closed" so the first open is treated as a fresh open
lid_opened_at=0        # local-only timestamp; opening the lid counts as activity without
                       # needing write access to the root-owned LAST_INPUT_FILE
next_hdmi_check=0

while true; do
  now=$(date +%s)

  if lid_closed; then
    desired="off"
    lid_was_closed=1
  else
    if [ "$lid_was_closed" = "1" ]; then
      lid_opened_at=$now
      next_hdmi_check=0   # re-evaluate HDMI/idle right away instead of waiting out the old cadence
    fi
    lid_was_closed=0

    if [ "$now" -ge "$next_hdmi_check" ]; then
      next_hdmi_check=$((now + HDMI_CHECK_EVERY))
      if hdmi_connected; then
        [ -z "$hdmi_since" ] && hdmi_since=$now
        last=$(real_last_input)
        [ "$lid_opened_at" -gt "$last" ] && last=$lid_opened_at
        if [ $((now - hdmi_since)) -ge 30 ] && [ $((now - last)) -ge "$IDLE_TIMEOUT" ]; then
          desired="off"
        else
          desired="on"
        fi
      else
        hdmi_since=""
        desired="on"
      fi
    fi
    # Between HDMI-check ticks, keep whatever "desired" already was.
    : "${desired:=on}"
  fi

  if [ "$desired" != "$state" ]; then
    kscreen-doctor "output.eDP-1.$desired" >/dev/null 2>&1
    state="$desired"
  fi
  sleep 2
done
