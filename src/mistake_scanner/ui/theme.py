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
INK = "#edf1e9"
MUTED = "#9aa9a3"
ACCENT = "#d8eaab"
MINT = "#a7c7ac"
AMBER = "#e5af76"
BG = "#141b19"
SURFACE = "#1d2723"
BORDER = "#34423b"

STYLE = """
QWidget { color: #edf1e9; background: #141b19; font-size: 13px; }
QMainWindow::separator { background: #141b19; width: 7px; height: 7px; }
QWidget#toolbar { background: #1b2420; border-bottom: 1px solid #34423b; }
QWidget#card, QFrame#card { background: #1d2723; border: 1px solid #34423b; border-radius: 10px; }
QWidget#subtle, QFrame#subtle { background: #202d27; border: 1px solid #34423b; border-radius: 8px; }
QLabel { background: transparent; border: 0; }
QLabel[role="brand"] { color: #f4f6ec; font-size: 21px; font-weight: 700; }
QLabel[role="title"] { color: #f4f6ec; font-size: 27px; font-weight: 700; }
QLabel[role="section"] { color: #f4f6ec; font-size: 16px; font-weight: 650; }
QLabel[role="muted"] { color: #9aa9a3; }
QLabel[role="label"] { color: #a7c7ac; font-size: 12px; font-weight: 650; }
QLabel[role="number"] { color: #d8eaab; font-size: 24px; font-weight: 650; }
QLabel[role="amber"] { color: #e5af76; }
QLabel#mark { color: #202d22; background: #d8eaab; border-radius: 7px; font: 25px 'Segoe UI Symbol', 'DejaVu Sans'; }
QPushButton, QToolButton {
    background: #26332d; color: #edf1e9; border: 1px solid #3b4a42;
    border-radius: 6px; padding: 6px 11px; min-height: 18px;
}
QPushButton:hover, QToolButton:hover { background: #33463a; border-color: #768f79; }
QPushButton:disabled, QToolButton:disabled { color: #6f8077; border-color: #303b35; background: #202923; }
QPushButton:checked { background: #3b5142; border-color: #799880; color: #f0f4e9; }
QPushButton[role="primary"] { background: #d8eaab; color: #242f23; font-weight: 700; border-color: #d8eaab; }
QPushButton[role="primary"]:hover { background: #e6f3c1; }
QPushButton[role="primary"]:disabled { background: #3b4939; color: #9aa997; border-color: #3b4939; }
QPushButton[role="nav"] { background: transparent; border-color: transparent; padding: 7px 15px; color: #aab9ae; }
QPushButton[role="nav"]:hover { background: #27342d; color: #edf1e9; }
QPushButton[role="nav"]:checked { color: #d8eaab; background: #314237; border-color: #405647; font-weight: 650; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {
    background: #19231e; color: #edf1e9; border: 1px solid #3b4a42;
    border-radius: 6px; padding: 5px 8px; min-height: 20px;
    selection-background-color: #507060;
}
QComboBox QAbstractItemView { background: #233229; selection-background-color: #3b5142; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color: #9bb293; }
QCheckBox, QRadioButton { background: transparent; spacing: 7px; }
QCheckBox::indicator { width: 13px; height: 13px; background: #19231e; border: 1px solid #6b8776; border-radius: 3px; }
QCheckBox::indicator:checked { background: #d8eaab; border: 2px solid #6e8b65; width: 11px; height: 11px; }
QCheckBox::indicator:disabled { background: #223029; border-color: #394941; }
QProgressBar { background: #18221e; color: #edf1e9; border: 1px solid #3b4a42; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #5f876b; border-radius: 4px; }
QTableView, QTreeView {
    background: #1d2723; alternate-background-color: #233029; border: 1px solid #34423b;
    border-radius: 6px; selection-background-color: #385345;
    selection-color: #f0f4e9; outline: 0; gridline-color: #34423b;
}
QHeaderView { background: #222e28; }
QHeaderView::section { background: #28372f; border: 0; border-bottom: 1px solid #3b4a42; padding: 6px 7px; color: #baccc0; }
QTableView::item, QTreeView::item { padding: 4px 7px; border: 0; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: #222e28; border: 1px solid #34423b; padding: 7px 15px; color: #aab9ae; }
QTabBar::tab:selected { background: #314237; color: #d8eaab; }
QDockWidget { font-weight: 650; }
QDockWidget::title { background: #26332d; padding: 5px 8px; text-align: left; }
QScrollArea { border: 0; background: #141b19; }
QScrollBar:vertical { background: #18221e; width: 9px; margin: 0; }
QScrollBar:horizontal { background: #18221e; height: 9px; margin: 0; }
QScrollBar::handle { background: #4a6354; min-height: 23px; min-width: 23px; border-radius: 4px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QToolTip { background: #dce8cf; color: #243127; padding: 5px; border: 1px solid #9bb293; }
QStatusBar { background: #1b2420; color: #aab9ae; border-top: 1px solid #34423b; }
"""


def style_application(app) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI" if __import__("os").name == "nt" else "DejaVu Sans", 10))
    palette = QPalette()
    for item, color in (
        (QPalette.ColorRole.Window, BG), (QPalette.ColorRole.Base, SURFACE),
        (QPalette.ColorRole.Text, INK), (QPalette.ColorRole.WindowText, INK),
        (QPalette.ColorRole.Button, SURFACE), (QPalette.ColorRole.ButtonText, INK),
        (QPalette.ColorRole.Highlight, "#385345"), (QPalette.ColorRole.HighlightedText, INK),
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
    widget.verticalHeader().setDefaultSectionSize(30)
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
