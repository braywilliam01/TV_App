# panel-watch

For a laptop running TV Launcher with the lid closed and output going to a TV over HDMI (or
sitting on a desk doubling as both). Keeps the laptop's own screen panel off when it isn't
needed, purely to save power and avoid burn-in -- **it never sleeps, suspends, or logs out
the machine**. Pair it with a logind drop-in that sets `HandleLidSwitch=ignore` (and the
`...ExternalPower`/`...Docked` variants) and masks `sleep.target`/`suspend.target`/
`hibernate.target`/`hybrid-sleep.target` if you want that guaranteed at the systemd level too.

Two small pieces:

- **`panel-watch.service`** (user-level) runs `panel-watch.sh`, which watches the lid switch
  and the HDMI connector and turns the `eDP-1` output on/off via `kscreen-doctor`:

  | Lid | HDMI | Panel |
  |---|---|---|
  | Closed | any | **Off**, immediately |
  | Open | not connected | **On**, always |
  | Open | connected <30s | **On** (still settling) |
  | Open | connected 30s+ | **On** for 2 min after the last real input, then **off** |

  The 30s settle avoids flapping the panel while an HDMI cable negotiates. The 2-minute
  window exists because opening the lid on a machine that's normally closed usually means
  you're about to debug something at the keyboard -- it starts counting from the moment the
  lid opens, so the panel comes on right away and stays on while you're actually using it.

- **`input-watch.service`** (root, system-level) feeds that 2-minute window with *real*
  keyboard/mouse/touch timestamps via `libinput debug-events`, written to
  `/run/panel-watch/last_input`. It runs as root specifically so it needs no `input` group
  membership or udev ACL setup -- on some distros (this one included) that group is managed
  dynamically by `systemd-sysusers` and plain `usermod -aG input` doesn't reliably stick.

## Requirements

- KDE Plasma on Wayland (`kscreen-doctor`, `libinput`)
- A lid switch exposed at `/proc/acpi/button/lid/LID0/state` and an HDMI connector at
  `/sys/class/drm/card1-HDMI-A-1/status` -- check `ls /sys/class/drm/card*-*/status` and
  adjust the `LID`/`HDMI` paths at the top of `panel-watch.sh` if yours differ.
- Passwordless (or at least scriptable) root for installing the system-level service.

## Install

```bash
# user-level: the panel watcher itself
install -Dm755 panel-watch.sh ~/.local/bin/panel-watch.sh
install -Dm644 panel-watch.service ~/.config/systemd/user/panel-watch.service
systemctl --user daemon-reload
systemctl --user enable --now panel-watch.service

# root-level: real input activity tracking
sudo install -Dm755 input-watch.sh /usr/local/bin/input-watch.sh
sudo install -Dm644 input-watch.service /etc/systemd/system/input-watch.service
sudo systemctl daemon-reload
sudo systemctl enable --now input-watch.service
```
