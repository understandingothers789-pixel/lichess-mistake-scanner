from __future__ import annotations

import io
from pathlib import Path

import chess
import chess.pgn
from PySide6.QtCore import QByteArray, QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDockWidget, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QMainWindow, QScrollArea, QSizePolicy, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..analysis import retained_outcome
from ..exports import san_line, write_export
from ..model import Score, SearchSettings, Variation, board_at, position_key
from .board import BoardWidget, EvalGraph
from .preview import VariationPreview
from .theme import ACCENT, AMBER, MAIA, MUTED, Task, button, cell, error, label, score_color, table, time_control_label

FORMATS = {
    "Annotated PGN": ("pgn", "pgn"), "Original PGN": ("original", "pgn"),
    "FEN positions": ("fen", "fen"), "Discovery CSV": ("csv", "csv"),
    "Discovery JSON": ("json", "json"), "Puzzle FEN and solution": ("puzzle", "json"),
}
NAMES = {
    "candidate": "Awaiting verification", "verified": "Verified · ratings unavailable",
    "awaiting_maia": "Verified · Maia pending", "complete": "Complete",
}


def export_ui(parent, library, targets, status):
    chosen, ok = QInputDialog.getItem(parent, "Export analysis", "Format", list(FORMATS), 0, False)
    if not ok:
        return None
    kind, extension = FORMATS[chosen]
    filename, _ = QFileDialog.getSaveFileName(parent, "Save export", f"second-look.{extension}",
                                               f"{chosen} (*.{extension})")
    if not filename:
        return None

    def run(_):
        try:
            return write_export(library, Path(filename), kind, **targets)
        finally:
            library.close_thread()

    task = Task(run, parent)
    task.succeeded.connect(lambda count: status(f"Exported {count:,} item{'s' if count != 1 else ''} to {filename}"))
    task.failed.connect(lambda message: error(parent, "Export failed", message))
    task.finished.connect(task.deleteLater)
    task.start()
    return task


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}:{seconds:02d}"


def recorded_clocks(game: dict, ply: int) -> dict[chess.Color, float | None]:
    """Reconstruct both clocks from the actual game at the selected mainline ply."""
    root = chess.Board(game["root"])
    base, separator, increment = (game.get("time_control") or "").partition("+")
    starting_time = int(base) if (root.fen() == chess.STARTING_FEN and separator
                                  and base.isdigit() and increment.isdigit()) else None
    clocks = {chess.WHITE: starting_time, chess.BLACK: starting_time}
    side = root.turn
    for seconds in game.get("clocks", ())[:ply]:
        clocks[side] = seconds
        side = not side
    return clocks


def _prob(value) -> str:
    if value is None:
        return "—"
    return "<0.1%" if 0 < value < 0.001 else f"{value:.1%}"


def _score_text(score, depth) -> str:
    if score is None:
        return "—"
    return f"{score.display()} (d{depth})" if depth else f"{score.display()} (final position)"


