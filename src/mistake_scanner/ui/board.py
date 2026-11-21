from __future__ import annotations

import math
from pathlib import Path

import chess
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QInputDialog, QSizePolicy, QToolTip, QWidget

from ..model import Score
from .theme import ACCENT, AMBER, BG, BORDER, MINT, MUTED, SURFACE
from .wheel import MoveWheel

LIGHT = QColor("#f0d9b5")
DARK = QColor("#b58863")
LAST_LIGHT = QColor("#f5e67a")
LAST_DARK = QColor("#ceba56")
PIECE_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "merida"
CLOCK_HEIGHT = 30


def clock_text(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    hours, seconds = divmod(int(seconds), 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class BoardWidget(QWidget):
    move_played = Signal(str)
    steps_requested = Signal(int)

    def __init__(self, parent=None, *, miniature=False):
        super().__init__(parent)
        self.miniature = miniature
        self.move_wheel = MoveWheel()
        self.clocks: dict[chess.Color, float | None] | None = None
        self.board = chess.Board()
        self.flip = False
        self.selected = None
        self.press = None
        self.right = None
        self.score = None
        self.hint = None
        self.arrows = set()
        self.setMouseTracking(True)
        if not miniature:
            self.setMinimumSize(322, 288)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Variation board" if miniature else "Interactive chessboard")
        self.pieces = {}
        for color in (chess.WHITE, chess.BLACK):
            for kind in chess.PIECE_TYPES:
                filename = ("w" if color else "b") + chess.piece_symbol(kind).upper() + ".svg"
                self.pieces[color, kind] = QSvgRenderer(str(PIECE_ASSETS / filename), self)

    def sizeHint(self):
        return QSize(602, 568)

    def set_position(self, board: chess.Board, score: Score | None, hint: str | None = None,
                     *, clocks: dict[chess.Color, float | None] | None = None):
        if (self.board.fen() != board.fen() or self.board.move_stack != board.move_stack):
            self.selected = None
            self.arrows.clear()
        self.board, self.score, self.hint, self.clocks = board, score, hint, clocks
        self.update()

    def toggle(self):
        self.flip = not self.flip
        self.selected = None
        self.update()

    def _geometry(self) -> tuple[QRectF, float]:
        gutter = 0 if self.miniature else 34
        reserved = 2 * CLOCK_HEIGHT if self.clocks is not None else 0
        length = max(0, min(self.width() - gutter, self.height() - reserved)) // 8 * 8
        return QRectF((self.width() - length - gutter) / 2, (self.height() - length) / 2,
                      length, length), length / 8

    def _square(self, square: chess.Square, rect: QRectF, block: float) -> QRectF:
        file, rank = chess.square_file(square), chess.square_rank(square)
        return QRectF(rect.x() + (7 - file if self.flip else file) * block,
                      rect.y() + (rank if self.flip else 7 - rank) * block, block, block)

    def _at(self, point: QPointF):
        rect, block = self._geometry()
        if not rect.contains(point) or block <= 0:
            return None
        file = min(7, int((point.x() - rect.x()) / block))
        rank = min(7, int((point.y() - rect.y()) / block))
        return chess.square(7 - file if self.flip else file, rank if self.flip else 7 - rank)

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect, block = self._geometry()
        if block <= 0:
            return
        last = self.board.peek() if self.board.move_stack else None
        targets = {move.to_square for move in self.board.legal_moves if move.from_square == self.selected}
        coords = QFont(self.font())
        coords.setPixelSize(max(11, int(block * 0.18)))
        coords.setBold(True)
        edge_rank, edge_file = (7, 7) if self.flip else (0, 0)
        for square in chess.SQUARES:
            area = self._square(square, rect, block)
            file, rank = chess.square_file(square), chess.square_rank(square)
            light = (file + rank) % 2 == 1
            color = LIGHT if light else DARK
            if last is not None and square in (last.from_square, last.to_square):
                color = LAST_LIGHT if light else LAST_DARK
            if square == self.selected:
                color = QColor("#b4c77d")
            painter.fillRect(area, color)
            painter.setFont(coords)
            painter.setPen(DARK if light else LIGHT)
            if not self.miniature and rank == edge_rank:
                painter.drawText(area.adjusted(0, 0, -4, -2),
                                 Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight, chess.FILE_NAMES[file])
            if not self.miniature and file == edge_file:
                painter.drawText(area.adjusted(4, 2, 0, 0),
                                 Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, str(rank + 1))
            piece = self.board.piece_at(square)
            if square in targets:
                target = QColor("#21382b")
                target.setAlphaF(0.38)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(target)
                painter.drawEllipse(area.center(), block * 0.125, block * 0.125)
            if piece is not None:
                self.pieces[piece.color, piece.piece_type].render(painter, area)
        for origin, target in self.arrows:
            self._arrow(painter, rect, block, origin, target, QColor("#306c52"))
        if self.hint:
            move = chess.Move.from_uci(self.hint)
            self._arrow(painter, rect, block, move.from_square, move.to_square, QColor("#ba7c48"))
        if not self.miniature:
            self._evaluation(painter, rect)
        if self.clocks is not None:
            self._paint_clocks(painter, rect)

    def _paint_clocks(self, painter, rect):
        font = QFont(self.font())
        font.setBold(True)
        font.setPixelSize(14)
        painter.setFont(font)
        colors = (chess.WHITE, chess.BLACK) if self.flip else (chess.BLACK, chess.WHITE)
        for color, y in zip(colors, (rect.top() - CLOCK_HEIGHT, rect.bottom() + 4), strict=True):
            text = clock_text(self.clocks[color])
            width = max(70, painter.fontMetrics().horizontalAdvance(text) + 20)
            badge = QRectF(rect.right() - width, y, width, CLOCK_HEIGHT - 4)
            white = color == chess.WHITE
            painter.setPen(QPen(QColor("#627b91" if color == self.board.turn else BORDER), 1))
            painter.setBrush(QColor("#eceddf" if white else "#222e3c"))
            painter.drawRoundedRect(badge, 5, 5)
            painter.setPen(QColor("#18222e" if white else "#eff1e8"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)

    def _arrow(self, painter, rect, block, origin, target, color):
        start = self._square(origin, rect, block).center()
        end = self._square(target, rect, block).center()
        dx, dy = end.x() - start.x(), end.y() - start.y()
        distance = math.hypot(dx, dy)
        if distance == 0:
            return
        direction = QPointF(dx / distance, dy / distance)
        normal = QPointF(-direction.y(), direction.x())
        tip = end + direction * (block * 0.21)
        base = tip - direction * (block * 0.33)
        color.setAlpha(205)
        painter.setPen(QPen(color, block * 0.118, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(start, base)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([tip, base + normal * block * 0.18, base - normal * block * 0.18]))

    def _evaluation(self, painter, board):
        area = QRectF(board.right() + 9, board.y(), 24, board.height())
        score = self.score
        value = 0 if score is None else score.winner * 1.0 if score.mate is not None else math.tanh((score.cp or 0) / 430)
        portion = 0.5 + value * 0.46
        if self.flip:
            portion = 1 - portion
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#18222e") if not self.flip else QColor("#e3e9d3"))
        painter.drawRoundedRect(area, 4, 4)
        bottom = QRectF(area.x(), area.bottom() - area.height() * portion, area.width(), area.height() * portion)
        painter.setBrush(QColor("#e3e9d3") if not self.flip else QColor("#18222e"))
        painter.drawRoundedRect(bottom, 3, 3)
        font = QFont(self.font())
        font.setPixelSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#1b2633") if not self.flip else QColor("#e3e9d3"))
        value_text = "—"
        if score:
            value_text = f"M{score.mate}" if score.mate is not None else f"{abs(score.cp or 0)/100:.1f}"
        painter.drawText(bottom.adjusted(0, 0, 0, -5), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter, value_text)

    def wheelEvent(self, event):
        steps = None if self.miniature else self.move_wheel.consume(event)
        if steps is None:
            event.ignore()
            return
        if steps:
            self.steps_requested.emit(steps)
        event.accept()

    def leaveEvent(self, event):
        self.move_wheel.reset()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        square = self._at(event.position())
        if event.button() == Qt.MouseButton.RightButton:
            self.right = square
        elif event.button() == Qt.MouseButton.LeftButton:
            self.press = square

    def mouseReleaseEvent(self, event):
        square = self._at(event.position())
        if event.button() == Qt.MouseButton.RightButton:
            if self.right is not None and square is not None and square != self.right:
                arrow = self.right, square
                if arrow in self.arrows:
                    self.arrows.remove(arrow)
                else:
                    self.arrows.add(arrow)
            elif square is not None:
                self.arrows.clear()
            self.right = None
            self.update()
            return
        if event.button() != Qt.MouseButton.LeftButton or square is None:
            return
        origin = self.press if self.press != square else self.selected
        options = [m for m in self.board.legal_moves if m.from_square == origin and m.to_square == square]
        if options:
            move = options[0]
            if len(options) > 1:
                pieces = [chess.piece_name(m.promotion).capitalize() for m in options]
                choice, ok = QInputDialog.getItem(self, "Promotion", "Promote to", pieces, 0, False)
                if not ok:
                    return
                move = options[pieces.index(choice)]
            self.selected = None
            self.move_played.emit(move.uci())
        elif self.board.color_at(square) == self.board.turn:
            self.selected = None if self.selected == square else square
        else:
            self.selected = None
        self.update()


class EvalGraph(QWidget):
    selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.series = {}
        self.moments = {}
        self.ply = 0
        self.length = 0
        self.setMinimumHeight(80)
        self.setMouseTracking(True)
        self.setAccessibleName("Clickable White-perspective evaluation graph")

    def sizeHint(self):
        return QSize(550, 157)

    def set_data(self, series, moments, length):
        self.series, self.moments, self.length = series, moments, length
        self.update()

    def set_ply(self, value):
        self.ply = value
        self.update()

    def _chart(self):
        return QRectF(37, 10, max(self.width() - 50, 1), max(self.height() - 36, 1))

    def _point(self, ply, score, area):
        value = score.winner * 6 if score.mate is not None else max(-6, min(6, (score.cp or 0) / 100))
        return QPointF(area.x() + ply * area.width() / max(1, self.length), area.center().y() - value * area.height() / 12)

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(SURFACE))
        area = self._chart()
        painter.setFont(QFont(self.font().family(), 8))
        for level in (3, 0, -3):
            y = area.center().y() - level * area.height() / 12
            painter.setPen(QPen(QColor("#3c5268" if level == 0 else BORDER), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            painter.setPen(QColor(MUTED))
            painter.drawText(QPointF(7, y + 4), f"{level:+d}" if level else "0")
        if not self.series:
            painter.setPen(QColor(MUTED))
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Evaluations appear after discovery")
            return
        points = [(ply, self._point(ply, data[0], area), data[1], data[2])
                  for ply, data in sorted(self.series.items())]
        shade = QPainterPath(QPointF(points[0][1].x(), area.center().y()))
        for _, point, _, _ in points:
            shade.lineTo(point)
        shade.lineTo(QPointF(points[-1][1].x(), area.center().y()))
        shade.closeSubpath()
        gradient = QLinearGradient(0, area.top(), 0, area.bottom())
        gradient.setColorAt(0, QColor("#345b64"))
        gradient.setColorAt(0.5, QColor("#1b2633"))
        gradient.setColorAt(1, QColor("#514638"))
        painter.fillPath(shade, gradient)
        for first, second in zip(points, points[1:]):
            pen = QPen(QColor(ACCENT if second[3] else MUTED if second[2] else MINT), 1.9)
            if second[2]:
                pen.setStyle(Qt.PenStyle.DotLine)
            painter.setPen(pen)
            painter.drawLine(first[1], second[1])
        selected = self._point(self.ply, Score(cp=0), area)
        painter.setPen(QPen(QColor("#7598af"), 1))
        painter.drawLine(QPointF(selected.x(), area.top()), QPointF(selected.x(), area.bottom()))
        for ply, point, _, deep in points:
            if ply in self.moments or deep or ply == self.ply:
                color = QColor(AMBER if ply in self.moments else ACCENT)
                painter.setPen(QPen(color, 1.45))
                painter.setBrush(QColor(BG) if self.moments.get(ply) == "candidate" else color)
                painter.drawEllipse(point, 3.4, 3.4)
        painter.setPen(QColor(MUTED))
        painter.drawText(QPointF(area.x(), area.bottom() + 18), "Start")
        painter.drawText(QRectF(area.right() - 80, area.bottom() + 4, 80, 18),
                         Qt.AlignmentFlag.AlignRight, f"{self.length} plies")

    def _nearest(self, x):
        area = self._chart()
        return max(0, min(self.length, round((x - area.left()) * self.length / area.width())))

    def mousePressEvent(self, event):
        if self.series and event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self._nearest(event.position().x()))

    def mouseMoveEvent(self, event):
        ply = self._nearest(event.position().x())
        data = self.series.get(ply)
        if data:
            score, depth, _, detail = data
            desc = "Detailed" if detail else "Discovery"
            QToolTip.showText(event.globalPosition().toPoint(), f"Ply {ply} • {score.display()} • {desc} d{depth}", self)
