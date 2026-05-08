"""PySide6 log viewer for ``dragndoc.log``.

Spawned as a subprocess from the toaster's tray "Log" menu so each click
gets its own QApplication in its own process — no threading conflict
with the pystray main thread.

    python -m dragndoc.log_viewer [path/to/log]

Defaults to ``<data_dir>/logs/dragndoc.log`` if no path is given.

Why Qt and not Tk or a TUI: Qt's text widgets do BiDi reordering via
HarfBuzz, so Hebrew/Arabic log lines render in visual order. Textual /
Rich-based TUIs render into a fixed terminal grid and don't reorder
RTL runs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PySide6.QtCore import QRegularExpression, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QKeySequence,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QTextEdit,
    QToolBar,
    QWidget,
)


TAIL_LINES = 2000
REFRESH_INTERVAL_MS = 750
LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

LEVEL_COLORS: dict[str, str] = {
    "DEBUG": "#7c8597",
    "INFO": "#a5b4fc",
    "WARNING": "#fbbf24",
    "ERROR": "#f87171",
    "CRITICAL": "#f43f5e",
}

FIELD_COLORS: dict[str, str] = {
    "timestamp": "#64748b",
    "logger": "#22d3ee",
    "default_msg": "#cbd5e1",
    "search_match_bg": "#3b82f6",
    "search_match_fg": "#0f172a",
}

# parses "%(asctime)s %(levelname)-7s %(name)s: %(message)s" lines from
# dragndoc/log.py — capture groups: timestamp, level, logger name
LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\w+)\s+([\w.]+):")


def _resolve_log_path(argv: list[str]) -> Path:
    if len(argv) > 1:
        return Path(argv[1])
    from dragndoc.config import get_settings

    return get_settings().logs_dir / "dragndoc.log"


def _parse_level(line: str) -> str | None:
    m = LINE_RE.match(line)
    return m.group(2) if m else None


class LogHighlighter(QSyntaxHighlighter):
    """Color the timestamp, level, and logger fields, plus the message per level."""

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        self.fmt_ts = self._fmt(FIELD_COLORS["timestamp"])
        self.fmt_logger = self._fmt(FIELD_COLORS["logger"])
        self.fmt_level = {lvl: self._fmt(LEVEL_COLORS[lvl], bold=True) for lvl in LEVELS}
        self.fmt_msg = {lvl: self._fmt(LEVEL_COLORS[lvl]) for lvl in LEVELS}
        self.fmt_msg_default = self._fmt(FIELD_COLORS["default_msg"])

    @staticmethod
    def _fmt(color: str, *, bold: bool = False) -> QTextCharFormat:
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        if bold:
            f.setFontWeight(QFont.Weight.Bold)
        return f

    def highlightBlock(self, text: str) -> None:
        m = LINE_RE.match(text)
        if not m:
            self.setFormat(0, len(text), self.fmt_msg_default)
            return
        level = m.group(2)
        self.setFormat(0, m.end(1), self.fmt_ts)
        self.setFormat(m.start(2), m.end(2) - m.start(2),
                       self.fmt_level.get(level, self.fmt_level["INFO"]))
        self.setFormat(m.start(3), m.end(3) - m.start(3), self.fmt_logger)
        msg_start = m.end(0)
        self.setFormat(msg_start, len(text) - msg_start,
                       self.fmt_msg.get(level, self.fmt_msg_default))


class LogViewerWindow(QMainWindow):
    def __init__(self, log_path: Path) -> None:
        super().__init__()
        self.log_path = log_path
        self.lines: list[str] = []
        self.last_size = 0
        self.tailing = True
        self.level_visible: dict[str, bool] = {lvl: True for lvl in LEVELS}
        self._scroll_guard = False  # suppress _on_scroll while we programmatically scroll

        self.setWindowTitle(f"Drag'n'Doc Log — {log_path.name}")
        self.resize(1100, 640)

        self.viewer = QPlainTextEdit()
        self.viewer.setReadOnly(True)
        self.viewer.setFont(QFont("Consolas", 10))
        self.viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.viewer.setStyleSheet("""
            QPlainTextEdit {
                background-color: #0f172a;
                color: #e2e8f0;
                selection-background-color: #1e293b;
                selection-color: #f1f5f9;
                border: none;
            }
        """)
        self.highlighter = LogHighlighter(self.viewer.document())
        self.setCentralWidget(self.viewer)

        self._build_toolbar()
        self._build_statusbar()
        self._install_shortcuts()

        self.viewer.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self._read_initial_tail()
        self._render()
        self._scroll_to_bottom()
        self._update_status()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REFRESH_INTERVAL_MS)

    # ---- UI construction ------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        tb.setFloatable(False)
        self.addToolBar(tb)

        tb.addWidget(QLabel(" Find: "))
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search…  (F3 = next, Shift+F3 = prev)")
        self.search_box.setMaximumWidth(320)
        self.search_box.textChanged.connect(self._refresh_search)
        tb.addWidget(self.search_box)

        self.regex_cb = QCheckBox("regex")
        self.regex_cb.toggled.connect(self._refresh_search)
        tb.addWidget(self.regex_cb)

        tb.addSeparator()

        tb.addWidget(QLabel(" Show: "))
        self.level_cbs: dict[str, QCheckBox] = {}
        for lvl in LEVELS:
            cb = QCheckBox(lvl)
            cb.setChecked(True)
            cb.setStyleSheet(f"QCheckBox {{ color: {LEVEL_COLORS[lvl]}; }}")
            cb.toggled.connect(lambda checked, name=lvl: self._on_level_toggle(name, checked))
            tb.addWidget(cb)
            self.level_cbs[lvl] = cb

        tb.addSeparator()

        self.wrap_cb = QCheckBox("Wrap")
        self.wrap_cb.toggled.connect(self._on_wrap_toggle)
        tb.addWidget(self.wrap_cb)

        self.ontop_cb = QCheckBox("On top")
        self.ontop_cb.toggled.connect(self._on_ontop_toggle)
        tb.addWidget(self.ontop_cb)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setToolTip("Copy currently visible (filtered) lines to the clipboard")
        self.copy_btn.clicked.connect(self._copy_visible)
        tb.addWidget(self.copy_btn)

        self.save_btn = QPushButton("Save…")
        self.save_btn.setToolTip("Save currently visible (filtered) lines to a file")
        self.save_btn.clicked.connect(self._save_visible)
        tb.addWidget(self.save_btn)

    def _build_statusbar(self) -> None:
        self.status = QStatusBar()
        self.lines_label = QLabel()
        self.tailing_label = QLabel()
        self.matches_label = QLabel()
        self.status.addWidget(self.lines_label)
        self.status.addWidget(QLabel("  ·  "))
        self.status.addWidget(self.tailing_label)
        self.status.addPermanentWidget(self.matches_label)
        self.setStatusBar(self.status)

    def _install_shortcuts(self) -> None:
        for spec, slot in (
            ("Ctrl++", self._zoom_in),
            ("Ctrl+=", self._zoom_in),
            ("Ctrl+-", self._zoom_out),
            ("Ctrl+0", self._zoom_reset),
            ("F3", self._find_next),
            ("Shift+F3", self._find_prev),
            ("Ctrl+F", lambda: self.search_box.setFocus()),
            ("Ctrl+End", self._jump_to_end),
        ):
            act = QAction(self)
            act.setShortcut(QKeySequence(spec))
            act.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            act.triggered.connect(slot)
            self.addAction(act)

    # ---- file reading / tailing ----------------------------------------

    def _read_initial_tail(self) -> None:
        if not self.log_path.exists():
            self.lines = [f"(log file not found: {self.log_path})"]
            self.last_size = 0
            return
        try:
            with self.log_path.open("r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                self.last_size = f.tell()
        except OSError as exc:
            self.lines = [f"(could not read log: {exc})"]
            self.last_size = 0
            return
        self.lines = [ln.rstrip("\n") for ln in all_lines[-TAIL_LINES:]]

    def _read_appended(self) -> list[str]:
        try:
            current_size = self.log_path.stat().st_size
        except OSError:
            return []
        if current_size < self.last_size:
            # file truncated or rotated: re-read tail from scratch
            self._read_initial_tail()
            return []
        if current_size == self.last_size:
            return []
        try:
            with self.log_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self.last_size)
                chunk = f.read()
                self.last_size = f.tell()
        except OSError:
            return []
        if not chunk:
            return []
        new_lines = chunk.splitlines()
        # cap memory: drop oldest lines beyond a generous tail window
        cap = TAIL_LINES * 2
        merged = self.lines + new_lines
        if len(merged) > cap:
            merged = merged[-cap:]
        self.lines = merged
        return new_lines

    def _tick(self) -> None:
        new_lines = self._read_appended()
        if not new_lines:
            return
        self._render()
        if self.tailing:
            self._scroll_to_bottom()
        self._update_status()

    # ---- rendering / filtering -----------------------------------------

    def _visible_lines(self) -> list[str]:
        if all(self.level_visible.values()):
            return self.lines
        out: list[str] = []
        for ln in self.lines:
            level = _parse_level(ln)
            # lines without a recognized level (e.g. tracebacks) are always shown
            if level is None or self.level_visible.get(level, True):
                out.append(ln)
        return out

    def _render(self) -> None:
        text = "\n".join(self._visible_lines())
        sb = self.viewer.verticalScrollBar()
        was_at_bottom = sb.value() >= sb.maximum() - 2
        self._scroll_guard = True
        self.viewer.setPlainText(text)
        self._scroll_guard = False
        self._refresh_search()
        if was_at_bottom and self.tailing:
            self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        sb = self.viewer.verticalScrollBar()
        self._scroll_guard = True
        sb.setValue(sb.maximum())
        self._scroll_guard = False

    def _jump_to_end(self) -> None:
        self.tailing = True
        self._scroll_to_bottom()
        self._update_status()

    # ---- search ---------------------------------------------------------

    def _refresh_search(self) -> None:
        pattern = self.search_box.text()
        doc = self.viewer.document()
        # clear any previous extra selections
        if not pattern:
            self.viewer.setExtraSelections([])
            self._search_count = 0
            self._update_status()
            return

        fmt = QTextCharFormat()
        fmt.setBackground(QColor(FIELD_COLORS["search_match_bg"]))
        fmt.setForeground(QColor(FIELD_COLORS["search_match_fg"]))

        flags = QTextDocument.FindFlag.FindCaseSensitively if self._is_case_sensitive() else QTextDocument.FindFlag(0)

        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        selections = []

        if self.regex_cb.isChecked():
            rx = QRegularExpression(pattern)
            if not self._is_case_sensitive():
                rx.setPatternOptions(QRegularExpression.PatternOption.CaseInsensitiveOption)
            if not rx.isValid():
                self._search_count = -1
                self.viewer.setExtraSelections([])
                self._update_status()
                return
            while True:
                cursor = doc.find(rx, cursor)
                if cursor.isNull():
                    break
                sel = QTextEdit.ExtraSelection()
                sel.cursor = cursor
                sel.format = fmt
                selections.append(sel)
        else:
            while True:
                cursor = doc.find(pattern, cursor, flags)
                if cursor.isNull():
                    break
                sel = QTextEdit.ExtraSelection()
                sel.cursor = cursor
                sel.format = fmt
                selections.append(sel)

        self.viewer.setExtraSelections(selections)
        self._search_count = len(selections)
        self._update_status()

    def _is_case_sensitive(self) -> bool:
        # search is case-insensitive by default; an uppercase letter in the
        # query flips it to case-sensitive (smart-case)
        return any(c.isupper() for c in self.search_box.text())

    def _find_next(self) -> None:
        self._jump_match(forward=True)

    def _find_prev(self) -> None:
        self._jump_match(forward=False)

    def _jump_match(self, *, forward: bool) -> None:
        pattern = self.search_box.text()
        if not pattern:
            return
        flags = QTextDocument.FindFlag(0)
        if not forward:
            flags |= QTextDocument.FindFlag.FindBackward
        if self._is_case_sensitive():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        rx = None
        if self.regex_cb.isChecked():
            rx = QRegularExpression(pattern)
            if not self._is_case_sensitive():
                rx.setPatternOptions(QRegularExpression.PatternOption.CaseInsensitiveOption)
        if rx is not None:
            ok = self.viewer.find(rx, flags)
        else:
            ok = self.viewer.find(pattern, flags)
        if ok:
            return
        # wrap around: reset the cursor to the opposite end and try once more
        cursor = self.viewer.textCursor()
        cursor.movePosition(
            QTextCursor.MoveOperation.Start if forward else QTextCursor.MoveOperation.End
        )
        self.viewer.setTextCursor(cursor)
        if rx is not None:
            self.viewer.find(rx, flags)
        else:
            self.viewer.find(pattern, flags)

    # ---- toolbar handlers ----------------------------------------------

    def _on_level_toggle(self, level: str, checked: bool) -> None:
        self.level_visible[level] = checked
        self._render()
        if self.tailing:
            self._scroll_to_bottom()
        self._update_status()

    def _on_wrap_toggle(self, checked: bool) -> None:
        self.viewer.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if checked else QPlainTextEdit.LineWrapMode.NoWrap
        )

    def _on_ontop_toggle(self, checked: bool) -> None:
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _zoom_in(self) -> None:
        self.viewer.zoomIn(1)

    def _zoom_out(self) -> None:
        self.viewer.zoomOut(1)

    def _zoom_reset(self) -> None:
        f = self.viewer.font()
        f.setPointSize(10)
        self.viewer.setFont(f)

    def _copy_visible(self) -> None:
        text = "\n".join(self._visible_lines())
        QApplication.clipboard().setText(text)
        self.status.showMessage(f"Copied {len(self._visible_lines())} lines to clipboard.", 2000)

    def _save_visible(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save filtered log", "dragndoc-filtered.log", "Log files (*.log);;All files (*)"
        )
        if not path:
            return
        try:
            Path(path).write_text("\n".join(self._visible_lines()), encoding="utf-8")
            self.status.showMessage(f"Saved {len(self._visible_lines())} lines to {path}.", 3000)
        except OSError as exc:
            self.status.showMessage(f"Save failed: {exc}", 5000)

    # ---- scrollbar tracking --------------------------------------------

    def _on_scroll(self, _value: int) -> None:
        if self._scroll_guard:
            return
        sb = self.viewer.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 2
        if at_bottom != self.tailing:
            self.tailing = at_bottom
            self._update_status()

    # ---- status ---------------------------------------------------------

    def _update_status(self) -> None:
        n_total = len(self.lines)
        n_visible = len(self._visible_lines())
        if n_total == n_visible:
            self.lines_label.setText(f"{n_total} lines")
        else:
            self.lines_label.setText(f"{n_visible} / {n_total} lines")
        self.tailing_label.setText("Tailing: ON" if self.tailing else "Tailing: paused (scroll to end to resume)")
        count = getattr(self, "_search_count", 0)
        if count == -1:
            self.matches_label.setText("invalid regex")
        elif self.search_box.text():
            self.matches_label.setText(f"{count} matches")
        else:
            self.matches_label.setText("")


def main(argv: list[str]) -> int:
    log_path = _resolve_log_path(argv)
    app = QApplication(argv)
    app.setApplicationName("Drag'n'Doc Log Viewer")
    win = LogViewerWindow(log_path)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