class Review(QWidget):
    reanalyze = Signal(int)
    open_library = Signal()
    open_scanner = Signal()
    review_next = Signal()
    review_stop = Signal()
    position_opened = Signal(int, int)
    info = Signal(str)

    def __init__(self, library, runner, parent=None):
        super().__init__(parent)
        self.library = library
        self.runner = runner
        self.live_key = None
        self.live_future = None
        self.live_lines = None
        self.live_error = ""
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(100)
        self.live_timer.timeout.connect(self._poll_live)
        self.comparison_future = None
        self.comparison = None
        self.comparison_error = ""
        self.comparison_timer = QTimer(self)
        self.comparison_timer.setInterval(100)
        self.comparison_timer.timeout.connect(self._poll_comparison)
        self.game = None
        self.explore = False
        self.scan = None
        self.moments = []
        self.current = None
        self.ply = 0
        self.move_ply = None
        self.branch = []
        self.step = 0
        self.variation_preview = VariationPreview(self)
        self.fast_depth = 4
        self.position_info = {}
        self.last_signature = None
        self.export_task = None
        self.lines = []
        self.evaluations = {}
        self.score_items = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 12)
        outer.setSpacing(8)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.heading = label("Analysis workspace", "title")
        self.context = label("", "muted")
        titles.addWidget(self.heading)
        titles.addWidget(self.context)
        header.addLayout(titles, 1)
        self.open_source = button("Lichess game ↗", self._open_source, tip="Open the game at this move on Lichess.")
        self.reanalysis_button = button("Reanalyze", lambda: self.reanalyze.emit(self.game["scan_game"]),
                                        tip="Analyze this game as a new scan with the selected engine")
        self.live_button = button("Live Stockfish", tip="Analyze the position currently on the board")
        self.live_button.setCheckable(True)
        self.live_button.toggled.connect(self._toggle_live)
        self.export_button = button("Export", self._export, tip="Save this game or its analysis to a file.")
        self.library_button = button("Open library", self.open_library.emit, tip="Browse previously analyzed games and findings.")
        self.scan_button = button("Start a scan", self.open_scanner.emit, role="primary",
                                  tip="Choose a source and scan games for mistakes.")
        for control in (self.open_source, self.live_button, self.reanalysis_button, self.export_button):
            header.addWidget(control)
        outer.addLayout(header)

        self.welcome = QWidget()
        welcome_layout = QVBoxLayout(self.welcome)
        welcome_layout.addStretch(1)
        welcome_card = QFrame()
        welcome_card.setObjectName("card")
        welcome_card.setMinimumWidth(520)
        welcome_card.setMaximumWidth(640)
        guide = QVBoxLayout(welcome_card)
        guide.setContentsMargins(26, 24, 26, 24)
        guide.setSpacing(10)
        guide.addWidget(label("Find a game to review", "title"))
        for title, description in (
            ("Set up Stockfish", "Stockfish finds mistakes. You can add Maia later to estimate how likely players are to make them."),
            ("Scan a small sample", "Try a Lichess game URL or a PGN file. Scanner shows progress as each game is analyzed."),
            ("Open a result", "Double-click a finding in Library → Discoveries. Scroll over the board to step through the moves."),
        ):
            guide.addWidget(label(title, "section"))
            detail = label(description, "muted", wrap=True)
            policy = detail.sizePolicy()
            policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
            detail.setSizePolicy(policy)
            guide.addWidget(detail)
        actions = QHBoxLayout()
        actions.addWidget(self.scan_button)
        actions.addWidget(self.library_button)
        actions.addWidget(button("Explore the board", self._explore_board,
                                 tip="Play moves from the starting position without importing a game."))
        actions.addStretch()
        guide.addLayout(actions)
        welcome_row = QHBoxLayout()
        welcome_row.addStretch(1)
        welcome_row.addWidget(welcome_card, 3)
        welcome_row.addStretch(1)
        welcome_layout.addLayout(welcome_row)
        welcome_layout.addStretch(1)
        outer.addWidget(self.welcome, 1)

        self.workspace = QMainWindow()
        self.workspace.setDockNestingEnabled(True)
        self.workspace.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)
        outer.addWidget(self.workspace, 1)

        board_card = QFrame()
        board_card.setObjectName("card")
        board_layout = QVBoxLayout(board_card)
        board_layout.setContentsMargins(10, 8, 10, 9)
        board_layout.setSpacing(6)
        self.top_player = label("♚  Black", "label")
        self.bottom_player = label("♚  White", "label")
        self.board = BoardWidget()
        self.board.setToolTip("Scroll down for the next move or up for the previous move. Click a piece and a destination to try a move.")
        self.board.move_played.connect(self._scratch)
        self.board.steps_requested.connect(self.advance)
        board_layout.addWidget(self.top_player)
        board_layout.addWidget(self.board, 1)
        lower = QHBoxLayout()
        lower.addWidget(self.bottom_player, 1)
        self.board_position = label("Starting position", "muted")
        lower.addWidget(self.board_position)
        board_layout.addLayout(lower)
        self.review_bar = QWidget()
        review_controls = QHBoxLayout(self.review_bar)
        review_controls.setContentsMargins(0, 0, 0, 0)
        self.review_progress = label("", "label")
        review_controls.addWidget(self.review_progress, 1)
        self.mark_next = button("Mark reviewed && next", self.review_next.emit, role="primary",
                                tip="Save this item as reviewed and go to the next matching result.")
        review_controls.addWidget(self.mark_next)
        review_controls.addWidget(button("Stop review", self.review_stop.emit, tip="End the review list and stay on this game."))
        self.review_bar.hide()
        board_layout.addWidget(self.review_bar)
        controls = QHBoxLayout()
        for text, callback, tip in (
            ("|‹", lambda: self.go(0), "First position"),
            ("‹", lambda: self.advance(-1), "Previous move"),
            ("›", lambda: self.advance(1), "Next move"),
            ("›|", lambda: self.go(len(self.game["moves"]) if self.game else 0), "Last position"),
        ):
            controls.addWidget(button(text, callback, tip=tip))
        controls.addWidget(button("Flip", self._flip, tip="Flip board"))
        self.return_line = button("Main line", self.leave_variation, tip="Exit the variation and return to the game")
        controls.addWidget(self.return_line)
        controls.addStretch()
        self.previous_moment = button("‹ Moment", lambda: self.jump(-1), tip="Previous interesting position")
        self.next_moment = button("Moment ›", lambda: self.jump(1), tip="Next interesting position")
        controls.addWidget(self.previous_moment)
        controls.addWidget(self.next_moment)
        board_layout.addLayout(controls)
        self.workspace.setCentralWidget(board_card)

        chart_box = QWidget()
        chart_box.setObjectName("card")
        chart_layout = QVBoxLayout(chart_box)
        chart_layout.setContentsMargins(4, 2, 7, 4)
        self.graph = EvalGraph()
        self.graph.setToolTip("Click the graph to jump to a move. Higher values favor White and lower values favor Black.")
        self.graph.selected.connect(self.go)
        chart_layout.addWidget(self.graph)
        legend = label("White perspective · Dotted: fast · Teal: detailed · Amber: finding", "muted", wrap=True)
        legend.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chart_layout.addWidget(legend)
        self.chart_dock = self._dock("Evaluation graph", chart_box, Qt.DockWidgetArea.BottomDockWidgetArea)

        analysis = QWidget()
        analysis.setObjectName("card")
        analysis_layout = QVBoxLayout(analysis)
        analysis_layout.setContentsMargins(9, 7, 9, 7)
        analysis_layout.setSpacing(6)
        position = QHBoxLayout()
        self.position_label = label("", "label", wrap=True)
        position.addWidget(self.position_label, 1)
        self.position_eval = label("—", "eval")
        self.position_eval.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.position_eval.setMinimumWidth(65)
        position.addWidget(self.position_eval)
        analysis_layout.addLayout(position)
        phase = QHBoxLayout()
        self.before = button("Before move", lambda: self._phase(False), tip="See the choices available before this move was played.")
        self.after = button("After move", lambda: self._phase(True), tip="See the position and possible replies after the move.")
        self.before.setCheckable(True)
        self.after.setCheckable(True)
        phase.addWidget(self.before)
        phase.addWidget(self.after)
        self.compare_button = button("Compare move", self._compare_move, tip="Run a fresh before-and-after Stockfish search")
        phase.addWidget(self.compare_button)
        phase.addStretch()
        analysis_layout.addLayout(phase)
        self.scan_scores = label("", "muted", wrap=True)
        self.scan_decision = label("", "muted", wrap=True)
        self.comparison_status = label("", "label", wrap=True)
        analysis_layout.addWidget(self.scan_scores)
        analysis_layout.addWidget(self.scan_decision)
        analysis_layout.addWidget(self.comparison_status)
        self.line_table = table(["Move", "Eval", "Continuation", "Depth", "Maia"])
        self.line_table.setToolTip("Hover a line for a small board preview. Click a line to play it on the main board.")
        for column, tip in enumerate((
            "First move in the suggested line.", "Positive scores favor White. Negative scores favor Black.",
            "Moves Stockfish expects after the first move.", "How far Stockfish searched, in half-moves.",
            "How likely Maia predicts a player at the shown rating would choose this move.",
        )):
            self.line_table.horizontalHeaderItem(column).setToolTip(tip)
        self.line_table.horizontalHeader().setStretchLastSection(False)
        for index, width in enumerate((64, 60, 190, 51, 62)):
            self.line_table.setColumnWidth(index, width)
        self.line_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.line_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.line_table.setMouseTracking(True)
        self.line_table.itemEntered.connect(self._preview)
        self.line_table.cellClicked.connect(self._play_variation)
        self.line_table.viewport().installEventFilter(self)
        self.line_table.setMinimumHeight(180)
        analysis_layout.addWidget(self.line_table, 1)
        self.summary = label("Open a discovery to see its full Stockfish variations.", "muted", wrap=True)
        self.maia = label("", "maia", wrap=True)
        analysis_layout.addWidget(self.summary)
        analysis_layout.addWidget(self.maia)
        options = QHBoxLayout()
        self.all_policy = button("All Maia moves", self._policy, tip="See Maia’s predicted chances for every legal move.")
        self.occurrences = button("Position occurrences", self._occurrences,
                                  tip="Find other games in your library that reached this position.")
        options.addWidget(self.all_policy)
        options.addWidget(self.occurrences)
        options.addStretch()
        analysis_layout.addLayout(options)
        self.engine_dock = self._dock("Stockfish and Maia", analysis, Qt.DockWidgetArea.RightDockWidgetArea, scroll=True)

        score_box = QWidget()
        score_box.setObjectName("card")
        score_layout = QVBoxLayout(score_box)
        score_layout.setContentsMargins(7, 6, 7, 6)
        score_layout.setSpacing(4)
        self.moves = QTreeWidget()
        self.moves.setToolTip("Click either player's move to jump there. Expand branches to see saved variations.")
        self.moves.setAccessibleName("Annotated PGN move list")
        self.moves.setHeaderLabels(["#", "White", "Eval", "Black", "Eval"])
        self.moves.setAlternatingRowColors(True)
        self.moves.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        for col, width in ((0, 56), (2, 62), (4, 62)):
            self.moves.setColumnWidth(col, width)
        score_header = self.moves.header()
        score_header.setStretchLastSection(False)
        for col in (1, 3):
            score_header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.moves.setIndentation(11)
        self.moves.setUniformRowHeights(True)
        self.moves.setMinimumHeight(110)
        self.moves.itemClicked.connect(self._score_clicked)
        score_layout.addWidget(self.moves, 1)
        review_tools = QHBoxLayout()
        self.bookmark = button("Bookmark game", lambda: self._review("bookmarked", True), tip="Toggle this game’s bookmark in the Library.")
        self.reviewed = button("Mark reviewed", lambda: self._review("reviewed", True), tip="Toggle the reviewed status for the full game.")
        review_tools.addWidget(self.bookmark)
        review_tools.addWidget(self.reviewed)
        review_tools.addStretch()
        score_layout.addLayout(review_tools)
        self.score_dock = self._dock("Game score", score_box, Qt.DockWidgetArea.RightDockWidgetArea, scroll=True)
        self.workspace.splitDockWidget(self.engine_dock, self.score_dock, Qt.Orientation.Vertical)

        moments_box = QWidget()
        moments_box.setObjectName("card")
        moment_layout = QVBoxLayout(moments_box)
        moment_layout.setContentsMargins(7, 6, 7, 6)
        moment_layout.setSpacing(4)
        self.moment_table = table(["Move", "Finding", "Maia"])
        self.moment_table.setToolTip("Click a finding to jump to the position before the mistake or missed reply.")
        self.moment_table.setColumnWidth(0, 92)
        self.moment_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.moment_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.moment_table.cellClicked.connect(self._moment_clicked)
        self.moment_table.setMinimumHeight(80)
        moment_layout.addWidget(self.moment_table)
        tools = QHBoxLayout()
        self.bookmark_moment = button("Bookmark moment", lambda: self._review("bookmarked", False),
                                      tip="Toggle this finding’s bookmark in the Library.")
        self.review_moment = button("Review moment", lambda: self._review("reviewed", False), tip="Toggle the reviewed status for this finding.")
        tools.addWidget(self.bookmark_moment)
        tools.addWidget(self.review_moment)
        tools.addStretch()
        moment_layout.addLayout(tools)
        self.moment_dock = self._dock("Interesting moments", moments_box, Qt.DockWidgetArea.RightDockWidgetArea, scroll=True)
        self.workspace.splitDockWidget(self.score_dock, self.moment_dock, Qt.Orientation.Vertical)
        self.workspace.setTabPosition(Qt.DockWidgetArea.RightDockWidgetArea, QTabWidget.TabPosition.North)
        self.workspace.resizeDocks([self.engine_dock], [565], Qt.Orientation.Horizontal)
        self.workspace.resizeDocks([self.engine_dock, self.score_dock, self.moment_dock], [420, 190, 150], Qt.Orientation.Vertical)
        self.workspace.resizeDocks([self.chart_dock], [108], Qt.Orientation.Vertical)
        self.reset_state = self.workspace.saveState()
        self.compact = False
        self.layout_states = {}
        for compact, key in ((False, "analysis_layout"), (True, "analysis_compact_layout")):
            saved = self.library.setting(key)
            if saved:
                self.layout_states[compact] = QByteArray.fromHex(saved.encode())
        self.workspace.restoreState(self.layout_states.get(False, self.reset_state))
        self._render()

    def _dock(self, title, content, area, *, scroll=False):
        dock = QDockWidget(title)
        dock.setObjectName(title.replace(" ", "_"))
        if scroll:
            viewport = QScrollArea()
            viewport.setWidgetResizable(True)
            viewport.setMinimumWidth(370)
            viewport.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            viewport.setWidget(content)
            content = viewport
        dock.setWidget(content)
        self.workspace.addDockWidget(area, dock)
        return dock

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = event.size().width() < 1120 or event.size().height() < 690
        if compact != self.compact:
            self.layout_states[self.compact] = self.workspace.saveState()
            self.compact = compact
            for dock, short, full in (
                (self.engine_dock, "Engine", "Stockfish and Maia"),
                (self.score_dock, "Score", "Game score"),
                (self.moment_dock, "Findings", "Interesting moments"),
                (self.chart_dock, "Graph", "Evaluation graph"),
            ):
                dock.setWindowTitle(short if compact else full)
            if compact and compact not in self.layout_states:
                self._compact_layout()
            else:
                self.workspace.restoreState(self.layout_states.get(compact, self.reset_state))

    def _compact_layout(self):
        self.workspace.restoreState(self.reset_state)
        for dock in (self.score_dock, self.moment_dock, self.chart_dock):
            self.workspace.tabifyDockWidget(self.engine_dock, dock)
        self.workspace.resizeDocks([self.engine_dock], [390], Qt.Orientation.Horizontal)
        self.engine_dock.raise_()

    def reset_layout(self):
        self.layout_states.clear()
        if self.compact:
            self._compact_layout()
        else:
            self.workspace.restoreState(self.reset_state)

    def set_review_progress(self, current=0, total=0, *, is_game=False):
        self.review_progress.setText(f"{'Game' if is_game else 'Finding'} {current:,} of {total:,}")
        self.mark_next.setText("Mark reviewed && finish" if current == total else "Mark reviewed && next")
        self.review_bar.setVisible(total > 0)

    def save(self):
        self.layout_states[self.compact] = self.workspace.saveState()
        for compact, key in ((False, "analysis_layout"), (True, "analysis_compact_layout")):
            saved = self.layout_states.get(compact)
            self.library.set_setting(key, bytes(saved.toHex()).decode() if saved is not None else "")

    def load(self, scan_game: int, moment_id: int | None = None, ply: int | None = None):
        self._clear_comparison()
        self.game = self.library.game(scan_game)
        if self.game is None:
            return
        self.scan = self.library.scan(self.game["scan_id"])
        self.fast_depth = self.scan["rules"]["fast_depth"]
        self.last_signature = None
        saved = moment_id or (self.game["cursor_moment"] if ply is None else None)
        self.current = self.library.mistake(saved) if saved else None
        self.ply = self.current["ply"] - 1 if moment_id and self.current else self.game["cursor"] if ply is None else ply
        self.move_ply = self.current["ply"] if self.current else None
        self.branch, self.step = [], 0
        profile = self.scan["profile"]
        self.heading.setText(f"{self.game['white'] or 'White'}  vs  {self.game['black'] or 'Black'}")
        metadata = [self.game["day"], (self.game["speed"] or "").capitalize(), time_control_label(self.game["time_control"]),
                    self.game["opening"], self.game["eco"], self.game["result"]]
        self.context.setText("  ·  ".join(str(value) for value in metadata if value))
        self.context.setToolTip(f"{profile.get('version', profile.get('name', 'Stockfish'))}\n"
                                f"Commit {profile.get('commit') or 'unavailable'}\n"
                                f"Executable SHA-256 {profile['sha256']}")
        self.refresh()
        if moment_id or ply is not None:
            self.library.review(scan_game, game=True, cursor=self.ply,
                                cursor_moment=self.current["id"] if self.current else None)

    def refresh(self):
        if not self.game:
            return
        self.game = self.library.game(self.game["scan_game"])
        moments = self.library.moments(self.game["scan_game"])
        evaluations = self.library.evaluations(self.game["scan_game"])
        signature = (len(evaluations), tuple((m["id"], m["state"], m["probability"], m["reviewed"], m["bookmarked"])
                                             for m in moments))
        if signature != self.last_signature:
            self.last_signature = signature
            self.moments, self.evaluations = moments, evaluations
            self.position_info.clear()
            for moment in moments:
                detail = moment["detail"]
                if detail:
                    for before, field in ((True, "before_key"), (False, "after_key")):
                        point = moment["ply"] - int(before)
                        lines = self.library.cached_search(detail[field])
                        if lines:
                            self.position_info[point] = lines[0]
            self._score_rows()
            markers = {m["ply"]: m["state"] for m in self.moments}
            series = {ply: (Score(**item["score"]), item["depth"], item["depth"] == self.fast_depth, False)
                      for ply, item in evaluations.items()}
            series.update({ply: (line.score, line.depth, False, True) for ply, line in self.position_info.items()})
            self.graph.set_data(series, markers, len(self.game["moves"]))
            self._moment_rows()
            if self.move_ply is None:
                first = self._at_ply()
                self.move_ply = first["ply"] if first else self.ply or (1 if self.game["moves"] else None)
            self.current = next((m for m in moments if m["ply"] == self.move_ply), None)
            self._select_moment()
            self._render()

    def _scan_score(self, ply):
        deep = self.position_info.get(ply)
        if deep is not None:
            return deep.score, deep.depth
        discovery = self.evaluations.get(ply)
        return (Score(**discovery["score"]), discovery["depth"]) if discovery else (None, None)

    def _score_rows(self):
        self.moves.clear()
        self.score_items = {}
        game = chess.pgn.read_game(io.StringIO(self.game["original"]))
        if not game:
            return
        moments = {m["ply"]: m for m in self.moments}
        root = QTreeWidgetItem(["", "Start", "", "", ""])
        self.moves.addTopLevelItem(root)
        self.score_items[0] = root, 1
        root.setData(1, Qt.ItemDataRole.UserRole, (0, ()))

        def branch(node, item, anchor, pv):
            board = node.parent.board()
            col = 1 if board.turn else 3
            score = node.eval()
            row = QTreeWidgetItem([f"{board.fullmove_number}." if board.turn else f"{board.fullmove_number}…"])
            row.setText(col, node.san())
            row.setForeground(col, QColor(MUTED))
            if score:
                evaluation = Score.from_uci(score)
                row.setText(col + 1, evaluation.display())
                row.setForeground(col + 1, QColor(score_color(evaluation)))
            item.addChild(row)
            line = (*pv, node.move.uci())
            for column in (col, col + 1):
                row.setData(column, Qt.ItemDataRole.UserRole, (anchor, line))
                row.setToolTip(column, f"PGN variation · d{node.eval_depth()}" if node.eval_depth() else "PGN variation")
            for child in node.variations:
                branch(child, row, anchor, line)

        row = None
        for ply, node in enumerate(game.mainline(), 1):
            move_board = node.parent.board()
            col = 1 if move_board.turn else 3
            if row is None or move_board.turn:
                row = QTreeWidgetItem([f"{move_board.fullmove_number}."])
                self.moves.addTopLevelItem(row)
            score, depth = self._scan_score(ply)
            scan_label = f"Saved scan {_score_text(score, depth)}" if score else ""
            moment = moments.get(ply)
            state = ""
            if moment:
                state = "Candidate" if moment["state"] == "candidate" else "Missed reply" if (
                    ply - 1 in moments and moments[ply - 1]["detail"]
                    and (moments[ply - 1]["detail"].get("punishment") or {}).get("missed")
                ) else "Mistake"
                row.setForeground(col, QColor(AMBER))
            row.setText(col, f"{node.san()}  {'?!' if state == 'Candidate' else '?' if state else ''}".rstrip())
            row.setText(col + 1, score.display() if score else "—")
            row.setForeground(col + 1, QColor(score_color(score) if score else MUTED))
            for column in (col, col + 1):
                row.setData(column, Qt.ItemDataRole.UserRole, (ply, ()))
                row.setToolTip(column, " · ".join(part for part in (node.san(), scan_label, state) if part))
            self.score_items[ply] = row, col
            for alternative in node.parent.variations[1:]:
                branch(alternative, self.score_items[ply - 1][0], ply - 1, ())
        self.moves.setCurrentItem(*self.score_items.get(self.ply, (root, 1)))

    def _moment_rows(self):
        self.moment_table.setRowCount(len(self.moments))
        for row, moment in enumerate(self.moments):
            board = board_at(self.game["root"], self.game["moves"], moment["ply"] - 1)
            notation = board.san(chess.Move.from_uci(moment["played"]))
            detail = moment["detail"]
            reason = detail["reason"] if detail else moment["discovery"]["reason"]
            status = "Verify · " if moment["state"] == "candidate" else ""
            if row and self.moments[row - 1]["ply"] == moment["ply"] - 1:
                prev = self.moments[row - 1]["detail"] or {}
                if (prev.get("punishment") or {}).get("missed") and detail:
                    status = "Missed reply · "
            number = f"{board.fullmove_number}{'.' if board.turn else '…'} {notation}"
            for col, value in enumerate((number, status + reason, _prob(moment["probability"]))):
                self.moment_table.setItem(row, col, cell(value, AMBER if col == 1 else None, NAMES[moment["state"]]))

    def _explore_board(self):
        self.explore = True
        self._render()

    def _render(self):
        has_board = self.game is not None or self.explore
        self.welcome.setVisible(not has_board)
        self.workspace.setVisible(has_board)
        self.live_button.setVisible(has_board)
        self.heading.setVisible(has_board)
        self.context.setVisible(self.game is not None)
        self.variation_preview.dismiss()
        board = self._root()
        if self.branch:
            for move in self.branch[:self.step]:
                board.push_uci(move)
        moment = self.current
        move_ply = self.move_ply
        score, depth = self._scan_score(self.ply)
        score_source = "Saved scan"
        self.line_table.setRowCount(0)
        self.lines = []
        policy = None
        good = ()
        hint = None
        can_inspect = move_ply is not None and not self.branch
        self.before.setEnabled(can_inspect)
        self.after.setEnabled(can_inspect)
        self.compare_button.setEnabled(can_inspect and self.comparison_future is None)
        after = move_ply is not None and self.ply == move_ply
        self.before.setChecked(can_inspect and not after)
        self.after.setChecked(can_inspect and after)
        self.position_label.setText("")
        self.maia.setText("")
        self.scan_scores.setVisible(can_inspect)
        self.scan_decision.setVisible(can_inspect)
        if can_inspect:
            root = board_at(self.game["root"], self.game["moves"], move_ply - 1)
            actual = root.san(chess.Move.from_uci(self.game["moves"][move_ply - 1]))
            self.position_label.setText(f"{root.fullmove_number}{'.' if root.turn else '…'} {actual}")
            saved_before = _score_text(*self._scan_score(move_ply - 1))
            saved_after = _score_text(*self._scan_score(move_ply))
            self.scan_scores.setText(f"Saved scan positions · Before {saved_before} · After {saved_after}")
            self.scan_decision.setText(self._decision_text(move_ply, root.turn))
        self._render_comparison(can_inspect)
        if moment:
            self._render_moment(moment, bool(after))
            data = moment["detail"]
            if data:
                self.lines = self.library.cached_search(data["after_key" if after else "before_key"]) or []
                policy = self.library.policy(moment["after_policy" if after else "before_policy"])
                good = data["good_after" if after else "good_before"]
        else:
            self.summary.setText("" if self.game else "Open a game or discovery to analyze moves.")
        self.lines_board = self._root()
        live_lines = False
        if self.branch and not self.live_button.isChecked():
            score, hint = None, None
        if self.comparison is not None and can_inspect:
            before, _, replies = self.comparison
            self.lines = replies if after else before
            live_lines = True
            good = ()
        elif self.live_button.isChecked() and self.isVisible():
            self.lines_board = board
            self._request_live(board)
            self.lines = self.live_lines or []
            live_lines = True
            good = ()
            if self.branch and self.step:
                policy = None
                self.maia.clear()
                self.summary.setText("Live analysis of a variation.")
            phase = self.live_error or ("Searching or waiting for a Stockfish worker…" if self.live_lines is None else
                                        f"Live Stockfish · depth {self.live_lines[0].depth}" if self.live_lines[0].pv else
                                        "Live Stockfish · game over")
            self.position_label.setText(phase)
        if self.lines:
            if live_lines or not self.branch:
                score, depth = self.lines[0].score, self.lines[0].depth
                hint = self.lines[0].pv[0] if self.lines[0].pv else None
                if live_lines:
                    score_source = "Live Stockfish"
            self._show_lines(self.lines_board, good, policy, live=live_lines)
        self.summary.setVisible(bool(self.summary.text()))
        self.maia.setVisible(bool(self.maia.text()))
        clocks = None
        if self.game:
            clocks = ({chess.WHITE: None, chess.BLACK: None} if self.branch and self.step
                      else recorded_clocks(self.game, self.ply))
        self.position_eval.setText(score.display() if score else "—")
        self.position_eval.setToolTip(f"{score_source} · {_score_text(score, depth)} · White perspective" if score else "")
        self.board.set_position(board, score, hint, clocks=clocks)
        self.graph.set_ply(self.ply)
        self.all_policy.setEnabled(policy is not None and bool(policy))
        self.occurrences.setEnabled(bool(self.game))
        self.previous_moment.setEnabled(bool(self.moments))
        self.next_moment.setEnabled(bool(self.moments))
        self.return_line.setVisible(bool(self.branch))
        self.open_source.setVisible(bool(self.game and (self.game["site"] or "").startswith("https://lichess.org/")))
        self.reanalysis_button.setVisible(bool(self.game))
        self.export_button.setVisible(bool(self.game))
        self.bookmark.setEnabled(self.game is not None)
        self.reviewed.setEnabled(self.game is not None)
        self.bookmark_moment.setEnabled(moment is not None)
        self.review_moment.setEnabled(moment is not None)
        if self.game:
            self.bookmark.setText("Remove game bookmark" if self.game["bookmarked"] else "Bookmark game")
            self.reviewed.setText("Mark unreviewed" if self.game["reviewed"] else "Mark reviewed")
        if moment:
            self.bookmark_moment.setText("Remove bookmark" if moment["bookmarked"] else "Bookmark moment")
            self.review_moment.setText("Mark unreviewed" if moment["reviewed"] else "Review moment")
        self._players()
        self.board_position.setText(f"Variation  {self.step}/{len(self.branch)}" if self.branch else
                                    f"Ply {self.ply} / {len(self.game['moves'])}" if self.game else "Starting position")

    def _show_lines(self, root, good, policy, *, live):
        self.lines = [line for line in self.lines if line.pv]
        self.line_table.setRowCount(len(self.lines))
        for i, line in enumerate(self.lines):
            move = line.pv[0]
            continuation_board = root.copy()
            san = continuation_board.san_and_push(chess.Move.from_uci(move))
            continuation = san_line(continuation_board, line.pv[1:]) if len(line.pv) > 1 else "—"
            source = "Live Stockfish" if live else "Saved scan"
            tip = f"{san_line(root, line.pv)} · {source} · d{line.depth}"
            if not live:
                tip += " · Acceptable" if move in good else " · Outside tolerance"
            if line.nodes is not None:
                tip += f" · {line.nodes:,} nodes"
            self.line_table.setItem(i, 0, cell(san + (" ✓" if move in good else ""), ACCENT if move in good else None, tip))
            self.line_table.setItem(i, 1, cell(line.score.display(), score_color(line.score), tip))
            self.line_table.setItem(i, 2, cell(continuation, None, tip))
            self.line_table.setItem(i, 3, cell(f"d{line.depth}", MUTED, tip))
            self.line_table.setItem(i, 4, cell(_prob(None if policy is None else policy.get(move)), MAIA, tip))

    def _toggle_live(self, enabled):
        if enabled:
            self._clear_comparison()
        else:
            self._clear_live()
        self._render()

    def _clear_live(self):
        if self.live_future is not None:
            self.live_future.cancel()
        self.live_future = self.live_key = self.live_lines = None
        self.live_error = ""
        self.live_timer.stop()

    def _request_live(self, board):
        profile = self.library.engine(self.library.setting("active_engine"))
        if profile is None:
            self._clear_live()
            self.live_error = "Select Stockfish in Engine Manager."
            return
        rules = SearchSettings(**self.library.setting("rules", {}))
        identity = (profile["id"], rules, board.root().fen(), tuple(board.move_stack))
        if identity != self.live_key:
            self._clear_live()
            self.live_key = identity
            self.live_future = self.runner.analyze_position(profile, rules, board)
            self.live_timer.start()

    def _poll_live(self):
        if self.live_future is None or not self.live_future.done():
            return
        self.live_timer.stop()
        try:
            self.live_lines = self.live_future.result()
        except Exception as exc:
            self.live_error = f"Stockfish: {exc}"
        self._render()

    def _clear_comparison(self):
        if self.comparison_future is not None:
            self.comparison_future.cancel()
        self.comparison_future = self.comparison = None
        self.comparison_error = ""
        self.comparison_timer.stop()

    def _compare_move(self):
        if self.move_ply is None or self.branch or self.comparison_future is not None:
            return
        self.live_button.setChecked(False)
        self._clear_comparison()
        profile = self.library.engine(self.library.setting("active_engine"))
        if profile is None:
            self.comparison_error = "Select Stockfish in Engine Manager to compare this move."
        else:
            rules = SearchSettings(**self.library.setting("rules", {}))
            before = board_at(self.game["root"], self.game["moves"], self.move_ply - 1)
            played_move = self.game["moves"][self.move_ply - 1]
            self.comparison_future = self.runner.analyze_position(profile, rules, before, played_move=played_move)
            self.comparison_timer.start()
        self._render()

    def _poll_comparison(self):
        if self.comparison_future is None or not self.comparison_future.done():
            return
        self.comparison_timer.stop()
        try:
            self.comparison = self.comparison_future.result()
        except Exception as exc:
            self.comparison_error = f"Stockfish: {exc}"
        self.comparison_future = None
        self._render()

    def _render_comparison(self, can_inspect):
        text = self.comparison_error
        if self.comparison_future is not None:
            text = "Live Stockfish · Searching before and after the move…"
        elif self.comparison is not None:
            before, played, replies = self.comparison
            best, reply = before[0], replies[0]
            root = board_at(self.game["root"], self.game["moves"], self.move_ply - 1)
            best_move = root.san(chess.Move.from_uci(best.pv[0]))
            played_move = root.san(chess.Move.from_uci(played.pv[0]))
            text = (f"Live Stockfish · Best {best_move} {_score_text(best.score, best.depth)} · "
                    f"Played {played_move} {_score_text(played.score, played.depth)} · After {_score_text(reply.score, reply.depth)}")
        self.comparison_status.setText(text)
        self.comparison_status.setVisible(can_inspect and bool(text))

    def _decision_text(self, ply, turn):
        mistake, evaluation = self.library.move_decision(self.game["scan_game"], ply)
        rules = SearchSettings(**self.scan["rules"])
        if mistake:
            state = mistake["state"]
            if state == "candidate":
                return "Scan decision · Verification failed" if mistake["detail_state"] == "failed" else (
                    "Scan decision · Candidate awaiting verification")
            detail = mistake["detail"]
            depth = detail["actual"]["depth"]
            if state != "rejected":
                value = detail["loss_cp"]
                reason = f"{value / 100:.2f} pawn loss" if value is not None else detail["reason"]
                return f"Scan decision · Verified at d{depth} · {reason}"
            reason = detail.get("rejection")
            if reason is None:
                lines = self.library.cached_search(detail["before_key"])
                if lines:
                    best = lines[0].score.for_turn(turn)
                    actual = Score(**detail["actual"]["score"]).for_turn(turn)
                    reason = retained_outcome(best, actual, rules)
            suffix = f" · {self._decision_reason(*reason, rules)}" if reason else ""
            return f"Scan decision · Candidate rejected at d{depth}{suffix}"
        if not evaluation:
            return "Scan decision · Waiting for discovery"
        decision = evaluation["decision"]
        if not decision:
            return "Scan decision · This scan saved position scores only"
        code, change, depth = decision
        if code == "start":
            return f"Scan decision · This scan started checking mistakes at move {rules.first_move}"
        if code == "best":
            return f"Scan decision · Stockfish chose the played move at d{depth}"
        if code == "terminal":
            return "Scan decision · The game was already over"
        step = "Discovery screen" if code.startswith("screen_") else "Played-move check"
        return f"Scan decision · {step} at d{depth} · {self._decision_reason(code.removeprefix('screen_'), change, rules)}"

    @staticmethod
    def _decision_reason(code, change, rules):
        if code == "threshold":
            return f"{max(0, change) / 100:.2f} pawn loss · {rules.loss_cp / 100:.2f} threshold"
        if code == "decisive":
            return f"Both scores kept the same decisive outcome (±{rules.decisive_cp / 100:.2f})"
        if code == "mate":
            return "The mate change stayed below the scan’s loss rule"
        return "Stockfish supplied no comparable scores"

    def _render_moment(self, moment, after):
        root = board_at(self.game["root"], self.game["moves"], moment["ply"] - 1)
        actual = root.san(chess.Move.from_uci(moment["played"]))
        data = moment["detail"]
        best = root.san(chess.Move.from_uci(data["best"] if data else moment["discovery"]["best"]))
        self.position_label.setText(f"{root.fullmove_number}{'.' if root.turn else '…'} {actual}")
        if data is None:
            source = self.library.scan_progress(self.game["scan_id"])
            failed = source["jobs"].get(("detail", "failed"), 0)
            self.summary.setText(f"{NAMES[moment['state']]} · Found {actual} instead of {best}."
                                 + (" Check Scanner for failed jobs." if failed else ""))
            return
        loss = data["reason"] if data["loss_cp"] is None else f"{data['loss_cp'] / 100:.2f} pawn loss"
        classification = data["label_after" if after else "label_before"]
        played_score = Score(**data["actual"]["score"]).display()
        summary = f"Played {actual} ({played_score}) · Best {best} · {loss}\n{classification}"
        reply = data.get("punishment")
        if reply:
            root.push_uci(moment["played"])
            strongest = root.san(chess.Move.from_uci(reply["best"]))
            chosen = root.san(chess.Move.from_uci(reply["played"]))
            summary += f" · {'Missed reply' if reply['missed'] else 'Reply'} {chosen}, best {strongest}"
        self.summary.setText(summary)
        if moment["probability"] is None:
            self.maia.setText(NAMES[moment["state"]])
        else:
            policy = self.library.policy(moment["after_policy"])
            lines = self.library.cached_search(data["after_key"]) or []
            strongest = lines[0].pv[0] if lines and lines[0].pv else None
            text = f"Maia {moment['maia_elo']} · Mistake {_prob(moment['probability'])}"
            if strongest and policy:
                text += f" · Best reply {_prob(policy.get(strongest))} · Acceptable replies {_prob(sum(policy.get(m, 0) for m in data['good_after']))}"
            self.maia.setText(text)

    def _root(self):
        return board_at(self.game["root"], self.game["moves"], self.ply) if self.game else chess.Board()

    def _players(self):
        order = ("white", "black") if self.board.flip else ("black", "white")
        for position, color in zip((self.top_player, self.bottom_player), order):
            name = (self.game[color] if self.game else None) or color.capitalize()
            rating = self.game[f"{color}_elo"] if self.game else None
            position.setText(f"{'♔' if color == 'white' else '♚'}  {name}" + (f"   {rating}" if rating is not None else ""))

    def _flip(self):
        self.board.toggle()
        self._players()

    def go(self, ply: int, moment=None, *, move_ply=None):
        if not self.game:
            return
        self.ply = max(0, min(ply, len(self.game["moves"])))
        if moment is None:
            moment = (self._at_ply() if move_ply is None else
                      next((m for m in self.moments if m["ply"] == move_ply), None))
        if move_ply is None:
            move_ply = moment["ply"] if moment else self.ply or (1 if self.game["moves"] else None)
        if move_ply != self.move_ply:
            self._clear_comparison()
        self.move_ply = move_ply
        self.current = moment
        self.branch, self.step = [], 0
        self.library.review(self.game["scan_game"], game=True, cursor=self.ply, cursor_moment=moment["id"] if moment else None)
        if self.ply in self.score_items:
            self.moves.setCurrentItem(*self.score_items[self.ply])
        self._select_moment()
        self._render()

    def _select_moment(self):
        if self.current in self.moments:
            row = self.moments.index(self.current)
            self.moment_table.selectRow(row)
            self.moment_table.scrollToItem(self.moment_table.item(row, 0))
        else:
            self.moment_table.clearSelection()

    def _at_ply(self):
        for delta in (1, 0):
            found = next((m for m in self.moments if m["ply"] == self.ply + delta), None)
            if found is not None:
                return found
        return None

    def _phase(self, after: bool):
        if self.move_ply is not None:
            self.go(self.move_ply - int(not after), self.current, move_ply=self.move_ply)

    def leave_variation(self):
        self.go(self.ply, self.current, move_ply=self.move_ply)

    def advance(self, delta: int):
        if self.branch:
            self.step = min(len(self.branch), max(0, self.step + delta))
            self._render()
        else:
            self.go(self.ply + delta)

    def jump(self, direction):
        if not self.moments:
            return
        points = [m["ply"] - 1 for m in self.moments]
        if self.current in self.moments:
            index = (self.moments.index(self.current) + direction) % len(self.moments)
        elif direction > 0:
            index = next((i for i, point in enumerate(points) if point >= self.ply), 0)
        else:
            index = next((i for i in reversed(range(len(points))) if points[i] <= self.ply), len(points) - 1)
        self.go(points[index], self.moments[index])

    def _score_clicked(self, item, column):
        target = item.data(column, Qt.ItemDataRole.UserRole)
        if column == 0:
            target = item.data(1, Qt.ItemDataRole.UserRole) or item.data(3, Qt.ItemDataRole.UserRole)
        if target is None:
            return
        ply, pv = target
        self.go(ply, move_ply=ply or None)
        if pv:
            self.branch, self.step = list(pv), len(pv)
            self._render()

    def _moment_clicked(self, row, _):
        moment = self.moments[row]
        self.go(moment["ply"] - 1, moment)

    def _scratch(self, notation):
        if self.game and not self.branch and self.ply < len(self.game["moves"]) and notation == self.game["moves"][self.ply]:
            self.advance(1)
            return
        self.branch = [*self.branch[:self.step], notation]
        self.step = len(self.branch)
        self._render()

    def _play_variation(self, row, _):
        if row < len(self.lines):
            prefix = self.branch[:self.step] if self.live_button.isChecked() else []
            self.branch, self.step = [*prefix, *self.lines[row].pv], len(prefix) + 1
            self._render()

    def _preview(self, item):
        row = item.row()
        if row < len(self.lines) and self.lines[row].pv:
            self.variation_preview.show_line(row, self.lines_board, self.lines[row].pv, self.board.flip, QCursor.pos())

    def eventFilter(self, watched, event):
        if watched == self.line_table.viewport():
            kind = event.type()
            popup = self.variation_preview
            if kind == QEvent.Type.Leave:
                popup.dismiss()
            elif kind == QEvent.Type.MouseMove and popup.isVisible():
                if self.line_table.indexAt(event.position().toPoint()).isValid():
                    popup.move_near(event.globalPosition().toPoint())
                else:
                    popup.dismiss()
            elif kind == QEvent.Type.Wheel and popup.isVisible() and popup.scroll(event):
                return True
            elif kind == QEvent.Type.ToolTip and popup.isVisible():
                return True
        return super().eventFilter(watched, event)

    def hideEvent(self, event):
        self.variation_preview.dismiss()
        self._clear_live()
        self._clear_comparison()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._render()

    def _review(self, name, game):
        item = self.game if game else self.current
        if item is not None:
            target = item["scan_game"] if game else item["id"]
            self.library.review(target, game=game, **{name: not item[name]})
            if game:
                self.game = self.library.game(target)
                self._render()
            else:
                self.refresh()

    def _policy(self):
        if not self.current:
            return
        after = self.ply == self.current["ply"]
        policy = self.library.policy(self.current["after_policy" if after else "before_policy"])
        if not policy:
            return
        board = self._root()
        observed = self.library.frequency(position_key(board))
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Maia legal moves · {self.current['maia_elo']}")
        dialog.resize(460, 470)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("Maia predicts choices at the selected rating. Observed counts come from analyzed games.", "muted", wrap=True))
        widget = table(["Move", "Maia probability", "Observed"])
        widget.setRowCount(len(policy))
        for row, (move, probability) in enumerate(sorted(policy.items(), key=lambda entry: entry[1], reverse=True)):
            for col, value in enumerate((board.san(chess.Move.from_uci(move)), _prob(probability), observed.get(move, 0))):
                widget.setItem(row, col, cell(value))
        layout.addWidget(widget)
        layout.addWidget(button("Close", dialog.accept))
        dialog.exec()

    def _occurrences(self):
        board = self._root()
        position = position_key(board)
        frequency = self.library.frequency(position)
        games = self.library.position_games(position)
        dialog = QDialog(self)
        dialog.setWindowTitle("Position occurrences")
        dialog.resize(630, 385)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(f"{sum(frequency.values()):,} observed plies in analyzed games · showing up to 50 source occurrences.", "muted", wrap=True))
        widget = table(["White", "Black", "Played", "Observed", "Date", "Ply"])
        widget.setRowCount(len(games))
        for row, item in enumerate(games):
            values = (item["white"], item["black"], board.san(chess.Move.from_uci(item["played"])),
                      frequency[item["played"]], item["day"], item["ply"])
            for col, value in enumerate(values):
                widget.setItem(row, col, cell(value))
        widget.cellDoubleClicked.connect(lambda row, _: (
            dialog.accept(), self.position_opened.emit(games[row]["scan_game"], games[row]["ply"] - 1),
        ))
        layout.addWidget(widget)
        layout.addWidget(button("Close", dialog.accept))
        dialog.exec()

    def _open_source(self):
        if self.game and (self.game["site"] or "").startswith("https://lichess.org/"):
            url = QUrl(self.game["site"])
            if self.ply:
                url.setFragment(str(self.ply))
            QDesktopServices.openUrl(url)

    def _export(self):
        if self.export_task and self.export_task.isRunning():
            return
        targets = {"games": [self.game["scan_game"]]}
        if self.current:
            targets["moments"] = [self.current["id"]]
        self.export_task = export_ui(self, self.library, targets, self.info.emit)
        if self.export_task:
            self.export_task.finished.connect(lambda: setattr(self, "export_task", None))
