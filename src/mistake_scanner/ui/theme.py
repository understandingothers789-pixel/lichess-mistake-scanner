from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHeaderView, QLabel, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QWidget,
)

LOG = logging.getLogger(__name__)
INK = "#dce5ef"
MUTED = "#94a6b9"
ACCENT = "#88cfc0"
MINT = "#8ed5ef"
AMBER = "#e5af76"
MAIA = "#c2a7eb"
BG = "#10161e"
SURFACE = "#171f2a"
BORDER = "#2a394a"

STYLE = """
QWidget { color: #dce5ef; background: #10161e; font-size: 13px; }
QMainWindow::separator { background: #10161e; width: 7px; height: 7px; }
QWidget#toolbar { background: #171f2a; border-bottom: 1px solid #2a394a; }
QWidget#card, QFrame#card { background: #171f2a; border: 1px solid #2a394a; border-radius: 10px; }
QWidget#subtle, QFrame#subtle { background: #1b2633; border: 1px solid #2a394a; border-radius: 8px; }
QLabel { background: transparent; border: 0; }
QLabel[role="brand"] { color: #e7edf5; font-size: 21px; font-weight: 700; }
QLabel[role="title"] { color: #e7edf5; font-size: 24px; font-weight: 650; }
QLabel[role="section"] { color: #e7edf5; font-size: 16px; font-weight: 650; }
QLabel[role="muted"] { color: #94a6b9; }
QLabel[role="label"] { color: #8ed5ef; font-size: 12px; font-weight: 650; }
QLabel[role="number"] { color: #88cfc0; font-size: 24px; font-weight: 650; }
QLabel[role="maia"] { color: #c2a7eb; font-size: 12px; font-weight: 600; }
QLabel[role="eval"] { color: #8ed5ef; background: #1b2633; border: 1px solid #2a394a;
    border-radius: 6px; padding: 4px 8px; font-size: 17px; font-weight: 650; }
QLabel[role="amber"] { color: #e5af76; }
QPushButton, QToolButton {
    background: #222e3c; color: #dce5ef; border: 1px solid #344559;
    border-radius: 6px; padding: 6px 11px; min-height: 18px;
}
QPushButton:hover, QToolButton:hover { background: #2a3a4a; border-color: #668995; }
QPushButton:disabled, QToolButton:disabled { color: #77899d; border-color: #273443; background: #18222e; }
QPushButton:checked { background: #2b4554; border-color: #537586; color: #e7f7f3; }
QPushButton[role="primary"] { background: #88cfc0; color: #112d29; font-weight: 700; border-color: #88cfc0; }
QPushButton[role="primary"]:hover { background: #a0e1d1; }
QPushButton[role="primary"]:disabled { background: #2b3c49; color: #9eafbd; border-color: #2b3c49; }
QPushButton[role="nav"] { background: transparent; border-color: transparent; padding: 7px 15px; color: #a3b4c7; }
QPushButton[role="nav"]:hover { background: #222e3c; color: #dce5ef; }
QPushButton[role="nav"]:checked { color: #88cfc0; background: #1b2633; border-bottom: 2px solid #88cfc0; font-weight: 650; }
QLineEdit, QComboBox, QDateEdit, QSpinBox, QPlainTextEdit {
    background: #1b2633; color: #dce5ef; border: 1px solid #344559;
    border-radius: 6px; padding: 5px 8px; min-height: 20px;
    selection-background-color: #2b4554;
}
QComboBox QAbstractItemView { background: #222e3c; selection-background-color: #2b4554; }
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color: #88cfc0; }
QCheckBox, QRadioButton { background: transparent; spacing: 7px; }
QCheckBox::indicator { width: 13px; height: 13px; background: #1b2633; border: 1px solid #7890a3; border-radius: 3px; }
QCheckBox::indicator:checked { background: #88cfc0; border: 2px solid #527e80; width: 11px; height: 11px; }
QCheckBox::indicator:disabled { background: #202c3a; border-color: #36475a; }
QProgressBar { background: #1b2633; color: #dce5ef; border: 1px solid #344559; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #73b5ab; border-radius: 4px; }
QTableView, QTreeView {
    background: #171f2a; alternate-background-color: #1b2633; border: 1px solid #2a394a;
    border-radius: 5px; selection-background-color: #2b4554;
    selection-color: #e7f7f3; outline: 0; gridline-color: #2a394a;
}
QHeaderView { background: #171f2a; }
QHeaderView::section { background: #1b2633; border: 0; border-bottom: 1px solid #344559; padding: 6px 7px; color: #a8bbce; }
QTableView::item, QTreeView::item { padding: 4px 7px; border: 0; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: #171f2a; border: 1px solid #2a394a; padding: 7px 15px; color: #a3b4c7; }
QTabBar::tab:selected { background: #2b4554; color: #88cfc0; }
QDockWidget { font-weight: 650; }
QDockWidget::title { background: #1b2633; color: #a8bbce; padding: 5px 8px; text-align: left; }
QScrollArea { border: 0; background: #10161e; }
QScrollBar:vertical { background: #1b2633; width: 9px; margin: 0; }
QScrollBar:horizontal { background: #1b2633; height: 9px; margin: 0; }
QScrollBar::handle { background: #50667b; min-height: 23px; min-width: 23px; border-radius: 4px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QToolTip { background: #263446; color: #e7edf5; padding: 5px; border: 1px solid #88cfc0; }
QStatusBar { background: #171f2a; color: #a3b4c7; border-top: 1px solid #2a394a; }
"""


