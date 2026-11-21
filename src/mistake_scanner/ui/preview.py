from __future__ import annotations

import chess
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QVBoxLayout

from .board import BoardWidget
from .theme import label
from .wheel import MoveWheel


class VariationPreview(QFrame):
    """A cursor popup whose wheel navigation leaves the main board in place."""

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAccessibleName("Variation preview")
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.board = BoardWidget(self, miniature=True)
        self.board.setFixedSize(256, 256)
        layout.addWidget(self.board)
        self.caption = label("", "label")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.caption)
        self.hint = label("Scroll ↓ forward · ↑ back", "muted")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.hint)
        self.row = None
        self.step = 0
        self.wheel = MoveWheel()
        self.root = chess.Board()
        self.moves = ()
        self.hide()

    def show_line(self, row, root, moves, flipped, cursor):
        if self.row != row:
            self.row, self.root, self.moves = row, root, moves
            self.step = 1
            self.wheel.reset()
            self.board.flip = flipped
            self._show_step()
        self.move_near(cursor)
        self.show()

    def scroll(self, event) -> bool:
        steps = self.wheel.consume(event)
        if steps is None:
            return False
        if steps:
            self.step = max(0, min(len(self.moves), self.step + steps))
            self._show_step()
        return True

    def dismiss(self):
        self.row = None
        self.hide()

    def _show_step(self):
        position = self.root.copy()
        move_text = "Start"
        for move in self.moves[:self.step]:
            turn = f"{position.fullmove_number}{'.' if position.turn else '…'}"
            move_text = f"{turn} {position.san_and_push(chess.Move.from_uci(move))}"
        self.board.set_position(position, None)
        self.caption.setText(f"{move_text}   ·   {self.step}/{len(self.moves)}")

    def move_near(self, cursor: QPoint):
        screen = QGuiApplication.screenAt(cursor) or self.parentWidget().screen()
        bounds = screen.availableGeometry()
        self.adjustSize()
        x, y = cursor.x() + 20, cursor.y() + 20
        if x + self.width() > bounds.right():
            x = cursor.x() - self.width() - 20
        if y + self.height() > bounds.bottom():
            y = cursor.y() - self.height() - 20
        self.move(max(bounds.left(), min(x, bounds.right() - self.width() + 1)),
                  max(bounds.top(), min(y, bounds.bottom() - self.height() + 1)))
