from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QSplitter, QVBoxLayout,
)

from ..engines import EngineManager, inspect_engine, latest_development
from ..network import Network
from .scanner_view import bytes_text
from .theme import ACCENT, Task, button, cell, combo, error, label, table


class EngineDialog(QDialog):
    changed = Signal()

    def __init__(self, library, runner, root, parent=None):
        super().__init__(parent)
        self.library, self.runner = library, runner
        self.manager = EngineManager(library, root)
        self.task = None
        self.revision = None
        self.profiles = []
        self.setWindowTitle("Stockfish Engine Manager")
        self.resize(930, 610)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(15, 13, 15, 12)
        outer.setSpacing(8)
        outer.addWidget(label("Stockfish Engine Manager", "title"))
        self.status = label("Select a validated engine for future scans.", "muted", wrap=True)
        outer.addWidget(self.status)
        actions = QHBoxLayout()
        self.check_updates = button("Check for updates", self._update, tip="Look up the latest official Stockfish development build.")
        self.install = button("Download dev build && activate", self._install, role="primary",
                              tip="Download an official Stockfish build, test it, and select it for new scans.")
        self.build = button("Build from source…", self._build, tip="Compile Stockfish locally using an installed MSYS2 toolchain.")
        self.add = button("Select existing executable…", self._add, tip="Choose a Stockfish program already on your computer.")
        for widget in (self.check_updates, self.install, self.build, self.add):
            actions.addWidget(widget)
        actions.addStretch()
        outer.addLayout(actions)
        self.available = label("", "label", wrap=True)
        outer.addWidget(self.available)

        split = QSplitter()
        outer.addWidget(split, 1)
        self.table = table(["Engine", "Source", "Architecture", "Commit", "Active"])
        self.table.setToolTip("Select a build to see its version, file path, and other details.")
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        for column, width in ((1, 139), (2, 132), (3, 90)):
            self.table.setColumnWidth(column, width)
        self.table.itemSelectionChanged.connect(self._selected)
        self.table.cellDoubleClicked.connect(lambda *_: self._activate())
        split.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Select an installed build to see version, commit and NNUE details.")
        split.addWidget(self.details)
        split.setSizes([570, 325])

        controls = QHBoxLayout()
        self.active = button("Use selected engine", self._activate, tip="Choose this build for new scans and live analysis.")
        self.validate = button("Test engine", self._validate, tip="Run a short Stockfish search to check that this build and its network work.")
        self.remove = button("Remove engine…", self._remove, tip="Remove the selected profile and any app-managed files for it.")
        self.show_log = button("Show activity log", self._toggle_log, tip="See details of downloads, engine tests, and builds.")
        self.show_log.setCheckable(True)
        for widget in (self.active, self.validate, self.remove, self.show_log):
            controls.addWidget(widget)
        controls.addStretch()
        outer.addLayout(controls)
        self.progress = QProgressBar()
        self.progress.setToolTip("Progress of the current engine download or build.")
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        outer.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMaximumHeight(170)
        self.log.hide()
        outer.addWidget(self.log)
        footer = QHBoxLayout()
        footer.addStretch()
        self.close_button = button("Close", self.accept, tip="Return to Second Look.")
        footer.addWidget(self.close_button)
        outer.addLayout(footer)
        self._refresh()

    def _refresh(self, identity=None):
        selected = identity or (self._profile() or {}).get("id") or self.library.setting("active_engine")
        self.profiles = self.library.engines()
        active = self.library.setting("active_engine")
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.profiles))
        for row, profile in enumerate(self.profiles):
            commit = profile.get("commit")
            values = (profile["name"], profile["origin"], profile["architecture"], commit[:8] if commit else "—",
                      "Selected" if profile["id"] == active else "")
            for col, value in enumerate(values):
                self.table.setItem(row, col, cell(value, ACCENT if profile["id"] == active and col in (0, 4) else None))
            if profile["id"] == selected:
                self.table.selectRow(row)
        if self.table.currentRow() < 0 and self.profiles:
            self.table.selectRow(0)
        self._selected()

    def _profile(self):
        row = self.table.currentRow()
        return self.profiles[row] if 0 <= row < len(self.profiles) else None

    def _selected(self):
        profile = self._profile()
        busy = self.task is not None
        for widget in (self.active, self.validate, self.remove):
            widget.setEnabled(profile is not None and not busy)
        if profile is None:
            self.details.clear()
            return
        active = profile["id"] == self.library.setting("active_engine")
        self.active.setEnabled(not busy and not active)
        blocked = self.library.engine_removal_block(profile["id"])
        self.remove.setToolTip(blocked or "Remove this profile and any app-managed files for it.")
        values = [
            ("Profile", profile["name"]), ("UCI version", profile["version"]),
            ("Commit date", profile.get("commit_date")), ("Commit SHA", profile.get("commit")),
            ("Release", profile.get("release")), ("Source", profile["origin"]),
            ("Architecture", profile["architecture"]), ("Compiler", profile.get("compiler")),
            ("Build mode", profile.get("build_mode")), ("Installed", profile["installed"]),
            ("Executable", profile["path"]), ("SHA-256", profile["sha256"]),
            ("Removal", blocked),
        ]
        text = "\n".join(f"{name}\n{value}" for name, value in values if value)
        networks = profile.get("networks", [])
        if networks:
            text += "\n\nNNUE\n" + "\n".join(f"{item['name']} · {item['source']}" for item in networks)
        if profile.get("build_log"):
            text += f"\n\nBuild log\n{profile['build_log']}"
        if profile.get("bench_nps"):
            text += f"\n\nStockfish bench\n{profile['bench_nps']:,} nodes/s"
        self.details.setPlainText(text)

    def _toggle_log(self, shown):
        self.log.setVisible(shown)
        self.show_log.setText("Hide activity log" if shown else "Show activity log")
        if shown and not self.task:
            profile = self._profile() or {}
            filename = profile.get("build_log")
            if filename and Path(filename).is_file():
                with Path(filename).open("rb") as stream:
                    stream.seek(max(0, Path(filename).stat().st_size - 65536))
                    self.log.setPlainText(stream.read(65536).decode(errors="replace"))

    def _start(self, title, fn, on_success, *, maintenance=False):
        if self.task is not None:
            return
        self.status.setText(title)
        self.progress.setRange(0, 0)

        def run(task):
            try:
                if maintenance:
                    self.runner.begin_maintenance()
                try:
                    return fn(task)
                finally:
                    if maintenance:
                        self.runner.end_maintenance()
            finally:
                self.library.close_thread()

        task = self.task = Task(run, self)
        for widget in (self.check_updates, self.install, self.build, self.add, self.close_button):
            widget.setEnabled(False)
        self._selected()
        task.succeeded.connect(on_success)
        task.failed.connect(lambda message: self._failed(title, message))
        task.message.connect(self._message)
        task.transfer.connect(self._transfer)
        task.finished.connect(self._done)
        task.start()

    def _done(self):
        self.task.deleteLater()
        self.task = None
        for widget in (self.check_updates, self.install, self.build, self.add, self.close_button):
            widget.setEnabled(True)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self._selected()

    def _failed(self, title, message):
        self.status.setText(message)
        self.log.appendPlainText(message)
        self.show_log.setChecked(True)
        error(self, title, message)

    def _message(self, message):
        self.log.appendPlainText(message)
        self.status.setText(message[:210])

    def _transfer(self, loaded, total):
        self.progress.setRange(0, 1000 if total else 0)
        if total:
            self.progress.setValue(round(1000 * loaded / total))
        self.status.setText(f"Downloading Stockfish · {bytes_text(loaded)}" + (f" / {bytes_text(total)}" if total else ""))

    def _update(self):
        def check(_):
            network = Network()
            try:
                return latest_development(network)
            finally:
                network.close()

        def show(revision):
            self.revision = revision
            installed = [p for p in self.profiles if p.get("commit")]
            installed.sort(key=lambda item: item.get("commit_date") or "", reverse=True)
            current = installed[0] if installed else None
            previous = f"Installed {current['commit'][:8]} ({current.get('commit_date', '')[:10]})" if current else "No managed development build installed"
            state = ("This revision is installed" if any(p["commit"] == revision.sha for p in installed)
                     else "Official Windows x64 download available" if revision.asset else "Compatible download unavailable · build from source")
            self.available.setText(f"{previous}  ·  Available {revision.sha[:8]} ({revision.date[:10]})  ·  {state}")
            self.available.setToolTip(f"{revision.sha}\n{revision.release_url}")
            self.status.setText("Official prerelease check complete.")

        self._start("Checking official Stockfish prereleases…", check, show)

    def _install(self):
        def install(task):
            network = Network()
            try:
                revision = latest_development(network)
                profile = self.manager.install(network, revision, task.message.emit, task.transfer.emit)
                return self.manager.activate(profile)
            finally:
                network.close()

        self._start("Preparing official development build…", install, self._used, maintenance=True)

    def _add(self):
        name, _ = QFileDialog.getOpenFileName(self, "Choose Stockfish executable", "", "Executables (*.exe);;All files (*)")
        if not name:
            return

        def register(task):
            task.message.emit("Checking Stockfish UCI and its default NNUE")
            return self.manager.activate(self.manager.register(Path(name)))

        self._start("Validating local executable…", register, self._used, maintenance=True)

    def _activate(self):
        profile = self._profile()
        if profile:
            self._start("Checking executable identity…", lambda _: self.manager.activate(profile), self._used)

    def _used(self, profile):
        self._refresh(profile["id"])
        self.status.setText(f"Selected {profile['name']} for new scans. Existing scans keep their saved revision.")
        self.changed.emit()

    def _validate(self):
        profile = self._profile()
        if profile:
            options = {item["option"]: Path(item["path"]) for item in profile.get("networks", []) if item.get("path")}

            def validate(_):
                result = inspect_engine(Path(profile["path"]), options, commit=profile.get("commit"))
                if result["sha256"] != profile["sha256"]:
                    raise ValueError("The executable has changed. Register it again as a separate engine profile.")
                return result

            self._start("Checking UCI search and NNUE…", validate,
                        lambda result: self.status.setText(f"{result['version']} passed its UCI search."), maintenance=True)

    def _remove(self):
        profile = self._profile()
        if profile is None:
            return
        if message := self.library.engine_removal_block(profile["id"]):
            error(self, "Remove engine", message)
            return
        question = "Remove this profile and its managed installation?" if profile.get("managed_directory") else "Remove this profile from Second Look?"
        if profile["id"] == self.library.setting("active_engine"):
            question += "\n\nThis is the active engine. Select or download a replacement before starting a new scan."
        if QMessageBox.question(self, "Remove engine", question) != QMessageBox.StandardButton.Yes:
            return
        def removed(_):
            self._refresh()
            self.status.setText("Engine profile removed.")
            self.changed.emit()

        self._start("Removing engine…", lambda _: self.manager.remove(profile), removed, maintenance=True)

    def _build(self):
        form = QDialog(self)
        form.setWindowTitle("Build native Stockfish")
        layout = QFormLayout(form)
        layout.addRow(label("Use Git for Windows and an MSYS2 UCRT64 or MINGW64 GCC toolchain. "
                            "The build runs after active Stockfish searches finish.", "muted", wrap=True))
        commit = QLineEdit()
        commit.setPlaceholderText("Latest official development commit")
        msys = QLineEdit(r"C:\msys64")
        target = combo([("Auto-detect native CPU", "native"), ("Portable x86-64", "x86-64"), ("AVX2", "x86-64-avx2")])
        pgo = QCheckBox("Profile-guided optimization")
        pgo.setToolTip("Run extra build steps that can improve Stockfish performance.")
        commit.setToolTip("Enter a Stockfish Git commit, or leave blank for the newest official version.")
        msys.setToolTip("Folder where MSYS2 is installed.")
        target.setToolTip("Choose which CPUs the compiled build should support.")
        pgo.setChecked(True)
        layout.addRow("Commit SHA", commit)
        layout.addRow("MSYS2 directory", msys)
        layout.addRow("CPU target", target)
        layout.addRow(pgo)
        instructions = button("Open official build instructions ↗",
                              lambda: QDesktopServices.openUrl(QUrl("https://official-stockfish.github.io/docs/stockfish-wiki/Compiling-from-source.html")))
        layout.addRow(instructions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Build & activate")
        buttons.accepted.connect(form.accept)
        buttons.rejected.connect(form.reject)
        layout.addRow(buttons)
        if not form.exec():
            return
        revision, root, optimized = commit.text().strip(), Path(msys.text()), pgo.isChecked()
        architecture = target.currentData()
        self.log.clear()
        self.show_log.setChecked(True)

        def build(task):
            network = Network()
            try:
                profile = self.manager.build(network, revision, pgo=optimized, msys=root, target=architecture,
                                             log=task.message.emit)
                return self.manager.activate(profile)
            finally:
                network.close()

        self._start("Starting native Windows build…", build, self._used, maintenance=True)

    def reject(self):
        if self.task is None:
            super().reject()

    def closeEvent(self, event):
        if self.task is not None:
            event.ignore()
        else:
            super().closeEvent(event)
