#!/usr/bin/env python3
"""TV Launcher: a fullscreen grid of apps read from a YAML file.

Built for an air remote: arrow keys / D-pad move between tiles, Enter/OK opens
the app, and pointing + clicking works too. Edit the YAML and the grid reloads.

Usage: launcher.py [path/to/apps.yaml]
Keys:  arrows move, Enter/Space/OK open, Home jumps to first tile,
       Up from the top row (or the Menu key) reaches Settings,
       Back/Esc leaves a group (and does nothing on the home screen),
       F5 reloads the YAML, Ctrl+Q quits.
"""
import hashlib
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from PySide6.QtCore import (QDateTime, QFileSystemWatcher, QObject, QPoint, QPointF, QRect, QRectF, QRegularExpression,
                            QSize, Qt, QTimer, QUrl, Signal)
from PySide6.QtNetwork import (QLocalServer, QLocalSocket, QNetworkAccessManager, QNetworkReply,
                               QNetworkRequest)
from PySide6.QtGui import (QColor, QCursor, QFont, QFontDatabase, QIcon, QKeySequence, QPainter, QPainterPath, QPen,
                           QPixmap, QShortcut, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextFormat, QTransform)
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTextEdit,
                               QVBoxLayout, QWidget)

THEMES = {
    "midnight": {"background": "#0f1216", "tile": "#1b2027", "accent": "#3d9cff", "text": "#e8ecf1"},
    "ocean":    {"background": "#0b1a2a", "tile": "#13304a", "accent": "#3fd0e0", "text": "#e6f4ff"},
    "forest":   {"background": "#0f1a14", "tile": "#1c2f23", "accent": "#5fd38d", "text": "#e8f5ec"},
    "sunset":   {"background": "#1d1220", "tile": "#301c31", "accent": "#ff8a5c", "text": "#fbe9e4"},
    "light":    {"background": "#e9edf2", "tile": "#ffffff", "accent": "#1f5fd6", "text": "#1b2027"},
    "contrast": {"background": "#000000", "tile": "#141414", "accent": "#ffd400", "text": "#ffffff"},
}
THEME_LABELS = {"contrast": "High Contrast"}
COLOR_KEYS = ("background", "tile", "accent", "text")

DEFAULTS = {
    "title": "",
    "columns": "auto",        # "auto" fits the screen, or a fixed number
    "theme": "midnight",
    "fullscreen": True,
    "show_clock": True,
    "browser": "xdg-open",    # used for apps that have `url:` instead of `command:`
    "shutdown_command": "systemctl poweroff",
    "restart_command": "systemctl reboot",
    "sleep_command": "systemctl suspend",
}
SETTING_KEYS = set(DEFAULTS) | set(COLOR_KEYS)
APP_KEYS = {"name", "command", "url", "action", "icon"}
ACTIONS = {"minimize": "hide the launcher and show the desktop"}   # built-in `action:` values
GROUP_KEYS = {"name", "icon", "apps"}
BACK_ITEM = {"name": "Back", "_back": True}   # first tile on every group screen
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".bmp", ".ico"}
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "tv-launcher" / "icons"
MAX_ICON_BYTES = 5 * 1024 * 1024
OK_COLOR, WARN_COLOR, ERROR_COLOR = "#4cd480", "#ffb84d", "#ff6b6b"

LAUNCH_KEYS = {Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_Select}
BACK_KEYS = {Qt.Key_Escape, Qt.Key_Back, Qt.Key_Backspace}
MOVE_KEYS = {Qt.Key_Left: (-1, 0), Qt.Key_Right: (1, 0), Qt.Key_Up: (0, -1), Qt.Key_Down: (0, 1)}
POINTER_WAKE_DISTANCE = 40   # px the pointer must move before hover takes over from the D-pad
LAUNCH_DEBOUNCE = 1.5        # seconds; stops a double-press opening an app twice


# ---- config -------------------------------------------------------------------

class ConfigError(ValueError):
    def __init__(self, message, line=None):
        super().__init__(message)
        self.line = line   # 1-based line number, when known


def find_config(argv):
    if len(argv) > 1:
        return Path(argv[1]).expanduser().resolve()
    user = Path.home() / ".config" / "tv-launcher" / "apps.yaml"
    if user.exists():
        return user
    return Path(__file__).resolve().with_name("apps.yaml")


def to_argv(command):
    return [str(a) for a in command] if isinstance(command, list) else shlex.split(str(command))


def theme_label(name):
    return THEME_LABELS.get(name, name.title())


def _likely_mistake(text, error_line):
    """The parser often notices a problem lines after the real mistake. Look for the usual suspects."""
    for number, line in enumerate(text.splitlines(), 1):
        if error_line and number > error_line:
            break
        if re.match(r"^ *\t", line):
            return number, f"Line {number} is indented with a tab. YAML needs spaces."
        code = re.sub(r"(^|\s)#.*$", "", line)   # drop comments
        if code.replace("\\\"", "").count("\"") % 2:
            return number, f"Line {number} has an unclosed \" quote."
        if code.strip().startswith("'") and code.count("'") % 2:
            return number, f"Line {number} has an unclosed ' quote."
    return None


def resolve_settings(raw, warnings):
    settings = dict(DEFAULTS)
    settings.update(raw)
    for key in raw:
        if key not in SETTING_KEYS:
            warnings.append(f"Unknown setting '{key}' (ignored)")

    theme = str(settings["theme"]).lower()
    if theme not in THEMES:
        warnings.append(f"Unknown theme '{settings['theme']}'. Options: {', '.join(THEMES)}")
        theme = "midnight"
    settings["theme"] = theme
    settings.update(THEMES[theme])
    settings["color_overrides"] = []
    for key in COLOR_KEYS:
        if key in raw:
            if QColor.isValidColorName(str(raw[key])):
                settings[key] = str(raw[key])
                settings["color_overrides"].append(key)
            else:
                warnings.append(f"'{key}: {raw[key]}' isn't a colour (use e.g. \"#3d9cff\")")
                settings[key] = THEMES[theme][key]

    if not (settings["columns"] == "auto" or (isinstance(settings["columns"], int) and settings["columns"] > 0)):
        warnings.append(f"'columns: {settings['columns']}' should be auto or a number")
        settings["columns"] = "auto"
    for key in ("fullscreen", "show_clock"):
        if not isinstance(settings[key], bool):
            warnings.append(f"'{key}' should be true or false")
            settings[key] = DEFAULTS[key]
    return settings