def style_application(app) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI" if __import__("os").name == "nt" else "DejaVu Sans", 10))
    palette = QPalette()
    for item, color in (
        (QPalette.ColorRole.Window, BG), (QPalette.ColorRole.Base, SURFACE),
        (QPalette.ColorRole.Text, INK), (QPalette.ColorRole.WindowText, INK),
        (QPalette.ColorRole.Button, SURFACE), (QPalette.ColorRole.ButtonText, INK),
        (QPalette.ColorRole.Highlight, "#2b4554"), (QPalette.ColorRole.HighlightedText, INK),
    ):
        palette.setColor(item, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)


def label(text: str = "", role: str = "", *, wrap=False) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setProperty("role", role)
    if wrap:
        widget.setWordWrap(True)
    return widget


def button(text: str, callback: Callable | None = None, *, role: str = "", tip="") -> QPushButton:
    widget = QPushButton(text)
    if role:
        widget.setProperty("role", role)
    if callback:
        widget.clicked.connect(callback)
    if tip:
        widget.setToolTip(tip)
        widget.setAccessibleDescription(tip)
        if not any(character.isalnum() for character in text):
            widget.setAccessibleName(tip)
    return widget


def spin(value: int, low: int, high: int, special="") -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(low, high)
    widget.setValue(value)
    if special:
        widget.setSpecialValueText(special)
    return widget


def combo(choices) -> QComboBox:
    widget = QComboBox()
    for item in choices:
        if isinstance(item, tuple):
            widget.addItem(*item)
        else:
            widget.addItem(item)
    return widget


def table(headers, *, single=True) -> QTableWidget:
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().hide()
    widget.verticalHeader().setDefaultSectionSize(27)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection if single
                            else QAbstractItemView.SelectionMode.ExtendedSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(True)
    widget.setShowGrid(False)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    widget.horizontalHeader().setStretchLastSection(True)
    return widget


def cell(value, color: str | None = None, tooltip: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem("—" if value is None else str(value))
    if color:
        item.setForeground(QColor(color))
    if tooltip:
        item.setToolTip(tooltip)
    return item


def score_color(score) -> str:
    if score.winner < 0 or (score.cp is not None and score.cp < -30):
        return AMBER
    if score.winner > 0 or (score.cp is not None and score.cp > 30):
        return ACCENT
    return MUTED


def time_control_label(value: str | None) -> str:
    if not value:
        return ""
    base, _, increment = value.partition("+")
    if base.isascii() and base.isdigit() and increment.isascii() and increment.isdigit():
        minutes, seconds = divmod(int(base), 60)
        start = f"{minutes} min" if minutes else f"{seconds} sec"
        if minutes and seconds:
            start += f" {seconds} sec"
        return f"{start} + {int(increment)} sec"
    return "Untimed" if value == "-" else value


def error(parent: QWidget, title: str, message: str) -> None:
    box = QMessageBox(QMessageBox.Icon.Warning, title, str(message), QMessageBox.StandardButton.Ok, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.exec()


class Task(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    message = Signal(str)
    transfer = Signal(object, object)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task

    def run(self):
        try:
            self.succeeded.emit(self.task(self))
        except Exception as exc:
            LOG.exception("Background task failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")
