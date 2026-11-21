from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QTimer, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

from .database import Library
from .maia import CHECKPOINT_NAME
from .model import APP_NAME, GameFilter, SearchSettings, Source
from .runner import Runner
from .ui.engine_view import EngineDialog
from .ui.library_view import LibraryView
from .ui.review import Review
from .ui.scanner_view import ScannerView
from .ui.theme import Task, button, error, label, style_application


class Window(QMainWindow):
    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.library = Library(root / "library.sqlite3")
        rules = SearchSettings(**self.library.setting("rules", {}))
        checkpoint = Path(self.library.setting("maia_checkpoint", str(root / "models" / CHECKPOINT_NAME)))
        self.runner = Runner(self.library, rules, checkpoint)
        self.dialog = None
        self.shutdown = None
        self.allowed_to_close = False
        self.close_requested = False
        self.setWindowTitle(APP_NAME + " · Lichess Mistake Scanner")
        self.resize(1420, 930)
        self.setMinimumSize(900, 665)

        shell = QWidget()
        self.setCentralWidget(shell)
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toolbar = QWidget()
        toolbar.setObjectName("toolbar")
        top = QHBoxLayout(toolbar)
        top.setContentsMargins(18, 10, 18, 10)
        top.setSpacing(10)
        mark = label("♞")
        mark.setObjectName("mark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(36, 36)
        top.addWidget(mark)
        top.addWidget(label(APP_NAME, "brand"))
        top.addSpacing(21)
        self.nav = []
        for index, title in enumerate(("Analysis", "Library", "Scanner")):
            nav = button(title, lambda _=False, i=index: self.show_page(i), role="nav")
            nav.setCheckable(True)
            self.nav.append(nav)
            top.addWidget(nav)
        top.addStretch()
        self.engine_status = label("", "label")
        top.addWidget(self.engine_status)
        top.addWidget(button("Engines", self.engines))
        layout.addWidget(toolbar)

        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self.review = Review(self.library, self.runner)
        self.browser = LibraryView(self.library)
        self.scanner = ScannerView(self.library, self.runner, root, rules)
        for page in (self.review, self.browser, self.scanner):
            self.pages.addWidget(page)
            page.info.connect(self.show_status)
        self.review.open_library.connect(lambda: self.show_page(1))
        self.review.open_scanner.connect(lambda: self.show_page(2))
        self.browser.opened.connect(self.open_game)
        self.review.reanalyze.connect(self.reanalyze_game)
        self.scanner.request_engines.connect(self.engines)

        menu = self.menuBar().addMenu("View")
        for index, title in enumerate(("Analysis", "Library", "Scanner")):
            action = menu.addAction(title)
            action.setShortcut(QKeySequence(f"Ctrl+{index+1}"))
            action.triggered.connect(lambda _=False, i=index: self.show_page(i))
        menu.addSeparator()
        for dock in (self.review.chart_dock, self.review.engine_dock, self.review.score_dock, self.review.moment_dock):
            menu.addAction(dock.toggleViewAction())
        menu.addAction("Reset analysis layout", lambda: self.review.workspace.restoreState(self.review.reset_state))
        navigation = self.menuBar().addMenu("Navigate")
        for title, shortcut, callback in (
            ("Previous move", "Left", lambda: self.review.advance(-1)),
            ("Next move", "Right", lambda: self.review.advance(1)),
            ("Previous interesting position", "Alt+Left", lambda: self.review.jump(-1)),
            ("Next interesting position", "Alt+Right", lambda: self.review.jump(1)),
            ("Leave variation", "Escape", lambda: self.review.go(self.review.ply)),
        ):
            action = QAction(title, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            navigation.addAction(action)

        self.statusBar().showMessage("Ready")
        geometry = self.library.setting("window_geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromHex(geometry.encode()))
        self.show_page(0)
        previous = self.library.setting("last_game")
        if previous:
            self.review.load(previous)
        self.refresh_engine()
        self.timer = QTimer(self)
        self.timer.setInterval(800)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.last_browser = 0

    def show_status(self, value):
        self.statusBar().showMessage(value, 9500)

    def show_page(self, index):
        self.pages.setCurrentIndex(index)
        for i, control in enumerate(self.nav):
            control.setChecked(i == index)
        for action in self.menuBar().actions():
            if action.text() == "Navigate":
                action.setEnabled(index == 0)
        if index == 1:
            self.browser.refresh(force=True)
        elif index == 2:
            self.scanner.refresh()
        elif index == 0:
            self.review.refresh()

    def open_game(self, scan_game: int, moment: int | None):
        self.review.load(scan_game, moment)
        self.library.set_setting("last_game", scan_game)
        self.show_page(0)

    def reanalyze_game(self, scan_game: int):
        profile = self.library.engine(self.library.setting("active_engine"))
        if profile is None:
            error(self, "Reanalyze game", "Select an engine in Engine Manager first.")
            return
        game = self.library.game(scan_game)
        name = f"Reanalysis · {game['white'] or 'White'} vs {game['black'] or 'Black'}"
        scan = self.library.create_scan(name, [Source("library", str(scan_game))], GameFilter(),
                                        self.scanner.rules, profile, 1)
        self.runner.wakeup.set()
        self.show_page(2)
        self.scanner.active_scan.setCurrentIndex(self.scanner.active_scan.findData(scan))
        self.show_status(f"Queued reanalysis with {profile['name']}. Both results are kept in the library.")

    def refresh_engine(self):
        selected = self.library.engine(self.library.setting("active_engine"))
        self.engine_status.setText(selected["name"] if selected else "No engine selected")
        self.engine_status.setToolTip(selected.get("commit") or selected["path"] if selected else "Open Engine Manager to select Stockfish")
        if self.review.live_button.isChecked():
            self.review._render()

    def engines(self):
        if self.dialog is None:
            self.dialog = EngineDialog(self.library, self.runner, self.root, self)
            self.dialog.changed.connect(self.refresh_engine)
            self.dialog.finished.connect(self._engine_closed)
            self.dialog.open()
        else:
            self.dialog.raise_()
            self.dialog.activateWindow()

    def _engine_closed(self):
        self.dialog.deleteLater()
        self.dialog = None

    def refresh(self):
        if self.close_requested:
            self.close()
            return
        index = self.pages.currentIndex()
        if index == 0:
            self.review.refresh()
        elif index == 1:
            self.last_browser += 1
            if self.last_browser % 5 == 0:
                self.browser.refresh()
        else:
            self.scanner.refresh()

    def closeEvent(self, event):
        if self.allowed_to_close:
            event.accept()
            return
        event.ignore()
        if self.shutdown:
            return
        if any(task.isRunning() for task in self.findChildren(Task)):
            if not self.close_requested:
                self.close_requested = True
                self.show_status("Waiting for the current background operation before closing.")
            return
        self.timer.stop()
        self.review.save()
        self.library.set_setting("window_geometry", bytes(self.saveGeometry().toHex()).decode())
        self.centralWidget().setEnabled(False)
        self.show_status("Closing the scanner and saving completed work…")
        task = self.shutdown = Task(lambda _: self.runner.close(), self)
        task.failed.connect(lambda message: error(self, "Close scanner", message))
        task.finished.connect(self._finish_close)
        task.start()

    def _finish_close(self):
        self.library.close_thread()
        self.allowed_to_close = True
        self.close()


def launch(root: Path) -> int:
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    style_application(app)
    try:
        window = Window(root)
    except Exception as exc:
        error(QWidget(), "Second Look startup", str(exc))
        return 1
    window.show()
    return app.exec()