def parse_config(text, base_dir):
    """Validate config text. Returns (settings, apps, warnings); raises ConfigError."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = mark.line + 1 if mark else None
        hint = _likely_mistake(text, line)
        if hint:
            raise ConfigError(hint[1], hint[0])
        problem = getattr(e, "problem", None) or str(e)
        where = f" on line {line}" if line else ""
        raise ConfigError(f"YAML syntax error{where}: {problem}", line)
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError("The file should have 'settings:' and 'apps:' sections", 1)

    warnings = []
    for key in data:
        if key not in ("settings", "apps"):
            warnings.append(f"Unknown section '{key}' (ignored)")
    raw = data.get("settings") or {}
    if not isinstance(raw, dict):
        raise ConfigError("'settings:' should contain 'key: value' lines")
    settings = resolve_settings(raw, warnings)

    items = data.get("apps") or []
    if not isinstance(items, list):
        raise ConfigError("'apps:' should be a list (each app starts with '- name:')")
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        root = None
    missing = []   # "not found" warnings go last so typos stay visible
    apps = _check_items(items, _child_nodes(root), "", base_dir, warnings, missing)
    return settings, apps, warnings + missing


def _child_nodes(node):
    """The YAML nodes of the items under a mapping's `apps:` key (used for line numbers)."""
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if getattr(key, "value", None) == "apps" and isinstance(value, yaml.SequenceNode):
                return value.value
    return []


def is_web_url(spec):
    return bool(re.match(r"^https?://", str(spec), re.IGNORECASE))


def _check_icon(app, label, base_dir, warnings, missing):
    icon = app.get("icon")
    if not icon or is_web_url(icon):
        return   # optional; web images are checked when they download
    if "://" in str(icon):
        warnings.append(f"{label}: icon link should start with https:// (or use a local file)")
    elif Path(str(icon)).suffix.lower() in IMAGE_SUFFIXES:
        path = Path(str(icon)).expanduser()
        if not (path if path.is_absolute() else base_dir / path).is_file():
            missing.append(f"{label}: icon file '{icon}' not found")


def _check_items(items, nodes, group, base_dir, warnings, missing):
    """Validate a list of apps and groups (an entry with its own `apps:` list), recursively."""
    apps = []
    for i, app in enumerate(items):
        node = nodes[i] if i < len(nodes) else None
        line = node.start_mark.line + 1 if node is not None else None
        at = f" (line {line})" if line else ""
        inside = f" in {group}" if group else ""
        if not isinstance(app, dict):
            raise ConfigError(f"App #{i + 1}{inside}{at} should have 'name:' and 'command:' or 'url:'", line)
        if not app.get("name"):
            raise ConfigError(f"App #{i + 1}{inside}{at} needs a 'name:'", line)
        label = f"'{app['name']}'{inside}{at}"

        if "apps" in app:
            children = app["apps"] if app["apps"] is not None else []
            if not isinstance(children, list):
                raise ConfigError(f"Group {label}: 'apps:' should be a list (each app starts with '- name:')", line)
            if not children:
                warnings.append(f"Group {label} has no apps yet")
            if app.get("command") or app.get("url"):
                warnings.append(f"Group {label}: command/url ignored (a group opens its own apps)")
            for key in app:
                if key not in GROUP_KEYS | {"command", "url"}:
                    warnings.append(f"Group {label}: unknown key '{key}'")
            _check_icon(app, f"Group {label}", base_dir, warnings, missing)
            path = f"{group} › {app['name']}" if group else str(app["name"])
            apps.append({**app, "apps": _check_items(children, _child_nodes(node), path, base_dir, warnings, missing)})
            continue

        if not (app.get("command") or app.get("url") or app.get("action")):
            raise ConfigError(f"App {label} needs a 'command:' or 'url:' (or 'apps:' to make it a group)", line)
        if app.get("action"):
            if str(app["action"]) not in ACTIONS:
                raise ConfigError(f"App {label}: unknown action '{app['action']}'. Options: {', '.join(ACTIONS)}", line)
            if app.get("command") or app.get("url"):
                warnings.append(f"App {label}: has an action, so command/url is ignored")
            for key in app:
                if key not in APP_KEYS:
                    warnings.append(f"App {label}: unknown key '{key}'")
            _check_icon(app, f"App {label}", base_dir, warnings, missing)
            apps.append(app)
            continue
        if app.get("command") and app.get("url"):
            warnings.append(f"App {label} has both command and url; command is used")
        for key in app:
            if key not in APP_KEYS:
                warnings.append(f"App {label}: unknown key '{key}'")
        if app.get("command"):
            try:
                argv = to_argv(app["command"])
            except ValueError as e:
                raise ConfigError(f"App {label}: can't read command ({e})", line)
            if not argv:
                raise ConfigError(f"App {label}: command is empty", line)
            if not shutil.which(argv[0]) and not Path(argv[0]).expanduser().exists():
                missing.append(f"App {label}: program '{argv[0]}' not found")
        elif "://" not in str(app["url"]):
            warnings.append(f"App {label}: url should start with https://")
        _check_icon(app, f"App {label}", base_dir, warnings, missing)
        apps.append(app)
    return apps


def load_config(path):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Config not found: {path}")
    return parse_config(text, path.parent)


