from __future__ import annotations

import os
from dataclasses import asdict
from time import monotonic, sleep

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import chess
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mistake_scanner.database import Library
from mistake_scanner.gui import Window
from mistake_scanner.model import SearchSettings
from mistake_scanner.ui.theme import style_application
from conftest import SAMPLE
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
        assert str(tmp_path / "models" / "maia3-79m.pt") in window.scanner.model_location.text()
        scanner = window.scanner
        assert scanner.first_month.isVisible() and not scanner.archive_day.isVisible()
        scanner.single_day.setChecked(True)
        assert scanner.archive_day.isVisible() and not scanner.first_month.isVisible() and not scanner.last_month.isVisible()
        assert not scanner.since.isVisible() and not scanner.until.isVisible()
        scanner.source.setCurrentIndex(1)
        assert scanner.since.isVisible() and scanner.until.isVisible()
        scanner.source.setCurrentIndex(0)
        assert scanner.archive_day.isVisible() and not scanner.since.isVisible()
        scanner.single_day.setChecked(False)
        assert scanner.first_month.isVisible() and scanner.since.isVisible() and not scanner.archive_day.isVisible()
        scanner.day_batch.setChecked(True)
        assert scanner.batch_dates.isVisible() and scanner.daily_limit.isVisible() and not scanner.game_limit.isVisible()
        assert not scanner.first_month.isVisible() and not scanner.since.isVisible()
        scanner._add_batch_day()
        assert scanner.batch_dates.text() == scanner.archive_day.date().toString("yyyy-MM-dd")
        scanner.single_day.setChecked(True)
        assert not scanner.day_batch.isChecked() and not scanner.batch_dates.isVisible() and scanner.game_limit.isVisible()
        scanner.single_day.setChecked(False)
        window.scanner.source.setCurrentIndex(2)
        window.scanner.files = [str(pgn)]
        window.scanner.game_limit.setValue(1)
        QTest.mouseClick(window.scanner.start_button, Qt.MouseButton.LeftButton)
        window.show_page(1)
        assert window.pages.currentIndex() == 1
        pump(app, lambda: bool(window.library.discovery_page()))
        scanner.refresh()
        assert scanner.stockfish_status.text().startswith("Stockfish · Complete")
        assert scanner.maia_progress.text().startswith("Maia · Waiting for checkpoint setup")
        assert scanner.transfer_label.text().startswith("Source intake")
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
        assert "Verified at d20" in review.scan_decision.text()
        assert review.moves.topLevelItemCount() == 5

        move_row, black_column = review.score_items[6]
        assert review.score_items[5] == (move_row, 1)
        review._score_clicked(move_row, black_column)
        assert review.ply == 6
        review._score_clicked(move_row, 1)
        assert review.ply == 5 and review.move_ply == 5 and review.current is None
        assert review.position_label.text() == "3. Qh5"
        assert "Saved scan" in review.scan_scores.text()
        assert "Discovery screen at d12" in review.scan_decision.text()
        QTest.mouseClick(review.before, Qt.MouseButton.LeftButton)
        assert review.ply == 4 and review.move_ply == 5
        QTest.mouseClick(review.compare_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(review.after, Qt.MouseButton.LeftButton)
        assert review.ply == 5 and review.move_ply == 5 and review.current is None
        pump(app, lambda: review.comparison is not None)
        assert "Played Qh5 +0.00 (d20)" in review.comparison_status.text()
        assert review.comparison_status.text().startswith("Live Stockfish")
        assert review.lines_board.peek() == chess.Move.from_uci("d1h5")
        assert "Live Stockfish" in review.line_table.item(0, 1).toolTip()
        assert len(window.library.discovery_page()) == 1
        review._moment_clicked(0, 0)
        assert review.comparison is None and review.ply == 5
        QTest.mouseClick(review.after, Qt.MouseButton.LeftButton)
        assert review.ply == 6 and review.board.board.peek() == chess.Move.from_uci("g8f6")
        assert review.line_table.item(0, 0).text().startswith("Qxf7")
        point = review.line_table.visualItemRect(review.line_table.item(0, 0)).center()
        viewport = review.line_table.viewport()
        QTest.mouseMove(viewport, point)
        pump(app, lambda: review.variation_preview.isVisible())
        preview = review.variation_preview
        assert preview.board.board.is_checkmate()
        assert not review.board.board.is_checkmate()
        for delta, step in ((120, 0), (-120, 1)):
            wheel = QWheelEvent(QPointF(point), QPointF(viewport.mapToGlobal(point)), QPoint(), QPoint(0, delta),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(viewport, wheel)
            assert preview.step == step
        QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=point)
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
        assert board.clocks == {chess.WHITE: 600, chess.BLACK: 600}
        assert len(board.pieces) == 12 and all(piece.isValid() for piece in board.pieces.values())
        bounds, size = board._geometry()
        e2 = board._square(chess.E2, bounds, size).center().toPoint()
        e4 = board._square(chess.E4, bounds, size).center().toPoint()
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=e2)
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=e4)
        assert review.ply == 1
        assert board.clocks == {chess.WHITE: 603, chess.BLACK: 600}
        assert review.open_source.isVisible()
        QTest.mouseClick(review.live_button, Qt.MouseButton.LeftButton)
        pump(app, lambda: review.position_label.text().startswith("Live Stockfish · depth"))
        assert review.lines_board.peek() == chess.Move.from_uci("e2e4")
        assert review.line_table.rowCount() == 5
        review.advance(1)
        pump(app, lambda: review.position_label.text().startswith("Live Stockfish · depth"))
        assert review.lines_board.peek() == chess.Move.from_uci("e7e5")
        QTest.mouseClick(review.live_button, Qt.MouseButton.LeftButton)
        assert review.live_future is None

        for delta, expected_ply in ((-120, 3), (120, 2)):
            point = board.rect().center()
            wheel = QWheelEvent(QPointF(point), QPointF(board.mapToGlobal(point)), QPoint(), QPoint(0, delta),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(board, wheel)
            assert review.ply == expected_ply

        window.resize(1000, 720)
        pump(app, lambda: review.compact)
        assert review.board._geometry()[0].width() >= 340
        assert set(review.workspace.tabifiedDockWidgets(review.engine_dock)) == {
            review.score_dock, review.moment_dock, review.chart_dock,
        }
        window.resize(1420, 930)
        pump(app, lambda: not review.compact)
        assert not review.workspace.tabifiedDockWidgets(review.engine_dock)

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
        browser = window.browser
        window.show_page(1)
        browser.tabs.setCurrentIndex(1)
        browser.speed_filter.setCurrentIndex(browser.speed_filter.findData("classical"))
        pump(app, lambda: browser.fetch is None)
        assert not browser.rows
        browser.speed_filter.setCurrentIndex(browser.speed_filter.findData("rapid"))
        pump(app, lambda: browser.fetch is None and len(browser.rows) == 2)
        browser.time_filter.setEditText("900+10")
        browser._changed()
        pump(app, lambda: browser.fetch is None)
        assert not browser.rows
        browser.tabs.setCurrentIndex(0)
        exact = browser.time_filter.findData("600+5")
        assert browser.time_filter.itemText(exact) == "10 min + 5 sec"
        browser.time_filter.setCurrentIndex(exact)
        pump(app, lambda: browser.fetch is None and bool(browser.rows))
        assert browser.time_filter.currentText() == "10 min + 5 sec"
        assert {row["speed"] for row in browser.rows} == {"rapid"}

    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_first_run_storage_and_filtered_review_across_games(tmp_path, pgn, fake_profile):
    app = QApplication.instance() or QApplication([])
    style_application(app)
    window = Window(tmp_path)
    window.runner.pool.factory = ScriptedStockfish
    window.show()
    try:
        assert not window.windowIcon().isNull()
        assert window.review.welcome.isVisible() and not window.review.workspace.isVisible()
        assert "Stockfish" in window.review.scan_button.toolTip()
        assert "UTC" in window.scanner.utc_end.toolTip()
        assert window.browser.review_filtered.toolTip()
        assert window.review.scan_button.text() == "Set up Second Look"
        window.review.scan_button.click()
        pump(app, lambda: window.setup_dialog.task is None)
        setup = window.setup_dialog
        assert "Select or install" in setup.statuses["Stockfish"].text()
        assert setup.sizes["Analysis library"].text() != "Measuring…"
        assert setup.sizes["Archive cache"].text() == "0 B"
        assert not setup.validate.isEnabled()
        setup.accept()
        app.processEvents()

        window.library.save_engine(fake_profile)
        window.library.set_setting("active_engine", fake_profile["id"])
        window.refresh_engine()
        assert window.review.scan_button.text() == "Start a scan"
        assert "Choose a game source" in window.review.scan_button.accessibleDescription()
        pgn.write_text(SAMPLE + SAMPLE.replace("AbcD1234", "seqG1234").replace("ExampleWhite", "OtherWhite"),
                       encoding="utf-8")
        window.show_page(2)
        window.scanner.source.setCurrentIndex(2)
        window.scanner.files = [str(pgn)]
        window.scanner.start_button.click()
        pump(app, lambda: len(window.library.discovery_page()) == 2)

        window.show_page(1)
        browser = window.browser
        browser.grouped.setChecked(True)
        pump(app, lambda: browser.fetch is None and browser.group_table.rowCount() == 1)
        assert browser.group_table.item(0, 2).text() == "2 of 2"
        browser.open.click()
        pump(app, lambda: browser.fetch is None and browser.member_table.rowCount() == 2)
        assert {browser.member_table.item(i, 0).text() for i in range(2)} == {"ExampleWhite", "OtherWhite"}
        assert {browser.member_table.item(i, 4).text() for i in range(2)} == {"—"}
        browser.grouped.setChecked(False)
        browser.review.setCurrentIndex(browser.review.findData("unreviewed"))
        pump(app, lambda: browser.fetch is None and browser.review_filtered.isEnabled())
        expected = list(window.library.review_ids("moments", {"review": "unreviewed"}))
        browser.review_filtered.click()
        pump(app, lambda: window.review.review_bar.isVisible())
        review = window.review
        assert review.current["id"] == expected[0] and review.review_progress.text() == "Finding 1 of 2"
        first_game = review.game["scan_game"]
        review.mark_next.click()
        assert review.current["id"] == expected[1] and review.game["scan_game"] != first_game
        assert window.library.mistake(expected[0])["reviewed"]
        review.mark_next.click()
        assert not review.review_bar.isVisible() and window.library.mistake(expected[1])["reviewed"]
        assert not window.library.review_ids("moments", {"review": "unreviewed"})
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())
