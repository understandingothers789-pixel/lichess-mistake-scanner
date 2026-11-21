from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import sys
import time
from pathlib import Path

from .acquisition import api_sources, archive_sources
from .database import Library
from .engines import EngineManager
from .maia import CHECKPOINT_NAME, download_checkpoint
from .model import SPEEDS, GameFilter, SearchSettings, Source, data_directory
from .network import Network
from .runner import Runner


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="mistake-scanner", description="Second Look chess analysis")
    parser.add_argument("--data-dir", type=Path, help="Override the separate local application data directory")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="Open the analysis workspace")
    scan = sub.add_parser("scan", help="Analyze games from a file, archive or Lichess API")
    inputs = scan.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--pgn", type=Path, nargs="+")
    inputs.add_argument("--archive", nargs=2, metavar=("FIRST_MONTH", "LAST_MONTH"))
    inputs.add_argument("--user")
    inputs.add_argument("--ids")
    inputs.add_argument("--game")
    scan.add_argument("--cache", type=Path)
    scan.add_argument("--max-cache-gib", default=100, type=int)
    scan.add_argument("--delete-cache", action="store_true")
    scan.add_argument("--engine", type=Path, help="Stockfish executable, if no engine is already selected")
    scan.add_argument("--name", default="Command line scan")
    scan.add_argument("--limit", type=int, default=250, help="Maximum selected games (0 = unlimited)")
    scan.add_argument("--workers", type=int)
    scan.add_argument("--first-move", type=int)
    scan.add_argument("--discovery-depth", type=int)
    scan.add_argument("--detail-depth", type=int)
    scan.add_argument("--fast-depth", type=int)
    scan.add_argument("--hash-mb", type=int)
    scan.add_argument("--min-rating", type=int)
    scan.add_argument("--max-rating", type=int)
    scan.add_argument("--either-rating", action="store_true")
    scan.add_argument("--speed", choices=SPEEDS, action="append", default=[])
    scan.add_argument("--rated", choices=("rated", "casual"))
    scan.add_argument("--time-control", default="")
    scan.add_argument("--since", default="")
    scan.add_argument("--until", default="")
    scan.add_argument("--opening", default="")
    scan.add_argument("--eco", default="")
    scan.add_argument("--username", default="")
    scan.add_argument("--clocks", action="store_true")
    scan.add_argument("--maia-model", type=Path)
    resume = sub.add_parser("resume", help="Resume or retry a stored scan")
    resume.add_argument("scan", type=int)
    resume.add_argument("--maia-model", type=Path)
    model = sub.add_parser("maia-download", help="Download and verify the official Maia 3 79M checkpoint")
    model.add_argument("--destination", type=Path)
    sub.add_parser("scans", help="List saved scan identities and counts")
    return parser.parse_args(argv)


def follow(library: Library, runner: Runner, scan_id: int) -> int:
    last = None
    started = time.monotonic()
    analyzed = library.scan(scan_id)["analyzed"]
    try:
        while True:
            scan = library.scan(scan_id)
            progress = library.scan_progress(scan_id)
            jobs = progress["jobs"]
            sources = progress["sources"]
            active = runner.active()
            counts = tuple(scan[name] for name in ("selected", "analyzed", "candidates", "verified", "predicted"))
            if counts != last:
                rate = (scan["analyzed"] - analyzed) * 60 / max(1, time.monotonic() - started)
                print(f"Scan {scan_id} | {counts[0]} selected | {counts[1]} analyzed | "
                      f"{counts[2]} candidates | {counts[3]} verified | {counts[4]} Maia | {rate:.1f} games/min", flush=True)
                last = counts
            engines = any(jobs.get((stage, state), 0) for stage in ("discovery", "detail") for state in ("running", "pending"))
            limited = scan["game_limit"] and scan["selected"] >= scan["game_limit"]
            intake = not limited and any(s["state"] in ("running", "pending") for s in sources)
            maia = runner.maia_ready and any(jobs.get(("maia", state), 0) for state in ("running", "pending"))
            if active["error"]:
                raise RuntimeError(active["error"])
            if scan["state"] in ("complete", "paused", "canceled") or not (engines or intake or maia):
                errors = [s["error"] for s in sources if s["state"] == "failed"]
                errors.extend(job["error"] for job in progress["errors"])
                for error in errors:
                    print(error, file=sys.stderr)
                if jobs.get(("maia", "pending"), 0) and not runner.maia_ready:
                    print("Verified Stockfish results saved. Maia is queued until the runtime and checkpoint are installed.")
                return int(bool(errors))
            time.sleep(0.5)
    except KeyboardInterrupt:
        library.set_state(scan_id, "paused")
        print(f"Paused scan {scan_id}. Run resume {scan_id} to continue.")
        return 130


