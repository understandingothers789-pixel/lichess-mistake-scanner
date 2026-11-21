from __future__ import annotations

import os
from dataclasses import asdict
from time import monotonic, sleep

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import chess
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mistake_scanner.database import Library
from mistake_scanner.gui import Window
from mistake_scanner.model import SearchSettings
from mistake_scanner.ui.theme import style_application
from test_pipeline import ScriptedStockfish


def pump(app, ready, timeout=12):
    until = monotonic() + timeout
    while monotonic() < until:
        app.processEvents()
        if ready():
            return
        sleep(0.02)
    raise AssertionError("The UI did not reach its expected state.")


def test_full_information_navigation_and_scanner_stay_responsive(tmp_path, pgn, fake_profile):
    app = QApplication.instance() or QApplication([])
    style_application(app)
    database = Library(tmp_path / "library.sqlite3")
    database.set_setting("rules", asdict(SearchSettings(workers=1, first_move=1)))
    database.save_engine(fake_profile)
    database.set_setting("active_engine", fake_profile["id"])
    database.close_thread()
    window = Window(tmp_path)
    window.runner.pool.factory = ScriptedStockfish
    window.show()
    try:
        window.show_page(2)
        window.scanner.source.setCurrentIndex(2)
        window.scanner.files = [str(pgn)]
        window.scanner.game_limit.setValue(1)
        QTest.mouseClick(window.scanner.start_button, Qt.MouseButton.LeftButton)
        window.show_page(1)
        assert window.pages.currentIndex() == 1
        pump(app, lambda: bool(window.library.discovery_page()))
        found = window.library.discovery_page()[0]
        window.browser.refresh(force=True)
        pump(app, lambda: window.browser.open.isEnabled())
        QTest.mouseClick(window.browser.open, Qt.MouseButton.LeftButton)
        app.processEvents()
        review = window.review
        assert window.pages.currentIndex() == 0
        assert review.current["id"] == found["id"] and review.ply == 5
        assert review.line_table.rowCount() == 5
        assert review.line_table.item(0, 3).text() == "d20"
        assert "Played Nf6" in review.summary.text()
        assert review.moves.topLevelItemCount() == 8

        QTest.mouseClick(review.after, Qt.MouseButton.LeftButton)
        assert review.ply == 6 and review.board.board.peek() == chess.Move.from_uci("g8f6")
        assert review.line_table.item(0, 0).text().startswith("Qxf7")
        point = review.line_table.visualItemRect(review.line_table.item(0, 0)).center()
        QTest.mouseClick(review.line_table.viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert review.board.board.is_checkmate()
        QTest.mouseClick(review.return_line, Qt.MouseButton.LeftButton)
        assert not review.board.board.is_checkmate()
        QTest.mouseClick(review.bookmark_moment, Qt.MouseButton.LeftButton)
        assert window.library.mistake(found["id"])["bookmarked"]
        assert window.library.game(found["scan_game"])["cursor_moment"] == found["id"]

        graph = review.graph
        origin = graph._chart().topLeft()
        QTest.mouseClick(graph, Qt.MouseButton.LeftButton, pos=QPoint(round(origin.x()), round(origin.y() + 8)))
        assert review.ply == 0
        board = review.board
        bounds, size = board._geometry()
        e2 = board._square(chess.E2, bounds, size).center().toPoint()
        e4 = board._square(chess.E4, bounds, size).center().toPoint()
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=e2)
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=e4)
        assert review.ply == 1

        window.engines()
        assert window.dialog.table.rowCount() == 1
        assert fake_profile["commit"] in window.dialog.details.toPlainText()
        window.dialog.accept()
        app.processEvents()
        assert window.dialog is None
        QTest.mouseClick(review.reanalysis_button, Qt.MouseButton.LeftButton)
        assert window.pages.currentIndex() == 2
        scan = window.scanner.active_scan.currentData()
        pump(app, lambda: window.library.scan(scan)["verified"] == 1)
        original = window.library.game(found["scan_game"])
        assert {row["game_id"] for row in window.library.game_page()} == {original["id"]}
        assert len(window.library.game_page()) == 2
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())
