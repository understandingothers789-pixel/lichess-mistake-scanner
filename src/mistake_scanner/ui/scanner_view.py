from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, fields
from datetime import date, timedelta
from pathlib import Path

import chess
from PySide6.QtCore import QDate, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QInputDialog, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
    QScrollArea, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)


from ..acquisition import api_sources, archive_months, archive_sources
from ..maia import (CHECKPOINT_NAME, CHECKPOINT_SHA, CHECKPOINT_SIZE, MaiaProcess,
                    download_checkpoint, install_cpu_runtime, runtime_installed)

from ..model import SPEEDS, GameFilter, SearchSettings, Source, file_digest
from ..network import Network
from .theme import Task, button, combo, error, label, spin


def bytes_text(count: int) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{count:,} B"


RULE_FIELDS = {
    "workers": ("Stockfish workers", 1, 32), "hash_mb": ("Hash per engine (MiB)", 1, 2048),
    "discovery_depth": ("Discovery depth", 1, 40), "fast_depth": ("Fast depth", 1, 40),
    "detail_depth": ("Detailed depth", 1, 40), "detail_pv": ("Detailed MultiPV", 1, 16),
    "loss_cp": ("Minimum loss (centipawns)", 1, 3000), "decisive_cp": ("Decisive threshold (centipawns)", 1, 3000),
    "decisive_plies": ("Consecutive decisive plies", 1, 100), "acceptable_cp": ("Acceptable-move tolerance (centipawns)", 1, 3000),
    "first_move": ("First eligible move number", 1, 500),
}


def edit_rules(parent, rules):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Stockfish search settings")
    form = QFormLayout(dialog)
    form.addRow(label("New scans use these settings. Each engine uses one thread.", "muted", wrap=True))
    controls = {}
    for field in fields(rules):
        value = getattr(rules, field.name)
        if field.name in RULE_FIELDS:
            name, lower, upper = RULE_FIELDS[field.name]
            widget = spin(value, lower, upper)
        else:
            name = "Assign workers to physical cores" if field.name == "affinity" else "Run below normal priority"
            widget = QCheckBox()
            widget.setChecked(value)
        form.addRow(name, widget)
        controls[field.name] = widget
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    form.addRow(buttons)
    if not dialog.exec():
        return None
    return SearchSettings(**{name: controls[name].value() if name in RULE_FIELDS else controls[name].isChecked()
                             for name in controls})


