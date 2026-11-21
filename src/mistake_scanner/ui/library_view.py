from __future__ import annotations

import chess
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QHeaderView, QLineEdit, QTabWidget, QVBoxLayout, QWidget,
)

from ..model import SPEEDS, board_at
from .review import NAMES, _clock, _prob, export_ui
from .theme import ACCENT, AMBER, MUTED, Task, button, cell, combo, label, table

PAGE = 80
ORDERS = (
    ("Maia probability", "probability"), ("Evaluation loss", "loss"), ("Lower player rating", "rating"),
    ("Time control", "speed"), ("Opening", "opening"), ("Observed move frequency", "frequency"),
    ("Date discovered", "recent"), ("Review status", "review"),
)
GAME_ORDERS = (
    ("Recently added", "recent"), ("Game date", "date"), ("White", "white"), ("Black", "black"),
    ("Rating", "rating"), ("Time control", "speed"), ("Opening", "opening"),
)


class LibraryView(QWidget):
    opened = Signal(int, object)
    info = Signal(str)

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self.library = library
        self.page = 0
        self.rows = []
        self.task = None
        self.fetch = None
        self.wanted = None
        self.last_signature = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 12)
        outer.setSpacing(11)
        heading = QHBoxLayout()
        headings = QVBoxLayout()
        headings.setSpacing(3)
        headings.addWidget(label("Analysis library", "title"))
        headings.addWidget(label("Explore discoveries or open the full games they came from.", "muted"))
        heading.addLayout(headings, 1)
        heading.addWidget(button("Export selected", lambda: self._export(False)))
        heading.addWidget(button("Export filtered collection", lambda: self._export(True)))
        outer.addLayout(heading)
        time_filters = QHBoxLayout()
        self.speed_filter = combo([("Any time category", ""), *((speed.capitalize(), speed) for speed in SPEEDS)])
        self.speed_filter.setAccessibleName("Time category filter")
        self.time_filter = QLineEdit()
        self.time_filter.setPlaceholderText("Exact time control, e.g. 900+10")
        self.time_filter.setAccessibleName("Exact time control filter")
        self.time_filter.setMaximumWidth(250)
        time_filters.addWidget(label("Time control", "label"))
        time_filters.addWidget(self.speed_filter)
        time_filters.addWidget(self.time_filter)
        time_filters.addStretch()
        outer.addLayout(time_filters)
        self.tabs = QTabWidget()
        self.discoveries = QWidget()
        self.games = QWidget()
        self.tabs.addTab(self.discoveries, "Discoveries")
        self.tabs.addTab(self.games, "Games")
        self.tabs.currentChanged.connect(self._changed)
        outer.addWidget(self.tabs, 1)

        discovery_layout = QVBoxLayout(self.discoveries)
        discovery_layout.setContentsMargins(0, 8, 0, 0)
        discovery_layout.setSpacing(7)
        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search players or opening")
        self.sort = combo(ORDERS)
        self.review = combo([("All review statuses", "all"), ("Unreviewed", "unreviewed"),
                             ("Reviewed", "reviewed"), ("Bookmarked", "bookmarked")])
        self.clock = combo([("Any clock", (None, None)), ("Under 10 seconds", (10, None)),
                            ("Under 30 seconds", (30, None)), ("Over two minutes", (None, 120))])
        self.eco = QLineEdit()
        self.eco.setMaxLength(3)
        self.eco.setMaximumWidth(56)
        self.eco.setPlaceholderText("ECO")
        self.pending = QCheckBox("Include candidates")
        for widget in (self.search, self.sort, self.review, self.clock, self.eco, self.pending):
            tools.addWidget(widget, 1 if widget is self.search else 0)
        discovery_layout.addLayout(tools)
        self.discovery_table = table(
            ["Maia", "Loss", "Move", "Players", "Rating", "Speed", "Opening", "Clock", "Observed", "Status"],
            single=False,
        )
        self.discovery_table.setAccessibleName("Discovered chess mistakes")
        self.discovery_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.discovery_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        for col, width in enumerate((71, 80, 86, 190, 100, 81, 190, 73, 83, 155)):
            self.discovery_table.setColumnWidth(col, width)
        self.discovery_table.cellDoubleClicked.connect(self._open)
        discovery_layout.addWidget(self.discovery_table, 1)

        game_layout = QVBoxLayout(self.games)
        game_layout.setContentsMargins(0, 8, 0, 0)
        game_layout.setSpacing(7)
        tools = QHBoxLayout()
        self.game_search = QLineEdit()
        self.game_search.setPlaceholderText("Search players or opening")
        self.game_sort = combo(GAME_ORDERS)
        self.bookmarks = QCheckBox("Bookmarks only")
        tools.addWidget(self.game_search, 1)
        tools.addWidget(self.game_sort)
        tools.addWidget(self.bookmarks)
        game_layout.addLayout(tools)
        self.game_table = table(["White", "Black", "Result", "Date", "Time", "Opening", "Mistakes", "Missed replies", "Status"],
                                single=False)
        self.game_table.setAccessibleName("Analyzed games")
        for column in (0, 1, 5):
            self.game_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.game_table.cellDoubleClicked.connect(self._open)
        game_layout.addWidget(self.game_table, 1)

        foot = QHBoxLayout()
        self.meta = label("", "muted")
        self.prev = button("‹ Previous", lambda: self._turn(-1))
        self.next = button("Next ›", lambda: self._turn(1))
        self.open = button("Open analysis", self._open_selected, role="primary")
        foot.addWidget(self.meta, 1)
        foot.addWidget(self.prev)
        foot.addWidget(self.next)
        foot.addWidget(self.open)
        outer.addLayout(foot)

        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(160)
        self.debounce.timeout.connect(self._changed)
        for field in (self.search, self.game_search, self.eco, self.time_filter):
            field.textChanged.connect(lambda _: self.debounce.start())
        for field in (self.sort, self.review, self.clock, self.game_sort, self.speed_filter):
            field.currentIndexChanged.connect(self._changed)
        for field in (self.pending, self.bookmarks):
            field.toggled.connect(self._changed)
        self.refresh()

    def _changed(self, *_):
        self.page = 0
        self.refresh(force=True)

    def _turn(self, delta):
        self.page = max(0, self.page + delta)
        self.refresh(force=True)

    def _params(self):
        time_filters = {"speed": self.speed_filter.currentData(), "time_control": self.time_filter.text().strip()}
        if self.tabs.currentIndex() == 1:
            return self.library.game_page, {
                **time_filters,
                "query": self.game_search.text().strip(), "bookmarks": self.bookmarks.isChecked(),
                "sort": self.game_sort.currentData(),
            }
        upper, lower = self.clock.currentData()
        return self.library.discovery_page, {
            **time_filters,
            "query": self.search.text().strip(), "sort": self.sort.currentData(), "review": self.review.currentData(),
            "clock": upper, "clock_min": lower, "eco": self.eco.text().strip(), "pending": self.pending.isChecked(),
        }

    def refresh(self, force=False):
        if not force and self.task and self.task.isRunning():
            return
        query, options = self._params()
        tab, page = self.tabs.currentIndex(), self.page
        request = (tab, page, options)
        if force and request != self.wanted:
            self.rows = []
            self.open.setEnabled(False)
            self.prev.setEnabled(False)
            self.next.setEnabled(False)
            self.discovery_table.setRowCount(0)
            self.game_table.setRowCount(0)
            self.meta.setText("Loading results…")
            self.last_signature = None
        self.wanted = request
        if self.fetch is not None:
            return

        def load(_):
            try:
                return query(**options, limit=PAGE + 1, offset=page * PAGE)
            finally:
                self.library.close_thread()

        def receive(results):
            if self.wanted == request:
                self._show_results(tab, page, results)

        def done():
            self.fetch = None
            task.deleteLater()
            if self.wanted != request:
                self.refresh()

        task = self.fetch = Task(load, self)
        task.succeeded.connect(receive)
        task.failed.connect(lambda message: self.meta.setText(message) if self.wanted == request else None)
        task.finished.connect(done)
        task.start()

    def _show_results(self, tab, page, fetched):
        next_page = len(fetched) > PAGE
        rows = fetched[:PAGE]
        signature = (tab, page, rows, next_page)
        if signature == self.last_signature:
            return
        self.last_signature = signature
        self.rows = rows
        actual = self.game_table if tab == 1 else self.discovery_table
        actual.setRowCount(0)
        actual.setRowCount(len(rows))
        for row, item in enumerate(rows):
            if actual is self.game_table:
                self._game_row(row, item)
            else:
                self._discovery_row(row, item)
        self.prev.setEnabled(page > 0)
        self.next.setEnabled(next_page)
        self.open.setEnabled(bool(rows))
        if rows:
            first = page * PAGE + 1
            self.meta.setText(f"Results {first:,}–{first + len(rows) - 1:,}  ·  Double-click a row to open")
        else:
            self.meta.setText("No matching results. Run a scan or adjust the filters.")

    def _game_row(self, row, item):
        game = ("★ " if item["bookmarked"] else "") + (item["white"] or "Unknown") + (f"  {item['white_elo']}" if item["white_elo"] is not None else "")
        other = (item["black"] or "Unknown") + (f"  {item['black_elo']}" if item["black_elo"] is not None else "")
        status = {"pending": "Discovery queued", "running": "Analyzing", "failed": "Discovery failed"}.get(item["analysis_state"])
        if status is None:
            status = f"{item['pending']} pending" if item["pending"] else "Analyzed"
            if item["reviewed"]:
                status += " · Reviewed"
        values = (game, other, item["result"], item["day"], item["time_control"] or item["speed"],
                  item["opening"], item["moments"], item["missed"], status)
        for column, value in enumerate(values):
            self.game_table.setItem(row, column, cell(value, ACCENT if column == 6 else None, str(value) if value else None))

    def _discovery_row(self, row, item):
        game = self.library.game(item["scan_game"])
        board = board_at(game["root"], game["moves"], item["ply"] - 1)
        san = board.san(chess.Move.from_uci(item["played"]))
        turn = f"{board.fullmove_number}{'.' if board.turn else '…'}"
        ratings = " / ".join("—" if item[f"{color}_elo"] is None else str(item[f"{color}_elo"])
                             for color in ("white", "black"))
        loss = item["loss_cp"]
        loss_text = f"{loss / 100:.2f}" if loss is not None else "Mate"
        state = NAMES[item["state"]]
        if item["detail_state"] == "failed":
            state = "Verification failed"
        if item["bookmarked"]:
            state = "★ " + state
        if item["missed"] and item["state"] != "candidate":
            state = "Missed reply · " + state
        values = (_prob(item["probability"]), loss_text, f"{turn} {san}",
                  f"{item['white'] or 'White'} vs {item['black'] or 'Black'}",
                  ratings, (item["speed"] or "").capitalize() or None, item["opening"], _clock(item["clock"]),
                  item["observed"], state)
        for column, value in enumerate(values):
            color = ACCENT if column == 0 and item["probability"] is not None else AMBER if column == 1 else MUTED if column in (7, 8) else None
            self.discovery_table.setItem(row, column, cell(value, color, str(value) if value else None))

    def _open_selected(self):
        actual = self.game_table if self.tabs.currentIndex() == 1 else self.discovery_table
        row = actual.currentRow()
        if row < 0 and self.rows:
            row = 0
        if row >= 0:
            self._open(row, 0)

    def _open(self, row, _):
        item = self.rows[row]
        if self.tabs.currentIndex() == 1:
            self.opened.emit(item["id"], None)
        else:
            self.opened.emit(item["scan_game"], item["id"])

    def _export(self, all_results):
        if self.task and self.task.isRunning():
            return
        is_games = self.tabs.currentIndex() == 1
        field = "games" if is_games else "moments"
        if all_results:
            query, params = self._params()

            def identities():
                offset = 0
                while batch := query(**params, offset=offset, limit=250):
                    yield from (item["id"] for item in batch)
                    offset += len(batch)

            targets = {field: identities()}
        else:
            widget = self.game_table if is_games else self.discovery_table
            rows = widget.selectionModel().selectedRows()
            if not rows:
                self.info.emit("Select one or more rows to export.")
                return
            targets = {field: [self.rows[i.row()]["id"] for i in rows]}
        self.task = export_ui(self, self.library, targets, self.info.emit)
        if self.task:
            self.task.finished.connect(lambda: setattr(self, "task", None))