def set_setting(text, key, value):
    """Set `key: value` under `settings:` while keeping the rest of the file (and comments) as-is."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not re.match(r"^settings:\s*(#.*)?$", line):
            continue
        indent = "  "
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j][0] in " \t#"):
            match = re.match(rf"^(\s+{re.escape(key)}:[ \t]*)([^#\r\n]*?)([ \t]*#.*)?(\r?\n)?$", lines[j])
            if match:
                lines[j] = f"{match.group(1)}{value}{match.group(3) or ''}{match.group(4) or ''}"
                return "".join(lines)
            if lines[j].strip() and not lines[j].lstrip().startswith("#"):
                indent = re.match(r"^(\s*)", lines[j]).group(1)
            j += 1
        lines.insert(i + 1, f"{indent}{key}: {value}\n")
        return "".join(lines)
    return f"settings:\n  {key}: {value}\n\n" + text


def write_config(path, text):
    """Save atomically, keeping the previous version as apps.yaml.bak."""
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def load_icon(spec, base_dir):
    """Icon can be an image path (relative to the YAML) or a theme icon name."""
    if not spec:
        return None
    path = Path(str(spec)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    if path.is_file():
        pix = QPixmap(str(path))
        return None if pix.isNull() else pix
    icon = QIcon.fromTheme(str(spec))   # e.g. "tv.kodi.Kodi", "steam", "org.mozilla.firefox"
    if icon.isNull():
        return None
    pix = icon.pixmap(512, 512)
    return None if pix.isNull() else pix


class WebIcons(QObject):
    """Downloads icon images in the background and keeps them on disk, so they also work offline."""
    loaded = Signal(str, QPixmap)
    failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.net = QNetworkAccessManager(self)
        self.pending = set()
        self.broken = {}   # url -> reason; not retried until retry_failed()

    @staticmethod
    def cache_path(url):
        return CACHE_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".img")

    def get(self, url):
        """Return the cached image, or None and start downloading it (`loaded` fires when done)."""
        path = self.cache_path(url)
        if path.is_file():
            pix = QPixmap()
            if pix.loadFromData(path.read_bytes()):
                return pix
        if url not in self.pending and url not in self.broken:
            self.pending.add(url)
            request = QNetworkRequest(QUrl(url))
            request.setTransferTimeout(15_000)
            request.setHeader(QNetworkRequest.UserAgentHeader, "tv-launcher")
            reply = self.net.get(request)
            reply.downloadProgress.connect(lambda got, _total, r=reply: self._limit(r, got))
            reply.finished.connect(lambda r=reply, u=url: self._done(r, u))
        return None

    def retry_failed(self):
        self.broken.clear()

    def _limit(self, reply, received):
        if received > MAX_ICON_BYTES:
            reply.setProperty("too_big", True)
            reply.abort()

    def _done(self, reply, url):
        self.pending.discard(url)
        reply.deleteLater()
        if reply.property("too_big"):
            reason = "image is bigger than 5 MB"
        elif reply.error() != QNetworkReply.NoError:
            status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            phrase = reply.attribute(QNetworkRequest.HttpReasonPhraseAttribute) or ""
            reason = f"the website replied {status} {phrase}".strip() if status else reply.errorString()
        else:
            data = bytes(reply.readAll())
            pix = QPixmap()
            if pix.loadFromData(data):
                try:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    tmp = self.cache_path(url).with_suffix(".tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp, self.cache_path(url))
                except OSError:
                    pass   # still show it, just not cached
                self.loaded.emit(url, pix)
                return
            reason = "the link isn't an image"
        self.broken[url] = reason
        self.failed.emit(url, reason)


def fit_grid(n, width, height, gap, columns):
    """Pick the column count that gives the biggest square tiles."""
    if n == 0:
        return 1, 0
    options = [int(columns)] if str(columns).isdigit() and int(columns) > 0 else range(1, n + 1)
    best_cols, best_side = 1, 0
    for cols in options:
        rows = math.ceil(n / cols)
        side = min((width - gap * (cols - 1)) / cols, (height - gap * (rows - 1)) / rows)
        if side > best_side:
            best_cols, best_side = cols, side
    # Don't let a handful of apps become enormous.
    return best_cols, int(max(0, min(best_side, height * 0.5)))


def repolish(*widgets):
    for widget in widgets:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


# ---- home screen --------------------------------------------------------------

class Tile(QFrame):
    def __init__(self, app, base_dir, launcher):
        super().__init__()
        self.app = app
        self.launcher = launcher
        self.setObjectName("tile")
        self.setMouseTracking(True)
        self.setProperty("focused", False)
        self.is_back = bool(app.get("_back"))
        self.is_group = "apps" in app
        self.side = 0
        self.pixmap = launcher.icon_for(app.get("icon"), base_dir)

        self.icon = QLabel(alignment=Qt.AlignCenter)
        self.icon.setObjectName("icon")
        self.name = QLabel(str(app["name"]), alignment=Qt.AlignCenter)
        self.name.setObjectName("name")
        count = len(app["apps"]) if self.is_group else 0
        self.detail = QLabel(f"{count} app{'' if count == 1 else 's'}  ›", alignment=Qt.AlignCenter)
        self.detail.setObjectName("detail")
        self.detail.setVisible(self.is_group)
        for label in (self.icon, self.name, self.detail):
            label.setAttribute(Qt.WA_TransparentForMouseEvents)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(self.icon)
        layout.addWidget(self.name)
        layout.addWidget(self.detail)
        layout.addStretch(1)

    def set_pixmap(self, pixmap):
        """Swap in an icon that finished downloading."""
        self.pixmap = pixmap
        if self.side:
            self.set_side(self.side)

    def set_side(self, side):
        self.side = side
        self.setFixedSize(side, side)
        icon_side = int(side * (0.5 if self.is_group else 0.58))
        font = self.name.font()
        font.setPixelSize(max(12, int(side * 0.085)))
        font.setBold(True)
        self.name.setFont(font)
        small = self.detail.font()
        small.setPixelSize(max(10, int(side * 0.06)))
        self.detail.setFont(small)
        if self.pixmap:
            self.icon.setPixmap(self.pixmap.scaled(
                icon_side, icon_side, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            # No image: show the first letter as a big placeholder.
            letter = self.icon.font()
            letter.setPixelSize(max(16, int(icon_side * 0.6)))
            letter.setBold(True)
            self.icon.setFont(letter)
            self.icon.setText("‹" if self.is_back else str(self.app["name"])[:1].upper())
        self.icon.setFixedHeight(icon_side)

    def set_focused(self, focused):
        self.setProperty("focused", focused)
        repolish(self, self.name)   # children don't restyle on their own

    def enterEvent(self, event):
        self.pointer_hover()
        super().enterEvent(event)

    def pointer_hover(self):
        self.launcher.hover(self)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.launcher.click(self)
        elif event.button() == Qt.BackButton:
            self.launcher.go_up()


class GearButton(QWidget):
    """Settings button, drawn by hand so it looks the same on every system."""

    def __init__(self, launcher):
        super().__init__()
        self.launcher = launcher
        self.focused = False
        self.setMouseTracking(True)
        self.setToolTip("Settings")

    def set_focused(self, focused):
        self.focused = focused
        self.update()

    def paintEvent(self, event):
        s = self.launcher.settings
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.focused:
            p.setPen(QPen(QColor(s["accent"]), 3))
            p.setBrush(QColor(s["tile"]))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 12, 12)
        c = QPointF(self.width() / 2, self.height() / 2)
        r = min(self.width(), self.height()) * 0.34
        gear = QPainterPath()
        gear.addEllipse(c, r * 0.78, r * 0.78)
        for i in range(8):
            tooth = QPainterPath()
            tooth.addRoundedRect(QRectF(-r * 0.17, -r, r * 0.34, r * 0.5), r * 0.06, r * 0.06)
            gear = gear.united(QTransform().translate(c.x(), c.y()).rotate(i * 45).map(tooth))
        hole = QPainterPath()
        hole.addEllipse(c, r * 0.32, r * 0.32)
        p.fillPath(gear.subtracted(hole), QColor(s["accent"] if self.focused else s["text"]))

    def enterEvent(self, event):
        self.pointer_hover()
        super().enterEvent(event)

    def pointer_hover(self):
        self.launcher.hover_gear()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.launcher.open_menu()


# ---- settings menu ------------------------------------------------------------

class MenuItem(QLabel):
    def __init__(self, text, index, menu):
        super().__init__(text)
        self.index = index
        self.menu = menu
        self.setObjectName("menuItem")
        self.setProperty("focused", False)
        self.setMouseTracking(True)

    def set_focused(self, focused):
        self.setProperty("focused", focused)
        repolish(self)

    def enterEvent(self, event):
        self.pointer_hover()
        super().enterEvent(event)

    def pointer_hover(self):
        if self.menu.launcher.pointer_mode:
            self.menu.select(self.index)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.menu.click(self.index)


class MenuOverlay(QWidget):
    """A dimmed overlay with a list of options. Pages stack: Back returns to the previous one."""

    def __init__(self, launcher):
        super().__init__(launcher)
        self.launcher = launcher
        self.setObjectName("overlay")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

        self.panel = QFrame(self)
        self.panel.setObjectName("panel")
        self.heading = QLabel(objectName="menuTitle")
        self.heading.setWordWrap(True)
        self.list = QVBoxLayout()
        self.list.setSpacing(4)
        self.hint = QLabel("OK to choose  ·  Back to return", objectName="hint", alignment=Qt.AlignCenter)
        layout = QVBoxLayout(self.panel)
        layout.addWidget(self.heading)
        layout.addLayout(self.list)
        layout.addWidget(self.hint)

        self.pages = []   # each: {"title", "entries": [(label, callback)], "selected"}
        self.items = []
        self.hide()

    def open_page(self, title, entries, selected=0, replace=False):
        page = {"title": title, "entries": entries, "selected": selected}
        if replace and self.pages:
            self.pages[-1] = page
        else:
            self.pages.append(page)
        self.render()
        self.show()
        self.raise_()
        self.setFocus()

    def render(self):
        page = self.pages[-1]
        self.heading.setText(page["title"])
        for item in self.items:
            self.list.removeWidget(item)
            item.deleteLater()
        self.items = [MenuItem(label, i, self) for i, (label, _) in enumerate(page["entries"])]
        for item in self.items:
            self.list.addWidget(item)
            item.show()   # the panel may already be visible; size it with the new items
        self.select(page["selected"])
        self.place()

    def select(self, index):
        self.pages[-1]["selected"] = index
        for item in self.items:
            item.set_focused(item.index == index)

    def activate(self, index=None):
        page = self.pages[-1]
        index = page["selected"] if index is None else index
        page["entries"][index][1]()

    def click(self, index):
        # Same rule as the tiles: with the pointer hidden, a click means "the highlighted one".
        self.activate(index if self.launcher.pointer_mode else None)

    def back(self):
        self.pages.pop()
        if self.pages:
            self.render()
        else:
            self.close_menu()

    def close_menu(self):
        self.pages.clear()
        self.hide()
        self.launcher.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if not self.items:
            return
        selected = self.pages[-1]["selected"]
        if key in (Qt.Key_Up, Qt.Key_Down):
            self.launcher.enter_dpad_mode()
            self.select((selected + (1 if key == Qt.Key_Down else -1)) % len(self.items))
        elif key in LAUNCH_KEYS:
            self.launcher.enter_dpad_mode()
            self.activate()
        elif key in BACK_KEYS or key == Qt.Key_Left:
            self.back()
        elif key == Qt.Key_Menu:
            self.close_menu()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and not self.panel.geometry().contains(event.position().toPoint()):
            self.close_menu()

    def place(self):
        parent = self.parentWidget().rect()
        self.setGeometry(parent)
        width = min(parent.width() - 32, max(420, int(parent.width() * 0.34)))
        self.panel.setFixedWidth(width)
        self.panel.ensurePolished()
        for item in self.items:
            item.ensurePolished()
        self.panel.layout().activate()
        self.panel.adjustSize()
        self.panel.move((parent.width() - width) // 2, (parent.height() - self.panel.height()) // 2)


# ---- YAML editor --------------------------------------------------------------

class YamlHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.rules = []

    def set_colors(self, key_color, string_color, comment_color):
        def fmt(color, bold=False):
            f = QTextCharFormat()
            f.setForeground(color)
            if bold:
                f.setFontWeight(QFont.Bold)
            return f
        self.rules = [
            (QRegularExpression(r"^\s*(-\s+)?[^\s#:'\"][^#:]*(?=:(\s|$))"), fmt(key_color, True)),
            (QRegularExpression(r"^\s*-(?=\s)"), fmt(key_color, True)),
            (QRegularExpression(r"\"[^\"]*\"|'[^']*'"), fmt(string_color)),
            (QRegularExpression(r"(^|\s)#.*$"), fmt(comment_color)),
        ]
        self.rehighlight()

    def highlightBlock(self, text):
        for regex, fmt in self.rules:
            matches = regex.globalMatch(text)
            while matches.hasNext():
                match = matches.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


class LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_gutter(event)


class CodeEdit(QPlainTextEdit):
    """Plain text editor with line numbers, YAML-friendly Tab/Enter and an error-line marker."""

    def __init__(self):
        super().__init__()
        self.setObjectName("yaml")
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.gutter = LineNumbers(self)
        self.error_line = None
        self.gutter_bg, self.gutter_fg = QColor("#000"), QColor("#888")
        self.blockCountChanged.connect(self.update_gutter_width)
        self.updateRequest.connect(self.update_gutter)
        self.update_gutter_width()

    def gutter_width(self):
        digits = max(2, len(str(self.blockCount())))
        return 20 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_gutter_width(self, *_):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_gutter_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, event):
        p = QPainter(self.gutter)
        p.fillRect(event.rect(), self.gutter_bg)
        p.setFont(self.font())
        block = self.firstVisibleBlock()
        number = block.blockNumber() + 1
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                p.setPen(QColor(ERROR_COLOR) if number == self.error_line else self.gutter_fg)
                p.drawText(0, top, self.gutter.width() - 10, self.fontMetrics().height(), Qt.AlignRight, str(number))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Tab:   # YAML doesn't allow tabs
            self.insertPlainText("  ")
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):   # keep the current indentation
            line = self.textCursor().block().text()
            indent = re.match(r"^\s*", line).group(0)
            if line.rstrip().endswith(":"):
                indent += "  "
            super().keyPressEvent(event)
            self.insertPlainText(indent)
            return
        super().keyPressEvent(event)


class EditorOverlay(QWidget):
    def __init__(self, launcher):
        super().__init__(launcher)
        self.launcher = launcher
        self.setObjectName("editor")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.valid = False
        self.discard_armed_until = 0.0

        self.title = QLabel(objectName="editorTitle")
        self.path_label = QLabel(objectName="hint")
        self.edit = CodeEdit()
        self.edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.highlighter = YamlHighlighter(self.edit.document())
        self.result = QLabel(objectName="result")
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.PlainText)

        self.save_button = QPushButton("Save  (Ctrl+S)", clicked=self.save)
        self.revert_button = QPushButton("Revert", clicked=self.revert)
        self.close_button = QPushButton("Close  (Esc)", clicked=self.request_close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.result, 1)
        for button in (self.save_button, self.revert_button, self.close_button):
            button.setObjectName("button")
            buttons.addWidget(button)

        header = QHBoxLayout()
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.path_label)
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.edit, 1)
        layout.addLayout(buttons)

        self.timer = QTimer(self, singleShot=True, interval=350, timeout=self.validate)
        self.edit.textChanged.connect(self.timer.start)
        QShortcut(QKeySequence.Save, self, activated=self.save, context=Qt.WidgetWithChildrenShortcut)
        self.hide()

    def open(self):
        path = self.launcher.config_path
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = "settings:\n  title: \"Living Room\"\n\napps:\n"
        self.edit.setPlainText(text)
        self.edit.document().setModified(False)
        self.edit.moveCursor(QTextCursor.Start)
        self.title.setText(f"Edit {path.name}")
        self.path_label.setText(str(path))
        self.validate()
        self.place()
        self.show()
        self.raise_()
        self.edit.setFocus()

    def place(self):
        self.setGeometry(self.parentWidget().rect())

    def validate(self):
        try:
            _, apps, warnings = parse_config(self.edit.toPlainText(), self.launcher.config_path.parent)
        except ConfigError as e:
            self.valid = False
            self.show_result(f"✗  {e}", ERROR_COLOR, e.line)
        else:
            self.valid = True
            if warnings:
                shown = "\n".join(f"•  {w}" for w in warnings[:5])
                more = f"\n   …and {len(warnings) - 5} more" if len(warnings) > 5 else ""
                self.show_result(f"✓  Valid: {len(apps)} apps, {len(warnings)} warning(s)\n{shown}{more}", WARN_COLOR)
            else:
                self.show_result(f"✓  Valid: {len(apps)} apps", OK_COLOR)
        self.save_button.setEnabled(self.valid)

    def show_result(self, text, color, line=None):
        self.result.setText(text)
        self.result.setStyleSheet(f"color: {color};")
        self.edit.error_line = line
        self.edit.gutter.update()
        selections = []
        if line:
            block = self.edit.document().findBlockByNumber(line - 1)
            if block.isValid():
                selection = QTextEdit.ExtraSelection()
                fmt = QTextCharFormat()
                fmt.setBackground(QColor(255, 80, 80, 70))
                fmt.setProperty(QTextFormat.FullWidthSelection, True)
                selection.format = fmt
                selection.cursor = QTextCursor(block)
                selections.append(selection)
        self.edit.setExtraSelections(selections)

    def save(self):
        self.timer.stop()
        self.validate()
        if not self.valid:
            return
        try:
            write_config(self.launcher.config_path, self.edit.toPlainText())
        except OSError as e:
            self.show_result(f"✗  Couldn't save: {e}", ERROR_COLOR)
            return
        self.edit.document().setModified(False)
        self.launcher.reload()
        self.result.setText("Saved.  " + self.result.text())

    def revert(self):
        self.open()

    def request_close(self):
        if self.edit.document().isModified() and time.monotonic() > self.discard_armed_until:
            self.discard_armed_until = time.monotonic() + 4
            self.show_result("Unsaved changes. Press Esc/Back again to discard them, or Ctrl+S to save.",
                             WARN_COLOR, self.edit.error_line)
            return
        self.hide()
        self.launcher.setFocus()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Back):
            self.request_close()
        else:
            super().keyPressEvent(event)


# ---- main window --------------------------------------------------------------

class Launcher(QWidget):
    def __init__(self, config_path):
        super().__init__()
        self.config_path = config_path
        self.settings = resolve_settings({}, [])
        self.apps = []             # whole tree from the YAML
        self.path = []             # names of the groups we're inside, outermost first
        self.parent_indexes = []   # where focus was on each parent screen
        self.tiles = []
        self.index = 0
        self.cols = 1
        self.focus_area = "grid"   # or "gear"
        self.last_launch = 0.0
        self.pointer_mode = True
        self.anchor = QPoint()

        self.setWindowTitle("TV Launcher")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

        self.title = QLabel(objectName="title")
        self.clock = QLabel(objectName="clock")
        self.gear = GearButton(self)
        header = QHBoxLayout()
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.clock)
        header.addWidget(self.gear)

        self.grid = QWidget()
        self.grid.setMouseTracking(True)
        self.status = QLabel(objectName="status", alignment=Qt.AlignCenter)

        root = QVBoxLayout(self)
        root.addLayout(header)
        root.addWidget(self.grid, 1)
        root.addWidget(self.status)

        self.web_icons = WebIcons(self)
        self.web_icons.loaded.connect(self.icon_loaded)
        self.web_icons.failed.connect(self.icon_failed)
        self.menu = MenuOverlay(self)
        self.editor = EditorOverlay(self)

        self.clock_timer = QTimer(self, timeout=self.update_clock, interval=15_000)
        self.status_timer = QTimer(self, singleShot=True, timeout=lambda: self.status.setText(""))
        self.pointer_timer = QTimer(self, timeout=self.check_pointer, interval=100)

        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(lambda _: QTimer.singleShot(300, self.reload))

        QShortcut(QKeySequence("Ctrl+Q"), self, activated=QApplication.quit)
        self.reload()

    # ---- config ---------------------------------------------------------------

    def reload(self):
        # Editors often replace the file on save, which drops it from the watcher.
        if self.config_path.exists() and str(self.config_path) not in self.watcher.files():
            self.watcher.addPath(str(self.config_path))
        try:
            settings, apps, warnings = load_config(self.config_path)
        except ConfigError as e:
            self.apply_style()
            self.show_status(f"{e}  (Settings › Edit apps to fix)", level="error", sticky=True)
            return
        self.settings = settings
        self.apps = apps
        self.web_icons.retry_failed()   # a reload is the way to retry broken icon links
        self.build_tiles()

        self.clock.setVisible(bool(settings["show_clock"]))
        if settings["show_clock"]:
            self.update_clock()
            self.clock_timer.start()
        else:
            self.clock_timer.stop()
        self.apply_style()
        self.layout_tiles()
        self.refresh_focus()
        if not self.apps:
            self.show_status("No apps yet. Open Settings › Edit apps to add some.", sticky=True)
        elif warnings:
            self.show_status(f"{len(warnings)} warning(s): {warnings[0]}  (see Settings › Edit apps)", level="warn")
        else:
            self.status.setText("")

    def current_items(self):
        """Items on the current screen. Drops groups from the path that no longer exist (after a reload)."""
        items = self.apps
        for depth, name in enumerate(self.path):
            group = next((a for a in items if "apps" in a and a["name"] == name), None)
            if group is None:
                self.index = self.parent_indexes[depth]
                del self.path[depth:], self.parent_indexes[depth:]
                break
            items = group["apps"]
        return [BACK_ITEM] + items if self.path else items

    def build_tiles(self):
        for tile in self.tiles:
            tile.deleteLater()
        self.tiles = [Tile(app, self.config_path.parent, self) for app in self.current_items()]
        for tile in self.tiles:
            tile.setParent(self.grid)
            tile.show()
        self.index = min(self.index, max(0, len(self.tiles) - 1))
        if not self.tiles:
            self.focus_area = "gear"
        self.title.setText("  ›  ".join([str(self.settings["title"]), *self.path]).strip(" ›"))
        self.layout_tiles()
        self.refresh_focus()

    def icon_for(self, spec, base_dir):
        if spec and is_web_url(spec):
            return self.web_icons.get(str(spec))
        return load_icon(spec, base_dir)

    def icon_loaded(self, url, pixmap):
        for tile in self.tiles:
            if str(tile.app.get("icon")) == url:
                tile.set_pixmap(pixmap)

    def icon_failed(self, url, reason):
        def find(items):
            for app in items:
                if str(app.get("icon")) == url:
                    return app["name"]
                found = find(app.get("apps") or [])
                if found:
                    return found
        self.show_status(f"Couldn't download the icon for {find(self.apps) or url}: {reason}", level="warn")

    def enter_group(self, tile):
        self.parent_indexes.append(self.index)
        self.path.append(tile.app["name"])
        self.index = 1 if tile.app["apps"] else 0   # first app, not the Back tile
        self.build_tiles()

    def go_up(self):
        if not self.path:
            return False
        self.path.pop()
        self.index = self.parent_indexes.pop()
        self.focus_area = "grid"
        self.build_tiles()
        return True

    def apply_style(self):
        s = self.settings
        h = max(self.height(), 480)
        muted = QColor(s["text"])
        muted.setAlpha(150)
        muted_css = f"rgba({muted.red()}, {muted.green()}, {muted.blue()}, 0.6)"
        self.setStyleSheet(f"""
            Launcher {{ background: {s['background']}; }}
            QLabel {{ color: {s['text']}; background: transparent; }}
            QLabel#title, QLabel#clock {{ font-size: {int(h * 0.04)}px; font-weight: 600; padding: 8px 16px; }}
            QLabel#status {{ font-size: {int(h * 0.025)}px; padding: 8px; }}
            QLabel#status[level="warn"] {{ color: {WARN_COLOR}; }}
            QLabel#status[level="error"] {{ color: {ERROR_COLOR}; }}
            QFrame#tile {{ background: {s['tile']}; border: 4px solid transparent; border-radius: 18px; }}
            QFrame#tile[focused="true"] {{ border-color: {s['accent']}; }}
            QFrame#tile[focused="true"] QLabel#name {{ color: {s['accent']}; }}
            QLabel#detail {{ color: {muted_css}; }}

            QWidget#overlay {{ background: rgba(0, 0, 0, 150); }}
            QFrame#panel {{ background: {s['background']}; border: 2px solid {s['tile']}; border-radius: 20px; }}
            QLabel#menuTitle {{ font-size: {int(h * 0.034)}px; font-weight: 700; padding: 12px 16px 8px; }}
            QLabel#menuItem {{ font-size: {int(h * 0.03)}px; padding: {int(h * 0.012)}px 20px;
                               border: 3px solid transparent; border-radius: 12px; }}
            QLabel#menuItem[focused="true"] {{ background: {s['tile']}; border-color: {s['accent']}; color: {s['accent']}; }}
            QLabel#hint {{ color: {muted_css}; font-size: {int(h * 0.018)}px; padding: 8px; }}

            QWidget#editor {{ background: {s['background']}; }}
            QLabel#editorTitle {{ font-size: {int(h * 0.032)}px; font-weight: 700; padding: 4px 8px; }}
            QLabel#result {{ font-size: {int(h * 0.02)}px; padding: 4px 8px; }}
            QPlainTextEdit#yaml {{ background: {s['tile']}; color: {s['text']}; border: 2px solid {s['tile']};
                                   border-radius: 10px; font-size: {int(h * 0.022)}px;
                                   selection-background-color: {s['accent']}; }}
            QPlainTextEdit#yaml:focus {{ border-color: {s['accent']}; }}
            QPushButton#button {{ background: {s['tile']}; color: {s['text']}; font-size: {int(h * 0.022)}px;
                                  padding: 10px 22px; border: 3px solid transparent; border-radius: 10px; }}
            QPushButton#button:focus, QPushButton#button:hover {{ border-color: {s['accent']}; }}
            QPushButton#button:disabled {{ color: {muted_css}; }}
        """)
        side = int(h * 0.04 * 1.9)
        self.gear.setFixedSize(side, side)
        self.gear.update()
        tile = QColor(s["tile"])
        self.editor.edit.gutter_bg = tile.darker(115) if tile.lightness() > 128 else tile.lighter(115)
        self.editor.edit.gutter_fg = muted
        accent = QColor(s["accent"])
        string_color = accent.lighter(130) if QColor(s["background"]).lightness() < 128 else accent.darker(130)
        self.editor.highlighter.set_colors(accent, string_color, muted)
        if self.menu.isVisible():
            self.menu.place()

    # ---- layout ---------------------------------------------------------------

    def layout_tiles(self):
        w, h = self.grid.width(), self.grid.height()
        gap = max(16, int(min(w, h) * 0.03))
        self.cols, side = fit_grid(len(self.tiles), w - 2 * gap, h - gap, gap, self.settings["columns"])
        if not self.tiles or side <= 0:
            return
        rows = math.ceil(len(self.tiles) / self.cols)
        top = (h - (rows * side + (rows - 1) * gap)) // 2
        for i, tile in enumerate(self.tiles):
            row, col = divmod(i, self.cols)
            in_row = min(self.cols, len(self.tiles) - row * self.cols)   # center a short last row
            left = (w - (in_row * side + (in_row - 1) * gap)) // 2
            tile.set_side(side)
            tile.move(left + col * (side + gap), top + row * (side + gap))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.apply_style()
        self.editor.place()
        QTimer.singleShot(0, self.layout_tiles)   # let the grid widget get its new size first

    # ---- navigation -----------------------------------------------------------

    def refresh_focus(self):
        for i, tile in enumerate(self.tiles):
            tile.set_focused(self.focus_area == "grid" and i == self.index)
        self.gear.set_focused(self.focus_area == "gear")

    def move_focus(self, dx, dy):
        if self.focus_area == "gear":
            if (dy > 0 or dx < 0) and self.tiles:
                self.focus_area = "grid"
                self.refresh_focus()
            return
        n = len(self.tiles)
        if not n:
            return
        i = self.index
        if dx:
            i = max(0, min(n - 1, i + dx))
        if dy:
            target = i + dy * self.cols
            if 0 <= target < n:
                i = target
            elif dy < 0:
                self.focus_area = "gear"   # Up from the top row reaches Settings
            elif i // self.cols < (n - 1) // self.cols:
                i = n - 1   # short last row: drop onto its last tile
        self.index = i
        self.refresh_focus()

    def keyPressEvent(self, event):
        key = event.key()
        if key in MOVE_KEYS:
            self.enter_dpad_mode()
            self.move_focus(*MOVE_KEYS[key])
        elif key in LAUNCH_KEYS:
            self.enter_dpad_mode()
            if self.focus_area == "gear":
                self.open_menu()
            elif self.tiles:
                self.launch(self.tiles[self.index])
        elif key == Qt.Key_Menu:
            self.open_menu()
        elif key in BACK_KEYS:
            self.go_up()   # leaves a group; does nothing on the home screen
        elif key in (Qt.Key_Home, Qt.Key_HomePage) and self.tiles:
            self.focus_area = "grid"
            self.index = 0
            self.refresh_focus()
        elif key == Qt.Key_F5:
            self.reload()
            self.show_status("Reloaded")
        else:
            super().keyPressEvent(event)

    # Air mice jitter. Once the D-pad is used, hide the pointer and ignore hover
    # until the pointer is deliberately moved a decent distance.
    def enter_dpad_mode(self):
        if self.pointer_mode:
            self.pointer_mode = False
            QApplication.setOverrideCursor(Qt.BlankCursor)
            self.pointer_timer.start()
        self.anchor = QCursor.pos()

    def check_pointer(self):
        if (QCursor.pos() - self.anchor).manhattanLength() > POINTER_WAKE_DISTANCE:
            self.wake_pointer()
            under = QApplication.widgetAt(QCursor.pos())
            while under and not hasattr(under, "pointer_hover"):
                under = under.parentWidget()
            if under:
                under.pointer_hover()

    def wake_pointer(self):
        if not self.pointer_mode:
            self.pointer_mode = True
            self.pointer_timer.stop()
            QApplication.restoreOverrideCursor()

    def hover(self, tile):
        if self.pointer_mode and tile in self.tiles:
            self.focus_area = "grid"
            self.index = self.tiles.index(tile)
            self.refresh_focus()

    def hover_gear(self):
        if self.pointer_mode:
            self.focus_area = "gear"
            self.refresh_focus()

    def click(self, tile):
        # Many air remotes send a left click for OK. With the pointer hidden, a click
        # means "open the highlighted thing", not whatever is under the pointer.
        if self.pointer_mode:
            self.launch(tile)
        elif self.focus_area == "gear":
            self.open_menu()
        else:
            self.launch(self.tiles[self.index])

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and not self.pointer_mode:
            if self.focus_area == "gear":
                self.open_menu()
            elif self.tiles:
                self.launch(self.tiles[self.index])
        elif event.button() == Qt.BackButton:
            self.go_up()
        else:
            super().mouseReleaseEvent(event)

    # ---- launching ------------------------------------------------------------

    def run(self, command, name):
        try:
            argv = to_argv(command)
            subprocess.Popen(argv, start_new_session=True, cwd=Path.home(),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except (OSError, ValueError, IndexError) as e:
            self.show_status(f"Couldn't open {name}: {e}", level="error")
            return False
        return True

    def launch(self, tile):
        now = time.monotonic()
        if now - self.last_launch < LAUNCH_DEBOUNCE:
            return
        self.last_launch = now   # also guards group changes, so a double-press can't open the first app inside
        if tile.is_back:
            self.go_up()
            return
        if tile.is_group:
            self.enter_group(tile)
            return
        app = tile.app
        if app.get("action") == "minimize":
            self.minimize()
            return
        if app.get("command"):
            command = app["command"]
        else:
            command = to_argv(self.settings["browser"]) + [str(app["url"])]
        if self.run(command, app["name"]):
            self.show_status(f"Opening {app['name']}…")

    # ---- settings menu --------------------------------------------------------

    def open_menu(self):
        if self.menu.isVisible() or self.editor.isVisible():
            return
        self.menu.pages.clear()
        self.menu.open_page("Settings", [
            ("Themes  ›", self.open_theme_menu),
            ("Edit apps (YAML)  ›", self.open_editor),
            ("Reload apps", self.menu_reload),
            ("Sleep", lambda: self.power("sleep")),
            ("Restart  ›", lambda: self.confirm("Restart the TV box?", "Restart", lambda: self.power("restart"))),
            ("Shut down  ›", lambda: self.confirm("Shut down the TV box?", "Shut down", lambda: self.power("shutdown"))),
            ("Exit launcher  ›", lambda: self.confirm("Close the launcher?", "Exit", self.exit_launcher)),
            ("Close", self.menu.close_menu),
        ])

    def confirm(self, question, action, callback):
        # Cancel comes first and is selected, so a double-press can't power anything off.
        self.menu.open_page(question, [("Cancel", self.menu.back), (action, callback)])

    def power(self, kind):
        self.menu.close_menu()
        doing = {"shutdown": "Shutting down", "restart": "Restarting", "sleep": "Going to sleep"}[kind]
        if self.run(self.settings[f"{kind}_command"], doing.lower()):
            self.show_status(f"{doing}…", sticky=True)

    def minimize(self):
        # Remember fullscreen ourselves: some window managers drop it while minimized.
        self.was_fullscreen = self.isFullScreen() or bool(self.settings.get("fullscreen", True))
        self.showMinimized()

    def bring_back(self):
        """Restore from minimized (used when "TV Launcher" is opened again from the app menu)."""
        if getattr(self, "was_fullscreen", self.settings.get("fullscreen", True)):
            self.showFullScreen()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def exit_launcher(self):
        self.menu.close_menu()
        QApplication.quit()

    def menu_reload(self):
        self.menu.close_menu()
        self.reload()
        if self.status.property("level") != "error":
            self.show_status("Reloaded")

    def open_theme_menu(self, selected=None, replace=False):
        names = list(THEMES)
        current = self.settings["theme"]
        entries = [(theme_label(n) + ("   (current)" if n == current else ""), lambda n=n: self.choose_theme(n))
                   for n in names]
        self.menu.open_page("Theme", entries, names.index(current) if selected is None else selected, replace)

    def choose_theme(self, name):
        try:
            text = self.config_path.read_text(encoding="utf-8")
            new_text = set_setting(text, "theme", name)
            settings, _, _ = parse_config(new_text, self.config_path.parent)
            if settings["theme"] != name:
                raise ConfigError("couldn't find where to put 'theme:'")
            write_config(self.config_path, new_text)
        except (OSError, ConfigError) as e:
            self.show_status(f"Couldn't save theme: {e}", level="error")
            return
        self.reload()
        if self.settings["color_overrides"]:
            self.show_status("Theme saved, but colours set in the YAML still override it: "
                             + ", ".join(self.settings["color_overrides"]), level="warn")
        self.open_theme_menu(selected=list(THEMES).index(name), replace=True)

    def open_editor(self):
        self.menu.close_menu()
        self.wake_pointer()
        self.editor.open()

    # ---- misc -----------------------------------------------------------------

    def update_clock(self):
        self.clock.setText(QDateTime.currentDateTime().toString("h:mm AP"))

    def show_status(self, text, level="info", sticky=False):
        self.status.setProperty("level", level)
        repolish(self.status)
        self.status.setText(text)
        if sticky:
            self.status_timer.stop()
        else:
            self.status_timer.start(5000)


def instance_name():
    return f"tv-launcher-{os.environ.get('USER') or os.environ.get('USERNAME') or 'user'}"


def ask_running_instance_to_show(name=None):
    """If the launcher is already running, tell it to come back to the front. Returns True if it was."""
    socket = QLocalSocket()
    socket.connectToServer(name or instance_name())
    if not socket.waitForConnected(500):
        return False
    socket.write(b"show")
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def listen_for_other_instances(window, name=None):
    """Opening the launcher again (e.g. from the app menu) brings this one back instead of starting a second copy."""
    name = name or instance_name()
    server = QLocalServer(window)
    QLocalServer.removeServer(name)   # clean up after a crash
    server.newConnection.connect(lambda: (server.nextPendingConnection().deleteLater(), window.bring_back()))
    server.listen(name)
    return server


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("tv-launcher")
    app.setDesktopFileName("tv-launcher")
    if ask_running_instance_to_show():
        return
    window = Launcher(find_config(sys.argv))
    listen_for_other_instances(window)
    if window.settings.get("fullscreen", True):
        window.showFullScreen()
    else:
        window.resize(1280, 720)
        window.show()
    window.activateWindow()
    window.setFocus()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