class ScannerView(QWidget):
    request_engines = Signal()
    info = Signal(str)

    def __init__(self, library, runner, root: Path, rules: SearchSettings, parent=None):
        super().__init__(parent)
        self.library, self.runner, self.root, self.rules = library, runner, root, rules
        self.files = []
        self.samples = deque()
        self.sample_id = None
        self.task = None
        self.model_task = None
        self.scan_snapshot = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 12)
        outer.setSpacing(10)
        title = QHBoxLayout()
        title.addWidget(label("Scanner", "title"), 1)
        title.addWidget(button("Search settings", self._rules))
        title.addWidget(button("Engine Manager", self.request_engines.emit))
        outer.addLayout(title)

        split = QSplitter()
        outer.addWidget(split, 1)
        config_scroll = QScrollArea()
        config_scroll.setWidgetResizable(True)
        config_scroll.setMinimumWidth(416)
        config_column = QWidget()
        config_column.setMinimumWidth(416)
        column_layout = QVBoxLayout(config_column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.addWidget(config_scroll, 1)
        split.addWidget(config_column)
        config = QWidget()
        config_scroll.setWidget(config)
        config_layout = QVBoxLayout(config)
        config_layout.setContentsMargins(0, 0, 8, 0)
        config_layout.setSpacing(10)

        input_frame, inputs = self._form("Game source")
        config_layout.addWidget(input_frame)
        self.source = combo([("Lichess monthly archives", "archive"), ("Lichess API", "api"),
                             ("Local PGN files", "local")])
        inputs.addRow("Source", self.source)
        self.modes = QStackedWidget()
        inputs.addRow(self.modes)
        self.source.currentIndexChanged.connect(self.modes.setCurrentIndex)

        archive = QWidget()
        self.modes.addWidget(archive)
        archive_form = QFormLayout(archive)
        archive_form.setContentsMargins(0, 0, 0, 0)
        previous = (date.today().replace(day=1) - timedelta(days=1)).isoformat()[:7]
        self.single_day = QCheckBox("Scan a single UTC day")
        archive_form.addRow(self.single_day)
        self.first_month = self._field("YYYY-MM", previous)
        self.last_month = self._field("YYYY-MM", previous)
        first_month_row = archive_form.rowCount()
        archive_form.addRow("First month", self.first_month)
        archive_form.addRow("Last month", self.last_month)
        archive_form.addRow(button("Browse available months", self._months))
        self.archive_day = QDateEdit(QDate.currentDate().addMonths(-1))
        self.archive_day.setDisplayFormat("yyyy-MM-dd")
        self.archive_day.setCalendarPopup(True)
        day_row = archive_form.rowCount()
        archive_form.addRow("UTC day", self.archive_day)

        def show_day(checked):
            for row in range(first_month_row, day_row):
                archive_form.setRowVisible(row, not checked)
            archive_form.setRowVisible(day_row, checked)

        self.single_day.toggled.connect(show_day)
        show_day(False)
        self.cache_mode = QCheckBox("Download to managed archive cache")
        archive_form.addRow(self.cache_mode)
        self.cache_location = self._field("Cache directory", str(self.root / "cache"))
        browse = QHBoxLayout()
        browse.addWidget(self.cache_location, 1)
        browse.addWidget(button("Browse…", self._pick_cache))
        archive_form.addRow("Cache directory", browse)
        self.cache_limit = spin(int(self.library.setting("archive_cache_gb", 1)), 1, 8192)
        self.cache_limit.setSuffix(" GB")
        archive_form.addRow("Cache limit", self.cache_limit)
        self.delete_cache = QCheckBox("Delete this archive after intake")
        archive_form.addRow(self.delete_cache)
        archive_form.addRow(label("Streaming transfers compressed data without keeping an archive on disk. "
                          "A day filter still reads the monthly stream. After interruption, the scanner may replay earlier records.", "muted", wrap=True))
        self.cache_mode.toggled.connect(self._toggle_cache)
        self._toggle_cache(False)

        api = QWidget()
        self.modes.addWidget(api)
        form = QFormLayout(api)
        form.setContentsMargins(0, 0, 0, 0)
        self.api_mode = combo([("Username", "user"), ("Game IDs", "ids"), ("Game URL", "url")])
        self.api_value = self._field("Lichess username")
        self.api_opponent = self._field("Optional opponent")
        self.api_color = combo([("Either color", ""), ("White", "white"), ("Black", "black")])
        self.api_max = spin(0, 0, 1_000_000, "Server default")
        self.api_mode.currentIndexChanged.connect(self._api_mode)
        form.addRow("Export", self.api_mode)
        form.addRow("Username / IDs / URL", self.api_value)
        form.addRow("Opponent", self.api_opponent)
        form.addRow("Player color", self.api_color)
        form.addRow("API export limit", self.api_max)
        form.addRow(label("Username exports use Lichess's supported filters. All sources also use the local filters below.", "muted", wrap=True))

        local = QWidget()
        self.modes.addWidget(local)
        layout = QVBoxLayout(local)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(button("Choose PGN or PGN.ZST files…", self._pick_local))
        self.local_files = label("No files selected", "muted", wrap=True)
        layout.addWidget(self.local_files)

        filter_frame, filters = self._form("Game filters")
        config_layout.addWidget(filter_frame)
        self.rating_min = spin(0, 0, 4000, "Any")
        self.rating_max = spin(0, 0, 4000, "Any")
        ratings = QHBoxLayout()
        ratings.addWidget(self.rating_min)
        ratings.addWidget(label("to", "muted"))
        ratings.addWidget(self.rating_max)
        filters.addRow("Rating range", ratings)
        self.rating_mode = combo([("Both players", "both"), ("Either player", "either")])
        filters.addRow("Rating applies to", self.rating_mode)
        speed = QHBoxLayout()
        self.speeds = {}
        for kind in SPEEDS:
            self.speeds[kind] = QCheckBox(kind.capitalize())
            speed.addWidget(self.speeds[kind])
        filters.addRow("Time categories", speed)
        self.rated = combo([("Any", None), ("Rated", True), ("Casual", False)])
        filters.addRow("Rated status", self.rated)
        self.tc = self._field("e.g. 600+5")
        self.since = self._field("YYYY-MM-DD")
        self.until = self._field("YYYY-MM-DD")
        self.user = self._field("Any player")
        self.opening = self._field("Any opening")
        self.eco = self._field("e.g. C20")
        self.clocks = QCheckBox("Require clock annotations")
        self.game_limit = spin(250, 0, 100_000_000, "Unlimited")
        for title, control in (("Exact time control", self.tc), ("Earliest game date", self.since),
                               ("Latest game date", self.until), ("Player username", self.user),
                               ("Opening contains", self.opening), ("ECO code", self.eco),
                               ("Game limit", self.game_limit)):
            filters.addRow(title, control)

        def show_filter_dates():
            visible = self.source.currentData() != "archive" or not self.single_day.isChecked()
            filters.setRowVisible(self.since, visible)
            filters.setRowVisible(self.until, visible)

        self.single_day.toggled.connect(show_filter_dates)
        self.source.currentIndexChanged.connect(show_filter_dates)
        filters.addRow(self.clocks)
        self.start_button = button("Start scan", self.start_scan, role="primary")
        column_layout.addWidget(self.start_button)
        config_layout.addStretch()

        progress_scroll = QScrollArea()
        progress_scroll.setWidgetResizable(True)
        split.addWidget(progress_scroll)
        progress_widget = QWidget()
        progress_scroll.setWidget(progress_widget)
        progress_layout = QVBoxLayout(progress_widget)
        progress_layout.setContentsMargins(3, 0, 0, 0)
        progress_layout.setSpacing(10)
        split.setSizes([460, 790])

        progress_card = QFrame()
        progress_card.setObjectName("card")
        progress = QVBoxLayout(progress_card)
        progress.setContentsMargins(13, 10, 13, 12)
        progress.setSpacing(8)
        toolbar = QHBoxLayout()
        toolbar.addWidget(label("Scan progress", "section"))
        self.active_scan = combo([])
        toolbar.addWidget(self.active_scan, 1)
        progress.addLayout(toolbar)
        self.status = label("No scans yet", "label")
        self.engine_label = label("", "muted", wrap=True)
        progress.addWidget(self.status)
        progress.addWidget(self.engine_label)
        stats = QGridLayout()
        self.metrics = {}
        for index, name in enumerate(("Records read", "Games selected", "Games analyzed",
                                      "Candidates", "Verified mistakes", "Maia complete")):
            card = QFrame()
            card.setObjectName("subtle")
            vbox = QVBoxLayout(card)
            vbox.setContentsMargins(11, 7, 11, 7)
            vbox.setSpacing(2)
            value = label("0", "number")
            vbox.addWidget(value)
            vbox.addWidget(label(name, "muted"))
            stats.addWidget(card, index // 3, index % 3)
            self.metrics[name] = value
        progress.addLayout(stats)
        self.stockfish_status = label("Stockfish · Waiting for a scan", "label", wrap=True)
        self.maia_progress = label("Maia · Waiting for a scan", "label", wrap=True)
        self.maia_progress.setToolTip(
            "Waiting jobs are queued. An active job is counted separately. "
            "Pausing stops new jobs from starting while current Stockfish work can finish."
        )
        self.rate = label("", "muted")
        progress.addWidget(self.stockfish_status)
        progress.addWidget(self.maia_progress)
        progress.addWidget(self.rate)
        self.transfer_label = label("Source intake · Waiting for a game source", "muted", wrap=True)
        progress.addWidget(self.transfer_label)
        self.transfer_bar = QProgressBar()
        self.transfer_bar.setAccessibleName("Source intake progress")
        self.transfer_bar.setToolTip("Source intake progress")
        self.transfer_bar.setRange(0, 1000)
        self.transfer_bar.setTextVisible(False)
        self.transfer_bar.setFixedHeight(9)
        progress.addWidget(self.transfer_bar)
        control = QHBoxLayout()
        self.pause = button("Pause", lambda: self._state("paused"))
        self.resume = button("Resume / retry", lambda: self._state("running"))
        self.cancel = button("Cancel", lambda: self._state("canceled"))
        for item in (self.pause, self.resume, self.cancel):
            control.addWidget(item)
        control.addStretch()
        progress.addLayout(control)
        self.recent_errors = QPlainTextEdit()
        self.recent_errors.setReadOnly(True)
        self.recent_errors.setMaximumHeight(100)
        self.recent_errors.setPlaceholderText("Source and job errors appear here")
        progress.addWidget(self.recent_errors)
        progress_layout.addWidget(progress_card)
        self.active_scan.currentIndexChanged.connect(self.refresh)

        maia_card, maia_form = self._form("Maia 3 · 79M")
        self.model_status = label("", "muted", wrap=True)
        maia_form.addRow(self.model_status)
        self.model_location = label("", "muted", wrap=True)
        self.model_location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        maia_form.addRow(self.model_location)
        buttons = QHBoxLayout()
        self.download_model = button("Download model", self._maia_download)
        self.choose_model = button("Choose checkpoint…", self._maia_choose)
        buttons.addWidget(self.download_model)
        buttons.addWidget(self.choose_model)
        buttons.addWidget(button("Open model folder", self._open_model_folder))
        buttons.addStretch()
        maia_form.addRow(buttons)
        progress_layout.addWidget(maia_card)
        progress_layout.addStretch()
        self.refresh()

    def _form(self, name):
        card = QFrame()
        card.setObjectName("card")
        layout = QFormLayout(card)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setVerticalSpacing(7)
        layout.addRow(label(name, "section"))
        return card, layout

    def _field(self, placeholder, value=""):
        widget = QLineEdit(value)
        widget.setPlaceholderText(placeholder)
        return widget

    def _rules(self):
        try:
            settings = edit_rules(self, self.rules)
            if settings is not None:
                self.runner.resize(settings.workers)
                self.rules = settings
                self.library.set_setting("rules", asdict(settings))
                self.info.emit("Search settings saved for new scans.")
        except ValueError as exc:
            error(self, "Search settings", str(exc))

    def _toggle_cache(self, checked):
        for widget in (self.cache_location, self.delete_cache):
            widget.setEnabled(checked)

    def _pick_cache(self):
        if not self.cache_mode.isChecked():
            self.cache_mode.setChecked(True)
        chosen = QFileDialog.getExistingDirectory(self, "Choose archive cache folder", self.cache_location.text())
        if chosen:
            self.cache_location.setText(chosen)

    def _api_mode(self):
        mode = self.api_mode.currentData()
        self.api_value.setPlaceholderText({"user": "Lichess username", "ids": "Comma-separated game IDs", "url": "https://lichess.org/…"}[mode])
        for field in (self.api_opponent, self.api_color, self.api_max):
            field.setEnabled(mode == "user")

    def _pick_local(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose PGN files", "", "Game files (*.pgn *.pgn.zst)")
        if paths:
            self.files = paths
            extra = f" (+{len(paths)-2} more)" if len(paths) > 2 else ""
            self.local_files.setText(", ".join(Path(path).name for path in paths[:2]) + extra)
            self.local_files.setToolTip("\n".join(paths))

    def _launch(self, fn, receive, title):
        if self.task and self.task.isRunning():
            return
        self.start_button.setEnabled(False)
        self.task = Task(fn, self)
        self.task.succeeded.connect(receive)
        self.task.failed.connect(lambda message: error(self, title, message))
        self.task.finished.connect(self._task_done)
        self.task.start()

    def _task_done(self):
        self.start_button.setEnabled(True)
        self.task.deleteLater()
        self.task = None

    def _months(self):
        def load(_):
            network = Network()
            try:
                return archive_months(network)
            finally:
                network.close()

        def show(months):
            if not months:
                error(self, "Lichess archives", "No monthly standard-game archives were listed.")
                return
            first, ok = QInputDialog.getItem(self, "Archive months", "First month", months, 0, False)
            if ok:
                last, ok = QInputDialog.getItem(self, "Archive months", "Last month", [m for m in months if m >= first], 0, False)
                if ok:
                    self.first_month.setText(first)
                    self.last_month.setText(last)

        self._launch(load, show, "Lichess archives")

    def start_scan(self):
        profile = self.library.engine(self.library.setting("active_engine"))
        if profile is None:
            error(self, "Select Stockfish", "Open Engine Manager to install a development build or select a local executable.")
            self.request_engines.emit()
            return
        try:
            source = self.source.currentData()
            day = self.archive_day.date().toString("yyyy-MM-dd") if source == "archive" and self.single_day.isChecked() else ""
            filters = GameFilter(
                self.rating_min.value() or None, self.rating_max.value() or None, self.rating_mode.currentData(),
                tuple(name for name, box in self.speeds.items() if box.isChecked()), self.rated.currentData(),
                self.tc.text().strip(), day or self.since.text().strip(), day or self.until.text().strip(), self.opening.text().strip(),
                self.eco.text().strip(), self.user.text().strip(), self.clocks.isChecked(),
            )
            rules, limit = self.rules, self.game_limit.value()

            def create(sources, name):
                identity = self.library.create_scan(name, sources, filters, rules, profile, limit)
                self.refresh()
                self.active_scan.setCurrentIndex(self.active_scan.findData(identity))
                self.runner.wakeup.set()
                self.info.emit(f"Started scan {identity} with {profile['name']}.")

            if source == "archive":
                first, last = self.first_month.text().strip(), self.last_month.text().strip()
                cache = Path(self.cache_location.text()) if self.cache_mode.isChecked() else None
                if day:
                    first = last = day[:7]
                self.library.set_setting("archive_cache_gb", self.cache_limit.value())
                size = self.cache_limit.value() * 1000 ** 3
                delete = self.delete_cache.isChecked()

                def planned(_):
                    network = Network()
                    try:
                        return archive_sources(network, first, last, cache=cache, limit_bytes=size, delete_after=delete)
                    finally:
                        network.close()

                self.info.emit("Checking Lichess's archive list and official checksums…")
                self._launch(planned, lambda value: create(value, day or (first if first == last else f"{first}–{last}")), "Start archive scan")
            elif source == "api":
                mode, value = self.api_mode.currentData(), self.api_value.text().strip()
                sources = api_sources(mode, value, filters, vs=self.api_opponent.text(),
                                      color=self.api_color.currentData(), max=self.api_max.value())
                create(sources, f"Lichess · {value}" if mode == "user" else "Lichess game export")
            else:
                if not self.files:
                    raise ValueError("Choose at least one PGN or PGN.ZST file.")
                local = [Source.local(Path(path)) for path in self.files]
                create(local, Path(self.files[0]).name if len(self.files) == 1 else f"{len(self.files)} PGN files")
        except (OSError, ValueError) as exc:
            error(self, "Start scan", str(exc))

    def _state(self, state):
        identity = self.active_scan.currentData()
        if identity is None:
            return
        if state == "canceled":
            answer = QMessageBox.question(self, "Cancel scan?", "Cancel remaining work? Completed analysis stays in the library.")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.library.set_state(identity, state)
        self.runner.wakeup.set()
        self.refresh()

    def _model_path(self) -> Path:
        return self.runner.checkpoint or (self.root / "models" / CHECKPOINT_NAME)

    def _open_model_folder(self):
        folder = self._model_path().parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                raise OSError(f"Could not open {folder}")
        except OSError as exc:
            error(self, "Maia model folder", str(exc))

    def _maia_download(self):
        checkpoint = self.root / "models" / CHECKPOINT_NAME

        def prepare(task):
            network = Network()
            try:
                return download_checkpoint(checkpoint, network, task.transfer.emit)
            finally:
                network.close()

        self._load_maia(prepare)

    def _maia_choose(self):
        name, _ = QFileDialog.getOpenFileName(
            self, "Choose the Maia 3 79M checkpoint", str(self._model_path()), "Checkpoint (*.pt)",
        )
        if not name:
            return

        def verify(_):
            path = Path(name).resolve()
            if path.stat().st_size != CHECKPOINT_SIZE or file_digest(path) != CHECKPOINT_SHA:
                raise ValueError("This is not the verified official Maia 3 79M checkpoint.")
            return path

        self._load_maia(verify)

    def _load_maia(self, fn):
        if self.model_task and self.model_task.isRunning():
            return
        def prepare(task):
            path = fn(task)
            if not runtime_installed():
                install_cpu_runtime(task.message.emit)
            task.message.emit("Checking local Maia inference…")
            worker = MaiaProcess(path)
            try:
                worker.predict(chess.Board(), 1800, 1800)
            finally:
                worker.close()
            return path

        task = self.model_task = Task(prepare, self)
        task.message.connect(self.model_status.setText)
        self.model_status.setText("Preparing Maia checkpoint…")
        task.transfer.connect(lambda n, total: self.model_status.setText(f"Downloading Maia · {bytes_text(n)} / {bytes_text(total)}"))
        task.failed.connect(lambda detail: error(self, "Maia setup", detail))

        def set_path(path):
            self.library.set_setting("maia_checkpoint", str(path))
            self.runner.maia_installed = True
            self.runner.set_checkpoint(path)
            self.info.emit("Maia 3 is ready. Queued predictions will run when the scan is active.")

        def done():
            self.model_task = None
            task.deleteLater()
            self.refresh()

        task.succeeded.connect(set_path)
        task.finished.connect(done)
        task.start()

    def refresh(self, *_):
        scans = self.library.scans()
        active = self.active_scan.currentData()
        choices = [(row["id"], row["name"], row["state"]) for row in scans]
        if choices != self.scan_snapshot:
            self.scan_snapshot = choices
            self.active_scan.blockSignals(True)
            self.active_scan.clear()
            for identity, name, state in choices:
                self.active_scan.addItem(f"#{identity} · {name} — {state.capitalize()}", identity)
            if active is not None:
                self.active_scan.setCurrentIndex(max(0, self.active_scan.findData(active)))
            self.active_scan.blockSignals(False)
            active = self.active_scan.currentData()

        if self.model_task is None:
            if self.runner.maia_ready:
                message = "Ready · one shared CPU inference process, two threads."
            elif self.runner.maia_installed:
                message = "Download the official 301 MiB checkpoint to run queued predictions."
            else:
                message = "Download or choose the official checkpoint to set up the Maia CPU runtime."
            self.model_status.setText(message)
        model_path = self._model_path()
        location = "Checkpoint" if model_path.is_file() else "Default checkpoint path"
        self.model_location.setText(f"{location} · {model_path}")
        self.download_model.setEnabled(self.model_task is None)
        self.choose_model.setEnabled(self.model_task is None)

        row = next((item for item in scans if item["id"] == active), None)
        info = self.runner.active()
        if row is None:
            for widget in (self.pause, self.resume, self.cancel):
                widget.setEnabled(False)
            return
        scan = self.library.scan_progress(row["id"])
        sources, jobs = scan["sources"], scan["jobs"]
        states = [source["state"] for source in sources]
        errors = [source["error"] for source in sources if source["state"] == "failed"]
        errors.extend(f"{job['stage'].capitalize()} — {job['error']}" for job in scan["errors"])
        if info["error"]:
            errors.append(info["error"])
        state = row["state"]
        limit_reached = row["game_limit"] and row["selected"] >= row["game_limit"]
        intake_complete = limit_reached or all(source == "done" for source in states)
        active_intake = not intake_complete and any(source in ("running", "pending") for source in states)
        engine_active = sum(jobs.get((stage, "running"), 0) for stage in ("discovery", "detail"))
        engine_waiting = sum(jobs.get((stage, "pending"), 0) for stage in ("discovery", "detail"))
        engine_failed = sum(jobs.get((stage, "failed"), 0) for stage in ("discovery", "detail"))
        self.pause.setEnabled(state == "running")
        self.cancel.setEnabled(state != "complete" and state != "canceled")
        self.resume.setEnabled(state != "complete" and (state != "running" or bool(errors)))
        description = state.capitalize()
        if state == "running" and not active_intake and not (engine_active or engine_waiting):
            if errors:
                description = "Needs attention · retry available"
            elif jobs.get(("maia", "pending"), 0) and not self.runner.maia_ready:
                description = "Stockfish complete · awaiting Maia setup"
        self.status.setText(description)
        profile = row["profile"]
        self.engine_label.setText(f"{profile['name']} · {profile.get('commit') or 'local executable'}")
        counts = [sum(source["seen"] for source in sources), *(row[key] for key in
                  ("selected", "analyzed", "candidates", "verified", "predicted"))]
        for name, count in zip(self.metrics, counts):
            self.metrics[name].setText(f"{count:,}")
        stockfish_state = self._stage_state(state, engine_active, engine_waiting, engine_failed,
                                             intake_complete, "Waiting for source intake")
        self.stockfish_status.setText(
            f"Stockfish · {stockfish_state} · {engine_active:,} active / {self.runner.capacity} workers · "
            f"{engine_waiting:,} queued · {jobs.get(('detail', 'done'), 0):,} of {row['candidates']:,} candidates searched")
        waiting = jobs.get(("maia", "pending"), 0)
        processing = jobs.get(("maia", "running"), 0)
        maia_state = self._stage_state(state, processing, waiting, jobs.get(("maia", "failed"), 0),
                                       stockfish_state == "Complete", "Waiting for Stockfish",
                                       "Waiting for checkpoint setup" if not self.runner.maia_ready else "Queued")
        self.maia_progress.setText(f"Maia · {maia_state} · {waiting:,} waiting · {processing:,} in progress")
        self._rate(row)
        transfer = info["transfer"]
        if transfer.get("scan") == row["id"]:
            total, received = transfer["total"], transfer["received"]
            self.transfer_bar.setRange(0, 1000 if total else 0)
            self.transfer_bar.setValue(round(1000 * received / total) if total else 0)
            if received:
                amount = f" · {bytes_text(received)}" + (f" / {bytes_text(total)}" if total else "")
            else:
                amount = ""
            self.transfer_label.setText(f"Source intake · {transfer['label']}{amount}")
        else:
            pending = next((s for s in sources if s["state"] == "running"), None)
            self.transfer_bar.setRange(0, 1000)
            self.transfer_bar.setValue(1000 if intake_complete else 0)
            text = f"Reading {pending['spec']['locator']}" if pending else f"{states.count('done')}/{len(sources)} sources processed"
            if limit_reached:
                text = "Game limit reached"
            skipped = sum(s["skipped"] for s in sources)
            malformed = sum(s["malformed"] for s in sources)
            self.transfer_label.setText(f"Source intake · {text} · {skipped:,} excluded · {malformed:,} malformed")
        content = "\n".join(errors)
        if content != self.recent_errors.toPlainText():
            self.recent_errors.setPlainText(content)

    @staticmethod
    def _stage_state(state, active, waiting, failed, upstream_complete, upstream_status, queued_status="Queued"):
        if active:
            return "Finishing active work" if state in ("paused", "canceled") else "Running"
        if state == "canceled":
            return "Canceled"
        if failed:
            return "Needs attention"
        if upstream_complete and not waiting:
            return "Complete"
        if state == "paused":
            return "Paused"
        if waiting:
            return queued_status
        return upstream_status

    def _rate(self, row):
        now = time.monotonic()
        if self.sample_id != row["id"]:
            self.sample_id = row["id"]
            self.samples.clear()
        self.samples.append((now, row["analyzed"]))
        while len(self.samples) > 1 and self.samples[1][0] <= now - 60:
            self.samples.popleft()
        start, completed = self.samples[0]
        elapsed = now - start
        per_min = (row["analyzed"] - completed) * 60 / elapsed if elapsed > 1 else 0
        db_size = self.library.path.stat().st_size
        wal = self.library.path.with_name(self.library.path.name + "-wal")
        try:
            db_size += wal.stat().st_size
        except FileNotFoundError:
            pass
        self.rate.setText(f"{per_min:,.1f} games/min · {per_min * 60:,.0f} games/hour · Library {bytes_text(db_size)}")
