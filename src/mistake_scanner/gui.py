from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, QTimer, Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

from .database import Library
from .maia import CHECKPOINT_NAME
from .model import APP_NAME, GameFilter, SearchSettings, Source
from .runner import Runner
from .ui.engine_view import EngineDialog
from .ui.library_view import LibraryView
from .ui.review import Review
from .ui.scanner_view import ScannerView
from .ui.setup_view import SetupDialog
from .ui.theme import Task, button, error, label, style_application

APP_ICON = Path(__file__).with_name("assets") / "second-look.png"


class Window(QMainWindow):
    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.library = Library(root / "library.sqlite3")
        rules = SearchSettings(**self.library.setting("rules", {}))
        checkpoint = Path(self.library.setting("maia_checkpoint", str(root / "models" / CHECKPOINT_NAME)))
        self.runner = Runner(self.library, rules, checkpoint)
        self.dialog = None
        self.setup_dialog = None
        self.review_ids = ()
        self.review_kind = ""
        self.review_index = 0
        self.shutdown = None
        self.allowed_to_close = False
        self.close_requested = False
        self.setWindowTitle(APP_NAME + " · Lichess Mistake Scanner")
        self.setWindowIcon(QIcon(str(APP_ICON)))
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
        top.setContentsMargins(16, 6, 16, 6)
        top.setSpacing(10)
        mark = label()
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(32, 32)
        mark.setPixmap(self.windowIcon().pixmap(QSize(32, 32), mark.devicePixelRatioF()))
        top.addWidget(mark)
        top.addWidget(label(APP_NAME, "brand"))
        top.addSpacing(21)
        self.nav = []
        pages = (
            ("Analysis", "Play through a game and explore Stockfish lines (Ctrl+1)."),
            ("Library", "Find analyzed games and interesting moves (Ctrl+2)."),
            ("Scanner", "Import games and track analysis (Ctrl+3)."),
        )
        for index, (title, tip) in enumerate(pages):
            nav = button(title, lambda _=False, i=index: self.show_page(i), role="nav", tip=tip)
            nav.setCheckable(True)
            self.nav.append(nav)
            top.addWidget(nav)
        top.addStretch()
        self.engine_status = label("", "label")
        top.addWidget(self.engine_status)
        top.addWidget(button("Engines", self.engines, tip="Install, select, test, or remove a Stockfish build."))
        top.addWidget(button("Setup && storage", self.setup, tip="Set up Stockfish and Maia and see how much disk space they use."))
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
        self.review.open_scanner.connect(self._start_from_analysis)
        self.browser.opened.connect(self.open_game)
        self.browser.review_requested.connect(self._begin_review)
        self.review.review_next.connect(self._mark_reviewed_next)
        self.review.review_stop.connect(self._stop_review)
        self.review.position_opened.connect(self._open_position)
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
        menu.addAction("Reset analysis layout", self.review.reset_layout)
        navigation = self.menuBar().addMenu("Navigate")
        for title, shortcut, callback in (
            ("Previous move", "Left", lambda: self.review.advance(-1)),
            ("Next move", "Right", lambda: self.review.advance(1)),
            ("Previous interesting position", "Alt+Left", lambda: self.review.jump(-1)),
            ("Next interesting position", "Alt+Right", lambda: self.review.jump(1)),
            ("Leave variation", "Escape", self.review.leave_variation),
        ):
            action = QAction(title, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            navigation.addAction(action)
        help_menu = self.menuBar().addMenu("Help")
        quick_start = help_menu.addAction("Quick start", self._quick_start)
        quick_start.setShortcut(QKeySequence("F1"))

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

    def _quick_start(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Second Look quick start")
        dialog.resize(580, 400)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("Quick start", "title"))
        for title, description in (
            ("Set up Stockfish", "Open Setup & storage, then Engine Manager. Download a build or select an existing executable. "
             "Stockfish looks for mistakes. Maia is optional and estimates how often a player at a given rating might make a move."),
            ("Scan a few games", "In Scanner, choose Lichess API for a game URL or Local PGN files for a saved game. "
             "Start with a small game limit. Use monthly archives to sample a date or a UTC time window."),
            ("Open a finding", "Go to Library → Discoveries and double-click a move. Scroll over the board or use the arrow keys to go through the game. "
             "Hover over a Stockfish line to preview it, or click it to explore it on the main board."),
            ("Read the scores", "Positive evaluations favor White and negative ones favor Black. "
             "Use Compare move to check any move with a fresh Stockfish search. Maia percentages are predictions."),
        ):
            layout.addWidget(label(title, "section"))
            layout.addWidget(label(description, "muted", wrap=True))
        layout.addStretch()
        layout.addWidget(button("Close", dialog.accept, tip="Return to Second Look."))
        dialog.exec()

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
        self._stop_review()
        self._show_game(scan_game, moment)

    def _open_position(self, scan_game: int, ply: int):
        self._stop_review()
        self._show_game(scan_game, ply=ply)

    def _show_game(self, scan_game: int, moment=None, *, ply=None):
        self.review.load(scan_game, moment, ply=ply)
        self.library.set_setting("last_game", scan_game)
        self.show_page(0)

    def _begin_review(self, kind, identities):
        if not identities:
            self.show_status("No results match these review filters.")
            return
        self.review_ids, self.review_kind, self.review_index = identities, kind, 0
        self._show_review_item()

    def _show_review_item(self):
        game = self.review_kind == "games"
        while self.review_index < len(self.review_ids):
            identity = self.review_ids[self.review_index]
            item = self.library.game(identity) if game else self.library.mistake(identity)
            if item:
                self._show_game(item["scan_game"], None if game else identity)
                self.review.set_review_progress(self.review_index + 1, len(self.review_ids), is_game=game)
                return
            self.review_index += 1
        self._stop_review()
        self.show_status("Filtered review complete.")

    def _mark_reviewed_next(self):
        if self.review_ids:
            identity = self.review_ids[self.review_index]
            self.library.review(identity, game=self.review_kind == "games", reviewed=True)
            self.review_index += 1
            self._show_review_item()
            self.review.refresh()

    def _stop_review(self):
        self.review_ids = ()
        self.review.set_review_progress()

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
        self.review.scan_button.setText("Start a scan" if selected else "Set up Second Look")
        tip = "Choose a game source and start analyzing games." if selected else "Open setup to install or select Stockfish."
        self.review.scan_button.setToolTip(tip)
        self.review.scan_button.setAccessibleDescription(tip)
        if self.review.live_button.isChecked():
            self.review._render()

    def _start_from_analysis(self):
        if self.library.engine(self.library.setting("active_engine")):
            self.show_page(2)
        else:
            self.setup()

    def setup(self):
        if self.setup_dialog is None:
            location = self.scanner.cache_location.text().strip()
            self.setup_dialog = SetupDialog(self.library, self.runner, self.root,
                                            Path(location) if location else self.root / "cache", self)
            self.setup_dialog.manage_engines.connect(self.engines)
            self.setup_dialog.validate_engine.connect(lambda: self.engines().validate.click())
            self.setup_dialog.setup_maia.connect(self._setup_maia)
            self.setup_dialog.finished.connect(self._setup_closed)
            self.setup_dialog.open()
        else:
            self.setup_dialog.raise_()
            self.setup_dialog.activateWindow()

    def _setup_closed(self):
        self.setup_dialog.deleteLater()
        self.setup_dialog = None

    def _setup_maia(self, download):
        self.show_page(2)
        (self.scanner.download_model if download else self.scanner.choose_model).click()

    def engines(self):
        if self.dialog is None:
            self.dialog = EngineDialog(self.library, self.runner, self.root, self)
            self.dialog.changed.connect(self.refresh_engine)
            self.dialog.finished.connect(self._engine_closed)
            self.dialog.open()
        else:
            self.dialog.raise_()
            self.dialog.activateWindow()
        return self.dialog

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
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SecondLook.LichessMistakeScanner")
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(APP_ICON)))
    style_application(app)
    try:
        window = Window(root)
    except Exception as exc:
        error(QWidget(), "Second Look startup", str(exc))
        return 1
    window.show()
    return app.exec()
