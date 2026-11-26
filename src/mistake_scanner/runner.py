from __future__ import annotations

import ctypes
import logging
import os
import struct
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import httpx
import psutil
import zstandard

from .acquisition import delete_cached, open_pgn, records
from .analysis import EngineWorkers, analyse_critical, discover
from .database import Library
from .maia import MaiaProcess, predict_mistake, round_rating, runtime_installed
from .model import GameFilter, SearchSettings, Source
from .network import Interrupted, Network

LOG = logging.getLogger(__name__)


def physical_cpus() -> list[int]:
    try:
        available = set(psutil.Process().cpu_affinity())
    except (AttributeError, psutil.Error, OSError):
        return []
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        size = ctypes.c_ulong()
        kernel.GetLogicalProcessorInformationEx(0, None, ctypes.byref(size))
        if not size.value:
            return []
        buffer = ctypes.create_string_buffer(size.value)
        if not kernel.GetLogicalProcessorInformationEx(0, buffer, ctypes.byref(size)):
            return []
        result = []
        offset = 0
        ptr_size = ctypes.sizeof(ctypes.c_size_t)
        while offset + 32 <= size.value:
            relation, length = struct.unpack_from("<II", buffer, offset)
            if length < 32 or offset + length > size.value:
                break
            groups = struct.unpack_from("<H", buffer, offset + 30)[0]
            if relation == 0 and groups == 1 and length >= 32 + ptr_size + 8:
                mask = int.from_bytes(buffer[offset + 32:offset + 32 + ptr_size], "little")
                group = struct.unpack_from("<H", buffer, offset + 32 + ptr_size)[0]
                choices = sorted(cpu for cpu in available if mask & (1 << cpu))
                if group == 0 and choices:
                    result.append(choices[0])
            offset += length
        return result
    per_core = {}
    try:
        for cpu in sorted(available):
            topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
            identity = tuple((topology / item).read_text().strip() for item in ("physical_package_id", "core_id"))
            per_core.setdefault(identity, cpu)
    except OSError:
        return []
    return list(per_core.values())


class LockFile:
    def __init__(self, path: Path):
        self.file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.file.tell() == 0:
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("The analysis library is already open in another scanner process.") from exc

    def close(self):
        self.file.close()


