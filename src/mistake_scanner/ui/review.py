from __future__ import annotations

import io
from pathlib import Path

import chess
import chess.pgn
from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QDialog, QDockWidget, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QMainWindow, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..exports import san_line, write_export
from ..model import Score, Variation, board_at, position_key
from .board import BoardWidget, EvalGraph
from .theme import ACCENT, AMBER, MUTED, Task, button, cell, error, label, score_color, table

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


def _prob(value) -> str:
    if value is None:
        return "—"
    return "<0.1%" if 0 < value < 0.001 else f"{value:.1%}"


class Review(QWidget):
    reanalyze = Signal(int)
    open_library = Signal()
    open_scanner = Signal()
    info = Signal(str)

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self.library = library
        self.game = None
        self.scan = None
        self.moments = []
        self.current = None
        self.ply = 0
        self.branch = []
        self.step = 0
        self.preview = None
        self.fast_depth = 4
        self.position_info = {}
        self.last_signature = None
        self.export_task = None
        self.lines = []
        self.evaluations = {}
        self.score_items = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 12)
        outer.setSpacing(11)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.heading = label("Analysis workspace", "title")
        self.context = label("Browse your library or start a scan to add games.", "muted")
        titles.addWidget(self.heading)
        titles.addWidget(self.context)
        header.addLayout(titles, 1)
        self.open_source = button("Lichess game ↗", self._open_source)
        self.reanalysis_button = button("Reanalyze", lambda: self.reanalyze.emit(self.game["scan_game"]),
                                        tip="Analyze this game as a new scan with the selected engine")
        self.export_button = button("Export", self._export)
        self.library_button = button("Open library", self.open_library.emit)
        self.scan_button = button("Start a scan", self.open_scanner.emit, role="primary")
        for control in (self.open_source, self.reanalysis_button, self.export_button, self.library_button, self.scan_button):
            header.addWidget(control)
        outer.addLayout(header)

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
        self.board.move_played.connect(self._scratch)
        board_layout.addWidget(self.top_player)
        board_layout.addWidget(self.board, 1)
        lower = QHBoxLayout()
        lower.addWidget(self.bottom_player, 1)
        self.board_position = label("Starting position", "muted")
        lower.addWidget(self.board_position)
        board_layout.addLayout(lower)
        controls = QHBoxLayout()
        for text, callback, tip in (
            ("|‹", lambda: self.go(0), "First position"),
            ("‹", lambda: self.advance(-1), "Previous move"),
            ("›", lambda: self.advance(1), "Next move"),
            ("›|", lambda: self.go(len(self.game["moves"]) if self.game else 0), "Last position"),
        ):
            controls.addWidget(button(text, callback, tip=tip))
        controls.addWidget(button("Flip", self._flip, tip="Flip board"))
        self.return_line = button("Main line", lambda: self.go(self.ply), tip="Exit the variation and return to the game")
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
        self.graph.selected.connect(self.go)
        chart_layout.addWidget(self.graph)
        legend = label("White perspective   ·   Dotted: fast search   ·   Pale marker: detailed   ·   Amber: discovery", "muted")
        legend.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chart_layout.addWidget(legend)
        self.chart_dock = self._dock("Evaluation graph", chart_box, Qt.DockWidgetArea.BottomDockWidgetArea)

        analysis = QWidget()
        analysis.setObjectName("card")
        analysis.setMinimumWidth(380)
        analysis_layout = QVBoxLayout(analysis)
        analysis_layout.setContentsMargins(9, 7, 9, 7)
        analysis_layout.setSpacing(6)
        phase = QHBoxLayout()
        self.before = button("Before move", lambda: self._phase(False))
        self.after = button("After move", lambda: self._phase(True))
        self.before.setCheckable(True)
        self.after.setCheckable(True)
        phase.addWidget(self.before)
        phase.addWidget(self.after)
        phase.addStretch()
        self.position_label = label("", "label")
        phase.addWidget(self.position_label)
        analysis_layout.addLayout(phase)
        self.line_table = table(["Move", "Eval", "Continuation", "Depth", "Maia"])
        self.line_table.horizontalHeader().setStretchLastSection(False)
        for index, width in enumerate((78, 61, 190, 54, 68)):
            self.line_table.setColumnWidth(index, width)
        self.line_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.line_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.line_table.setMouseTracking(True)
        self.line_table.viewport().installEventFilter(self)
        self.line_table.itemEntered.connect(self._preview)
        self.line_table.cellClicked.connect(self._play_variation)
        self.line_table.setMinimumHeight(180)
        analysis_layout.addWidget(self.line_table, 1)
        self.summary = label("Open a discovery to see its full Stockfish variations.", "muted", wrap=True)
        self.maia = label("", "label", wrap=True)
        analysis_layout.addWidget(self.summary)
        analysis_layout.addWidget(self.maia)
        options = QHBoxLayout()
        self.all_policy = button("All Maia moves", self._policy)
        self.occurrences = button("Position occurrences", self._occurrences)
        options.addWidget(self.all_policy)
        options.addWidget(self.occurrences)
        options.addStretch()
        analysis_layout.addLayout(options)
        self.engine_dock = self._dock("Stockfish and Maia", analysis, Qt.DockWidgetArea.RightDockWidgetArea)

        score_box = QWidget()
        score_box.setObjectName("card")
        score_layout = QVBoxLayout(score_box)
        score_layout.setContentsMargins(7, 6, 7, 6)
        score_layout.setSpacing(4)
        self.moves = QTreeWidget()
        self.moves.setAccessibleName("Annotated PGN move list")
        self.moves.setHeaderLabels(["Turn", "Move", "Eval", "Depth", "Moment"])
        self.moves.setAlternatingRowColors(True)
        self.moves.setColumnWidth(0, 75)
        self.moves.setColumnWidth(1, 97)
        self.moves.setColumnWidth(2, 60)
        self.moves.setColumnWidth(3, 55)
        self.moves.setIndentation(11)
        self.moves.setUniformRowHeights(True)
        self.moves.setMinimumHeight(110)
        self.moves.itemClicked.connect(self._score_clicked)
        score_layout.addWidget(self.moves, 1)
        review_tools = QHBoxLayout()
        self.bookmark = button("Bookmark game", lambda: self._review("bookmarked", True))
        self.reviewed = button("Mark reviewed", lambda: self._review("reviewed", True))
        review_tools.addWidget(self.bookmark)
        review_tools.addWidget(self.reviewed)
        review_tools.addStretch()
        score_layout.addLayout(review_tools)
        self.score_dock = self._dock("Game score", score_box, Qt.DockWidgetArea.RightDockWidgetArea)
        self.workspace.splitDockWidget(self.engine_dock, self.score_dock, Qt.Orientation.Vertical)

        moments_box = QWidget()
        moments_box.setObjectName("card")
        moment_layout = QVBoxLayout(moments_box)
        moment_layout.setContentsMargins(7, 6, 7, 6)
        moment_layout.setSpacing(4)
        self.moment_table = table(["Move", "Finding", "Maia"])
        self.moment_table.setColumnWidth(0, 92)
        self.moment_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.moment_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.moment_table.cellClicked.connect(self._moment_clicked)
        self.moment_table.setMinimumHeight(80)
        moment_layout.addWidget(self.moment_table)
        tools = QHBoxLayout()
        self.bookmark_moment = button("Bookmark moment", lambda: self._review("bookmarked", False))
        self.review_moment = button("Review moment", lambda: self._review("reviewed", False))
        tools.addWidget(self.bookmark_moment)
        tools.addWidget(self.review_moment)
        tools.addStretch()
        moment_layout.addLayout(tools)
        self.moment_dock = self._dock("Interesting moments", moments_box, Qt.DockWidgetArea.RightDockWidgetArea)
        self.workspace.splitDockWidget(self.score_dock, self.moment_dock, Qt.Orientation.Vertical)
        self.workspace.resizeDocks([self.engine_dock], [634], Qt.Orientation.Horizontal)
        self.workspace.resizeDocks([self.engine_dock, self.score_dock, self.moment_dock], [324, 239, 150], Qt.Orientation.Vertical)
        self.workspace.resizeDocks([self.chart_dock], [145], Qt.Orientation.Vertical)
        self.reset_state = self.workspace.saveState()
        saved = self.library.setting("analysis_layout")
        if saved:
            from PySide6.QtCore import QByteArray

            self.workspace.restoreState(QByteArray.fromHex(saved.encode()))
        self._render()

    def _dock(self, title, content, area):
        dock = QDockWidget(title)
        dock.setObjectName(title.replace(" ", "_"))
        dock.setWidget(content)
        self.workspace.addDockWidget(area, dock)
        return dock

    def save(self):
        self.library.set_setting("analysis_layout", bytes(self.workspace.saveState().toHex()).decode())

    def load(self, scan_game: int, moment_id: int | None = None, ply: int | None = None):
        self.game = self.library.game(scan_game)
        if self.game is None:
            return
        self.scan = self.library.scan(self.game["scan_id"])
        self.fast_depth = self.scan["rules"]["fast_depth"]
        self.last_signature = None
        saved = moment_id or (self.game["cursor_moment"] if ply is None else None)
        self.current = self.library.mistake(saved) if saved else None
        self.ply = self.current["ply"] - 1 if moment_id and self.current else self.game["cursor"] if ply is None else ply
        self.branch, self.step, self.preview = [], 0, None
        profile = self.scan["profile"]
        self.heading.setText(f"{self.game['white'] or 'White'}  vs  {self.game['black'] or 'Black'}")
        metadata = [self.game["day"], (self.game["speed"] or "").capitalize(), self.game["time_control"],
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
            if self.current:
                self.current = next((m for m in moments if m["id"] == self.current["id"]), None)
            if self.current is None:
                self.current = self._at_ply()
            self._select_moment()
            self._render()

    def _score_rows(self):
        self.moves.clear()
        self.score_items = {}
        game = chess.pgn.read_game(io.StringIO(self.game["original"]))
        if not game:
            return
        moments = {m["ply"]: m for m in self.moments}
        root = QTreeWidgetItem(["", "Start", "", "", ""])
        self.moves.addTopLevelItem(root)
        self.score_items[0] = root
        root.setData(0, Qt.ItemDataRole.UserRole, (0, ()))

        def branch(node, item, anchor, pv):
            board = node.parent.board()
            score = node.eval()
            row = QTreeWidgetItem([f"{board.fullmove_number}{'.' if board.turn else '…'}", node.san(),
                                   Score.from_uci(score).display() if score else "",
                                   str(node.eval_depth() or ""), "PGN variation"])
            item.addChild(row)
            line = (*pv, node.move.uci())
            row.setData(0, Qt.ItemDataRole.UserRole, (anchor, line))
            for child in node.variations:
                branch(child, row, anchor, line)

        for ply, node in enumerate(game.mainline(), 1):
            move_board = node.parent.board()
            deep = self.position_info.get(ply)
            discovery = self.evaluations.get(ply)
            score = deep.score if deep else Score(**discovery["score"]) if discovery else None
            depth = deep.depth if deep else discovery["depth"] if discovery else None
            moment = moments.get(ply)
            state = ""
            if moment:
                state = "Candidate" if moment["state"] == "candidate" else "Missed reply" if (
                    ply - 1 in moments and moments[ply - 1]["detail"]
                    and (moments[ply - 1]["detail"].get("punishment") or {}).get("missed")
                ) else "Mistake"
            row = QTreeWidgetItem([
                f"{move_board.fullmove_number}{'.' if move_board.turn else '…'}", node.san(),
                score.display() if score else "—", (f"d{depth}" if depth else "Final") if depth is not None else "—", state,
            ])
            if score:
                row.setForeground(2, QColor(score_color(score)))
            if depth == self.fast_depth:
                row.setForeground(3, QColor(MUTED))
            if deep:
                row.setForeground(3, QColor(ACCENT))
            if moment:
                row.setForeground(4, QColor(AMBER))
            self.moves.addTopLevelItem(row)
            self.score_items[ply] = row
            row.setData(0, Qt.ItemDataRole.UserRole, (ply, ()))
            for alternative in node.parent.variations[1:]:
                branch(alternative, self.score_items[ply - 1], ply - 1, ())
        self.moves.setCurrentItem(self.score_items.get(self.ply, root))

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

    def _render(self):
        board = self._root()
        if self.branch:
            for move in self.branch[:self.step]:
                board.push_uci(move)
        if self.preview and not self.branch:
            board.push_uci(self.preview)
        moment = self.current
        deep = self.position_info.get(self.ply)
        entry = self.evaluations.get(self.ply)
        score = deep.score if deep else Score(**entry["score"]) if entry else None
        self.line_table.setRowCount(0)
        self.lines = []
        policy = None
        hint = None
        self.before.setEnabled(moment is not None)
        self.after.setEnabled(moment is not None)
        after = moment and self.ply == moment["ply"]
        self.before.setChecked(bool(moment and not after))
        self.after.setChecked(bool(after))
        self.position_label.setText("")
        self.maia.setText("")
        if moment:
            self._render_moment(moment, bool(after))
            data = moment["detail"]
            if data:
                self.lines = self.library.cached_search(data["after_key" if after else "before_key"]) or []
                policy = self.library.policy(moment["after_policy" if after else "before_policy"])
                good = data["good_after" if after else "good_before"]
                root = self._root()
                visible = [line for line in self.lines if line.pv]
                self.line_table.setRowCount(len(visible))
                for i, line in enumerate(visible):
                    move = line.pv[0]
                    continuation_board = root.copy()
                    san = continuation_board.san_and_push(chess.Move.from_uci(move))
                    continuation = san_line(continuation_board, line.pv[1:]) if len(line.pv) > 1 else "—"
                    tip = f"{san_line(root, line.pv)}\n{'Acceptable' if move in good else 'Outside tolerance'} • d{line.depth}"
                    if line.nodes is not None:
                        tip += f" • {line.nodes:,} nodes"
                    self.line_table.setItem(i, 0, cell(san + (" ✓" if move in good else ""), ACCENT if move in good else None, tip))
                    self.line_table.setItem(i, 1, cell(line.score.display(), score_color(line.score), tip))
                    self.line_table.setItem(i, 2, cell(continuation, None, tip))
                    self.line_table.setItem(i, 3, cell(f"d{line.depth}", MUTED, tip))
                    self.line_table.setItem(i, 4, cell(_prob(None if policy is None else policy.get(move)), ACCENT, tip))
                    if i == 0:
                        hint, score = move, line.score
        else:
            self.summary.setText("Select an interesting moment to inspect its before and after searches."
                                 if self.game else "Open a discovery to see its full Stockfish variations.")
        if self.branch or self.preview:
            score, hint = None, None
        self.board.set_position(board, score, hint)
        self.graph.set_ply(self.ply)
        self.all_policy.setEnabled(policy is not None and bool(policy))
        self.occurrences.setEnabled(bool(self.game))
        self.previous_moment.setEnabled(bool(self.moments))
        self.next_moment.setEnabled(bool(self.moments))
        self.return_line.setVisible(bool(self.branch))
        self.open_source.setVisible(bool(self.game and (self.game["site"] or "").startswith("https://lichess.org/")))
        self.reanalysis_button.setVisible(bool(self.game))
        self.export_button.setVisible(bool(self.game))
        self.library_button.setVisible(self.game is None)
        self.scan_button.setVisible(self.game is None)
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

    def go(self, ply: int, moment=None):
        if not self.game:
            return
        self.ply = max(0, min(ply, len(self.game["moves"])))
        if moment is None:
            moment = self._at_ply()
        self.current = moment
        self.branch, self.step, self.preview = [], 0, None
        self.library.review(self.game["scan_game"], game=True, cursor=self.ply, cursor_moment=moment["id"] if moment else None)
        if self.ply in self.score_items:
            self.moves.setCurrentItem(self.score_items[self.ply])
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
        if self.current:
            self.go(self.current["ply"] - int(not after), self.current)

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

    def _score_clicked(self, item, _):
        ply, pv = item.data(0, Qt.ItemDataRole.UserRole)
        self.go(ply)
        if pv:
            self.branch, self.step = list(pv), len(pv)
            self._render()

    def _moment_clicked(self, row, _):
        moment = self.moments[row]
        self.go(moment["ply"] - 1, moment)

    def _scratch(self, notation):
        if self.preview:
            self.branch, self.step = [self.preview], 1
        if self.game and not self.branch and self.ply < len(self.game["moves"]) and notation == self.game["moves"][self.ply]:
            self.advance(1)
            return
        self.branch = [*self.branch[:self.step], notation]
        self.step = len(self.branch)
        self.preview = None
        self._render()

    def _play_variation(self, row, _):
        if row < len(self.lines):
            self.branch, self.step, self.preview = list(self.lines[row].pv), 1, None
            self._render()

    def _preview(self, item):
        if not self.branch and item.row() < len(self.lines) and self.lines[item.row()].pv:
            self.preview = self.lines[item.row()].pv[0]
            board = self._root()
            board.push_uci(self.preview)
            self.board.set_position(board, None)

    def eventFilter(self, watched, event):
        if watched == self.line_table.viewport() and event.type() == QEvent.Type.Leave and self.preview:
            self.preview = None
            self._render()
        return super().eventFilter(watched, event)

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
        widget.cellDoubleClicked.connect(lambda row, _: (dialog.accept(), self.load(games[row]["scan_game"], ply=games[row]["ply"] - 1)))
        layout.addWidget(widget)
        layout.addWidget(button("Close", dialog.accept))
        dialog.exec()

    def _open_source(self):
        if self.game and (self.game["site"] or "").startswith("https://lichess.org/"):
            QDesktopServices.openUrl(QUrl(self.game["site"]))

    def _export(self):
        if self.export_task and self.export_task.isRunning():
            return
        targets = {"games": [self.game["scan_game"]]}
        if self.current:
            targets["moments"] = [self.current["id"]]
        self.export_task = export_ui(self, self.library, targets, self.info.emit)
        if self.export_task:
            self.export_task.finished.connect(lambda: setattr(self, "export_task", None))
