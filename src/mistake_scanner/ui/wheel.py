from PySide6.QtCore import Qt


class MoveWheel:
    """Accumulate vertical mouse or trackpad deltas into signed ply steps."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.remainder = self.threshold = 0

    def consume(self, event) -> int | None:
        pixels = event.pixelDelta().y()
        delta = pixels or event.angleDelta().y()
        if not delta or event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.reset()
            return None
        threshold = 40 if pixels else 120
        if threshold != self.threshold or delta * self.remainder < 0:
            self.remainder = 0
        self.threshold = threshold
        self.remainder += delta
        steps = int(self.remainder / threshold)
        self.remainder -= steps * threshold
        return -steps
