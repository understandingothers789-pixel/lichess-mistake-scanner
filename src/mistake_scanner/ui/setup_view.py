from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QGridLayout, QHBoxLayout, QVBoxLayout

from ..acquisition import ARCHIVE_FILE
from ..maia import CHECKPOINT_NAME
from .scanner_view import bytes_text
from .theme import Task, button, error, label


def _file_bytes(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _folder_bytes(path: Path) -> int:
    return sum(_file_bytes(Path(folder) / name) for folder, _, files in os.walk(path)
               for name in files if not (Path(folder) / name).is_symlink())


class SetupDialog(QDialog):
    manage_engines = Signal()
    validate_engine = Signal()
    setup_maia = Signal(bool)

    def __init__(self, library, runner, root: Path, cache_dir: Path, parent):
        super().__init__(parent)
        self.library, self.runner, self.root = library, runner, root
        self.cache_dir = cache_dir.expanduser().resolve() / "lichess-monthly"
        self.task = None
        self.setWindowTitle("Setup and storage")
        self.resize(755, 392)
        layout = QVBoxLayout(self)
        layout.addWidget(label("Setup and storage", "title"))
        grid = QGridLayout()
        grid.setVerticalSpacing(14)
        layout.addLayout(grid)
        self.statuses = {}
        self.sizes = {}

        actions = (
            ("Stockfish", [button("Engine Manager", lambda: self._leave(self.manage_engines.emit)),
                           button("Validate", lambda: self._leave(self.validate_engine.emit))]),
            ("Maia", [button("Download model", lambda: self._leave(self.setup_maia.emit, True)),
                      button("Choose checkpoint…", lambda: self._leave(self.setup_maia.emit, False)),
                      button("Open folder", lambda: self._open(self.model_path.parent))]),
            ("Archive cache", [button("Open folder", lambda: self._open(self.cache_dir))]),
            ("Analysis library", [button("Open folder", lambda: self._open(library.path.parent))]),
        )
        self.validate = actions[0][1][1]
        for row, (name, controls) in enumerate(actions):
            self.statuses[name] = label("", "muted", wrap=True)
            self.sizes[name] = label("Measuring…", "label")
            grid.addWidget(label(name, "section"), row * 3, 0)
            grid.addWidget(self.sizes[name], row * 3, 1)
            grid.addWidget(self.statuses[name], row * 3 + 1, 0, 1, 2)
            tools = QHBoxLayout()
            for control in controls:
                tools.addWidget(control)
            tools.addStretch()
            grid.addLayout(tools, row * 3 + 2, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        layout.addStretch()
        footer = QHBoxLayout()
        self.refresh_button = button("Refresh sizes", self.refresh)
        footer.addWidget(self.refresh_button)
        footer.addStretch()
        footer.addWidget(button("Close", self.accept))
        layout.addLayout(footer)
        self.refresh()

    def _leave(self, action, *args):
        self.accept()
        action(*args)

    def _open(self, folder):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                raise OSError(f"Could not open {folder}.")
        except OSError as exc:
            error(self, "Open folder", exc)

    def refresh(self):
        if self.task is not None:
            return
        profile = self.library.engine(self.library.setting("active_engine"))
        engine_path = Path(profile["path"]) if profile else None
        self.validate.setEnabled(bool(engine_path and engine_path.is_file()))
        if engine_path and engine_path.is_file():
            self.statuses["Stockfish"].setText(f"Selected · {profile['name']}")
        elif profile:
            self.statuses["Stockfish"].setText(f"Executable missing · {engine_path}")
        else:
            self.statuses["Stockfish"].setText("Select or install Stockfish to scan games.")
        models = self.root / "models"
        self.model_path = self.runner.checkpoint or models / CHECKPOINT_NAME
        if self.runner.maia_ready:
            maia = "Ready"
        elif self.runner.maia_installed:
            maia = "Checkpoint needed"
        else:
            maia = "CPU runtime setup needed"
        self.statuses["Maia"].setText(f"{maia} · {self.model_path}")
        self.statuses["Analysis library"].setText(str(self.library.path))
        self.statuses["Archive cache"].setText(f"Current folder · {self.cache_dir}")
        self.refresh_button.setEnabled(False)

        library, root, cache_dir, checkpoint = self.library, self.root, self.cache_dir, self.model_path

        def measure(_):
            try:
                directories = {cache_dir, root / "cache" / "lichess-monthly", *library.archive_cache_directories()}
                cache_files = set()
                for directory in directories:
                    try:
                        cache_files.update(path.resolve() for path in directory.iterdir()
                                           if not path.is_symlink() and ARCHIVE_FILE.fullmatch(path.name.removesuffix(".part")))
                    except OSError:
                        continue
                paths = {models / CHECKPOINT_NAME, models / (CHECKPOINT_NAME + ".part"), checkpoint}
                external_engines = set()
                for engine in library.engines():
                    if not engine.get("managed_directory"):
                        external_engines.add(Path(engine["path"]).resolve())
                        external_engines.update(Path(network["path"]).resolve() for network in engine.get("networks", ())
                                                if network.get("path"))
                return {
                    "Stockfish": _folder_bytes(root / "engines"),
                    "External engines": sum(_file_bytes(path) for path in external_engines),
                    "Maia": sum(_file_bytes(path) for path in {p.resolve() for p in paths}),
                    "Archive cache": sum(_file_bytes(path) for path in cache_files),
                    "Analysis library": sum(_file_bytes(library.path.with_name(library.path.name + suffix))
                                            for suffix in ("", "-wal", "-shm")),
                }
            finally:
                library.close_thread()

        task = self.task = Task(measure, self.parent())
        task.succeeded.connect(self._show_sizes)
        task.failed.connect(self._size_error)
        task.finished.connect(self._measured)
        task.finished.connect(task.deleteLater)
        task.start()

    def _show_sizes(self, values):
        for name, widget in self.sizes.items():
            value = bytes_text(values[name])
            if name == "Stockfish":
                value += " managed"
                if values["External engines"]:
                    value += f" · {bytes_text(values['External engines'])} external"
            widget.setText(value)

    def _size_error(self, message):
        if self.isVisible():
            error(self, "Storage sizes", message)

    def _measured(self):
        self.task = None
        self.refresh_button.setEnabled(True)