def _main(args) -> int:
    root = (args.data_dir or data_directory()).resolve()
    if args.command in (None, "gui"):
        from .gui import launch

        return launch(root)

    library = Library(root / "library.sqlite3")
    if args.command == "scans":
        print(json.dumps([{k: scan[k] for k in ("id", "name", "state", "selected", "analyzed", "verified", "predicted")}
                          for scan in library.scans()], indent=2))
        return 0

    if args.command == "maia-download":
        network = Network()
        try:
            path = download_checkpoint(args.destination or root / "models" / CHECKPOINT_NAME, network)
        finally:
            network.close()
        library.set_setting("maia_checkpoint", str(path.resolve()))
        print(path)
        return 0

    checkpoint = args.maia_model or Path(library.setting("maia_checkpoint", str(root / "models" / CHECKPOINT_NAME)))
    if args.command == "resume":
        scan = library.scan(args.scan)
        if scan is None:
            raise ValueError(f"Scan {args.scan} does not exist.")
        if scan["state"] == "complete":
            print(f"Scan {args.scan} is complete.")
            return 0
        rules = SearchSettings(**scan["rules"])
        runner = Runner(library, rules, checkpoint)
        try:
            library.set_state(args.scan, "running")
            return follow(library, runner, args.scan)
        finally:
            runner.close()

    base = library.setting("rules", {})
    for option in ("workers", "first_move", "discovery_depth", "detail_depth", "fast_depth", "hash_mb"):
        if getattr(args, option) is not None:
            base[option] = getattr(args, option)
    rules = SearchSettings(**base)
    filters = GameFilter(args.min_rating, args.max_rating, "either" if args.either_rating else "both",
                         tuple(args.speed), None if args.rated is None else args.rated == "rated",
                         args.time_control, args.since, args.until, args.opening, args.eco, args.username, args.clocks)

    if args.pgn:
        sources = [Source.local(path) for path in args.pgn]
    elif args.archive:
        network = Network()
        try:
            sources = archive_sources(network, *args.archive, cache=args.cache,
                                      limit_bytes=args.max_cache_gib * 1024**3, delete_after=args.delete_cache)
        finally:
            network.close()
    else:
        mode, value = ("user", args.user) if args.user else ("ids", args.ids) if args.ids else ("url", args.game)
        sources = api_sources(mode, value, filters)

    runner = Runner(library, rules, checkpoint)
    try:
        manager = EngineManager(library, root)
        profile = manager.activate(manager.register(args.engine)) if args.engine else library.engine(library.setting("active_engine"))
        if profile is None:
            raise ValueError("Select Stockfish in the GUI or pass --engine PATH.")
        scan_id = library.create_scan(args.name, sources, filters, rules, profile, args.limit)
        return follow(library, runner, scan_id)
    finally:
        runner.close()


def main(argv=None) -> int:
    multiprocessing.freeze_support()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return _main(parse(argv))
    except Exception as exc:
        print(f"Second Look: {exc}", file=sys.stderr)
        return 1