class Runner:
    def __init__(self, library: Library, rules: SearchSettings, checkpoint: Path | None = None,
                 *, network_factory=Network, engine_factory=None, maia_factory=MaiaProcess):
        self.library = library
        self.lock_file = LockFile(library.path.with_suffix(".lock"))
        self.library.recover()
        self.capacity = rules.workers
        self.checkpoint = checkpoint
        self.maia_installed = runtime_installed()
        self.network_factory = network_factory
        self.maia_factory = maia_factory
        self.maia = None
        self.pool = EngineWorkers(physical_cpus(), **({"factory": engine_factory} if engine_factory else {}))
        self.engines = ThreadPoolExecutor(self.capacity, "stockfish")
        self.inference = ThreadPoolExecutor(1, "maia")
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.gate = threading.RLock()
        self.futures = {}
        self.pending_live = None
        self.maintenance = False
        self.transfer = {}
        self.last_error = ""
        self.dispatcher = threading.Thread(target=self._dispatch, name="dispatcher", daemon=True)
        self.intake = threading.Thread(target=self._intake, name="acquisition", daemon=True)
        self.dispatcher.start()
        self.intake.start()

    @property
    def maia_ready(self) -> bool:
        return self.maia_installed and self.checkpoint is not None and self.checkpoint.is_file()

    def set_checkpoint(self, path: Path | None) -> None:
        with self.gate:
            self.checkpoint = path
            self.wakeup.set()

    def resize(self, workers: int) -> None:
        if workers == self.capacity:
            return
        with self.gate:
            if any(job["stage"] != "maia" and not future.done() for future, job in self.futures.items()):
                raise ValueError("Pause the active scans and wait for Stockfish before changing worker count.")
            self.engines.shutdown()
            self.pool.close()
            self.capacity = workers
            self.engines = ThreadPoolExecutor(self.capacity, "stockfish")

    def begin_maintenance(self) -> None:
        with self.gate:
            if self.maintenance:
                raise ValueError("An engine operation is already active.")
            if any(job["stage"] != "maia" and not future.done() for future, job in self.futures.items()):
                raise ValueError("Pause scans and wait for the active Stockfish games before this engine operation.")
            self.maintenance = True
            self.pool.close()

    def end_maintenance(self) -> None:
        with self.gate:
            self.maintenance = False
            self.wakeup.set()

    def analyze_position(self, profile, rules, board, *, played_move: str | None = None) -> Future:
        result = Future()
        with self.gate:
            if self.pending_live is not None:
                self.pending_live[0].cancel()
            self.pending_live = result, profile, rules, board.copy(), played_move
            self.wakeup.set()
        return result

    def _live_search(self, result, profile, rules, board, played_move):
        if not result.set_running_or_notify_cancel():
            return
        try:
            engine = self.pool.acquire(profile, rules)
            before = engine(board, rules.detail_depth, rules.detail_pv, seconds=2.0)
            if played_move is None:
                result.set_result(before)
            else:
                if not before[0].pv:
                    raise ValueError("The game ended before the selected move.")
                played = next((line for line in before if line.pv and line.pv[0] == played_move), None)
                if played is None:
                    played = engine(board, rules.detail_depth, move=played_move, seconds=2.0)[0]
                board.push_uci(played_move)
                after = engine(board, rules.detail_depth, rules.detail_pv, seconds=2.0)
                result.set_result((before, played, after))
        except Exception as exc:
            self.pool.discard()
            result.set_exception(exc)
        finally:
            self.wakeup.set()

    def active(self) -> dict:
        with self.gate:
            return {
                "workers": sum(job["stage"] != "maia" and not f.done() for f, job in self.futures.items()),
                "maia": any(job["stage"] == "maia" and not f.done() for f, job in self.futures.items()),
                "transfer": dict(self.transfer), "error": self.last_error,
            }

    def close(self) -> None:
        self.stop.set()
        with self.gate:
            if self.pending_live is not None:
                self.pending_live[0].cancel()
                self.pending_live = None
        self.wakeup.set()
        self.dispatcher.join()
        self.intake.join()
        self.engines.shutdown()
        self.inference.shutdown()
        self.pool.close()
        if self.maia is not None:
            self.maia.close()
        self.library.close_thread()
        self.lock_file.close()

    def _dispatch(self) -> None:
        prefer_detail = True
        try:
            while not self.stop.is_set():
                with self.gate:
                    self.futures = {f: job for f, job in self.futures.items() if not f.done()}
                    running = [job for job in self.futures.values() if job["stage"] != "maia"]
                    if not self.maintenance and len(running) < self.capacity:
                        slots = self.capacity - len(running)
                        if self.pending_live and not any(job["stage"] == "live" for job in running):
                            request, self.pending_live = self.pending_live, None
                            if not request[0].cancelled():
                                future = self.engines.submit(self._live_search, *request)
                                self.futures[future] = {"stage": "live"}
                                slots -= 1
                        stages = ("detail", "discovery") if prefer_detail else ("discovery", "detail")
                        for _ in range(slots):
                            job = self.library.claim(stages[0]) or self.library.claim(stages[1])
                            if job is None:
                                break
                            self.futures[self.engines.submit(self._work, job)] = job
                            prefer_detail = job["stage"] != "detail"
                            stages = ("detail", "discovery") if prefer_detail else ("discovery", "detail")
                    if self.maia_ready and not any(job["stage"] == "maia" for job in self.futures.values()):
                        job = self.library.claim("maia")
                        if job is not None:
                            self.futures[self.inference.submit(self._work, job)] = job
                self.library.settle()
                self.wakeup.wait(0.25)
                self.wakeup.clear()
        except Exception as exc:
            self.last_error = f"Dispatcher: {exc}"
            LOG.exception("Dispatcher stopped")
        finally:
            self.library.close_thread()

    def _work(self, job: dict) -> None:
        stage = job["stage"]
        engine_start_failed = False
        try:
            scan = self.library.scan(job["scan_id"])
            rules = SearchSettings(**scan["rules"])
            profile = scan["profile"]
            mistake = self.library.mistake(job["target"]) if stage != "discovery" else None
            game = self.library.game(mistake["scan_game"] if mistake is not None else job["target"])

            if self.stop.is_set():
                raise Interrupted()
            if stage == "maia":
                checkpoint = self.checkpoint
                if checkpoint is None:
                    raise Interrupted()
                if self.maia is None or checkpoint != self.maia.checkpoint:
                    if self.maia is not None:
                        self.maia.close()
                    self.maia = self.maia_factory(checkpoint)
                before, after, probability = predict_mistake(self.library, game, mistake, self.maia.predict)
                self.library.finish_maia(job, before, after, probability)
                return

            try:
                engine = self.pool.acquire(profile, rules)
            except Exception:
                engine_start_failed = True
                raise

            def search(*args, **kwargs):
                if self.stop.is_set():
                    raise Interrupted()
                return engine(*args, **kwargs)

            if stage == "discovery":
                decisions = {}
                traces, candidates, occurrences = discover(game, rules, search, decisions=decisions)
                self.library.finish_discovery(job, game, traces, candidates, occurrences, decisions)
            else:
                detail = analyse_critical(self.library, game, mistake, profile, rules, search)
                elo = round_rating(game["white_elo"], game["black_elo"])
                self.library.finish_detail(job, detail, elo)
        except Interrupted:
            self.library.release_job(job["id"])
        except Exception as exc:
            LOG.exception("Job %s (%s) failed", job["id"], stage)
            if stage != "maia":
                self.pool.discard()
            elif self.maia is not None:
                self.maia.close()
                self.maia = None
            self.library.fail_job(job["id"], f"{type(exc).__name__}: {exc}", pause_scan=engine_start_failed)
        finally:
            self.wakeup.set()

    def _intake(self) -> None:
        try:
            while not self.stop.is_set():
                source = self.library.ready_source()
                if source is None:
                    self.stop.wait(0.25)
                    continue
                self._consume(source)
        except Exception as exc:
            self.last_error = f"Acquisition: {exc}"
            LOG.exception("Acquisition stopped")
        finally:
            self.library.close_thread()

    def _consume(self, row: dict) -> None:
        source = Source(**row["spec"])
        scan_id, source_id = row["scan_id"], row["id"]
        filters = GameFilter(**self.library.scan(scan_id)["filters"])
        wanted_days = set(source.params.get("days", ()))
        daily_limit = source.params.get("daily_limit", 0)
        day_counts = self.library.scan_day_counts(scan_id) if wanted_days else {}
        remaining_days = {day for day in wanted_days if day_counts.get(day, 0) < daily_limit}
        last_check = 0.0
        scan = None

        def check(force=False):
            nonlocal last_check, scan
            if self.stop.is_set():
                raise Interrupted()
            if force or time.monotonic() - last_check > 0.25:
                last_check = time.monotonic()
                scan = self.library.intake_status(scan_id)
                if scan["state"] != "running":
                    raise Interrupted()

        def wait(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                check()
                self.stop.wait(min(0.25, max(0, end - time.monotonic())))

        def progress(received, total, label):
            check()
            self.transfer = {"scan": scan_id, "source": source.locator, "received": received, "total": total, "label": label}

        def read(start: int) -> int:
            check(True)
            skipped, malformed, index = 0, 0, start
            with open_pgn(source, network, progress, check) as text:
                for index, game, reason in records(
                    text, filters, start if source.immutable else 0, check,
                    remote=source.kind != "local", known_rated=source.rated,
                    days=remaining_days if wanted_days else None,
                ):
                    if game is None:
                        if reason != "replay" and index > start:
                            skipped += reason in ("filtered", "variant", "empty")
                            malformed += reason == "malformed"
                            if skipped + malformed >= 256:
                                self.library.checkpoint(source_id, index, skipped, malformed)
                                skipped, malformed = 0, 0
                        continue
                    # Keep bounded discovery and detail backlogs; paused scans have their own saved work.
                    while self.library.outstanding(("discovery", "detail"), scan_id) >= max(16, self.capacity * 6):
                        wait(0.25)
                    check(True)
                    if scan["game_limit"] and scan["selected"] >= scan["game_limit"]:
                        break
                    selected = self.library.import_game(source_id, scan_id, game, index, skipped, malformed)
                    skipped, malformed = 0, 0
                    self.wakeup.set()
                    if selected:
                        if wanted_days:
                            day = game["day"]
                            day_counts[day] = day_counts.get(day, 0) + 1
                            if day_counts[day] >= daily_limit:
                                remaining_days.remove(day)
                            if not remaining_days:
                                break
                        if scan["game_limit"] and scan["selected"] + 1 >= scan["game_limit"]:
                            break
            self.library.checkpoint(source_id, index, skipped, malformed)
            return index

        network = self.network_factory(wait=wait)
        start = row["seen"]
        try:
            check(True)
            for attempt in range(4):
                try:
                    if source.kind == "library":
                        game = self.library.game(int(source.locator))
                        if game is None:
                            raise ValueError("The saved source game no longer exists.")
                        self.library.import_game(source_id, scan_id, game, 1)
                    else:
                        if not wanted_days or remaining_days:
                            read(start)
                        delete_cached(source)
                    self.library.source_state(source_id, "done")
                    break
                except (httpx.TransportError, zstandard.ZstdError):
                    if source.kind == "local" or attempt == 3:
                        raise
                    start = self.library.source_cursor(source_id)
                    progress(0, None, "Connection interrupted · replaying saved records")
                    wait(network.delay(attempt))
        except Interrupted:
            self.library.source_state(source_id, "pending")
        except Exception as exc:
            LOG.exception("Source %s failed", source.locator)
            self.library.source_state(source_id, "failed", f"{type(exc).__name__}: {exc}")
        finally:
            self.transfer = {}
            network.close()
            self.wakeup.set()
