# TV App

A fullscreen app launcher for a TV box (built for Bazzite / Fedora KDE), driven by an air remote.
Apps are listed in a YAML file and laid out automatically to fit the screen.

![Home screen](screenshots/home.png)

## Install (on the TV box)

```bash
git clone https://github.com/braywilliam01/TV_App.git
cd TV_App
chmod +x install.sh
./install.sh --autostart
```

`--autostart` opens the launcher when KDE logs in. Leave it off to start it from the app menu ("TV Launcher").
Nothing needs root; everything installs into your home folder.

**Update later:** `cd TV_App && git pull && ./install.sh` (your apps list is never overwritten).

## Controls

| Remote / keyboard | Action |
|---|---|
| D-pad / arrow keys | Move between tiles |
| OK / Enter | Open app |
| Up from the top row, or Menu key | Settings (gear, top right) |
| Back / Esc | Close menus (does nothing on the home screen) |
| Home | Jump to first tile |
| Ctrl+Q | Quit |

Pointing and clicking with the air mouse works too. After using the D-pad the pointer hides, so air-mouse wobble
doesn't move the selection.

## Settings menu

- **Themes:** Midnight, Ocean, Forest, Sunset, Light, High Contrast
- **Edit apps:** built-in YAML editor that checks the file as you type and won't save a broken file
- **Reload apps, Sleep, Restart, Shut down, Exit** (with confirmation, Cancel selected by default)

![Settings menu](screenshots/settings-menu.png)

## Adding apps

Edit `~/.config/tv-launcher/apps.yaml` (or use Settings > Edit apps on the TV):

```yaml
apps:
  - name: Kodi
    command: flatpak run tv.kodi.Kodi
    icon: tv.kodi.Kodi            # installed app icon name, or an image like icons/kodi.png

  - name: YouTube
    url: https://www.youtube.com  # opens in the browser from settings.browser
    icon: icons/youtube.png
```

Put your own icon images in `~/.config/tv-launcher/icons/`. Without an icon, the tile shows the first letter.
