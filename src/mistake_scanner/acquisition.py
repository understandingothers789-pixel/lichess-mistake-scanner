from __future__ import annotations

import hashlib
import io
import logging
import re
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import chess
import chess.pgn
import httpx
import zstandard

from .model import GameFilter, Source, key
from .network import CHUNK, ChunkReader, IntegrityError, Network, fetch_file

ARCHIVE_ROOT = "https://database.lichess.org"
LICHESS = "https://lichess.org"
ARCHIVE_FILE = re.compile(r"lichess_db_standard_rated_(\d{4}-(?:0[1-9]|1[0-2]))\.pgn\.zst")
GAME_ID = re.compile(r"[A-Za-z0-9]{8}")
USER = re.compile(r"[A-Za-z0-9_-]{1,40}")
STANDARD = {"standard", "chess", "normal", "from position"}
PGN_LIMIT = 8 * 1024 * 1024
LOG = logging.getLogger(__name__)


def archive_months(network: Network) -> list[str]:
    page = network.bytes(ARCHIVE_ROOT).decode()
    return sorted(set(ARCHIVE_FILE.findall(page)), reverse=True)


def archive_sources(
    network: Network, first: str, last: str, *, cache: Path | None = None,
    limit_bytes: int = 100 * 1024 ** 3, delete_after: bool = False,
    days: tuple[str, ...] = (), daily_limit: int = 0,
) -> list[Source]:
    start, end = date.fromisoformat(first + "-01"), date.fromisoformat(last + "-01")
    if start > end:
        raise ValueError("First archive month is after the last month.")
    months = [m for m in reversed(archive_months(network)) if first <= m <= last]
    days_by_month: dict[str, list[str]] = {}
    for day in sorted({date.fromisoformat(day).isoformat() for day in days}):
        days_by_month.setdefault(day[:7], []).append(day)
    if days_by_month:
        missing = sorted(days_by_month.keys() - set(months))
        if missing:
            raise ValueError(f"No public archive is available for {', '.join(missing)}.")
        if daily_limit < 1:
            raise ValueError("Choose a positive game limit per day.")
        months = [month for month in months if month in days_by_month]
    if not months:
        raise ValueError("No public standard-game archives match those months.")
    try:
        checksum_text = network.bytes(f"{ARCHIVE_ROOT}/standard/sha256sums.txt").decode()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404 or cache is not None:
            raise
        checksum_text = ""
    sums = {}
    for line in checksum_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[a-f0-9]{64}", parts[0]):
            sums[parts[1].lstrip("*")] = parts[0]
    sources = []
    for month in months:
        name = f"lichess_db_standard_rated_{month}.pgn.zst"
        expected = sums.get(name)
        if cache is not None and not expected:
            raise IntegrityError(f"No official checksum is available for {month}. Try streaming this archive.")
        kind = "archive_cache" if cache is not None else "archive_stream"
        params = {"month": month, "sha256": expected}
        if days_by_month:
            params.update(days=days_by_month[month], daily_limit=daily_limit)
        if cache is not None:
            params.update(cache=str(cache.expanduser().resolve() / "lichess-monthly"),
                          limit_bytes=limit_bytes, delete_after=delete_after)
        sources.append(Source(kind, f"{ARCHIVE_ROOT}/standard/{name}", params))
    return sources


def parse_game_id(value: str) -> str:
    token = value.strip()
    if GAME_ID.fullmatch(token):
        return token
    url = urlsplit(token)
    if url.scheme == "https" and url.netloc.lower() in ("lichess.org", "www.lichess.org"):
        parts = url.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "game":
            parts = parts[1:]
        if len(parts) in (1, 2) and (len(parts) == 1 or parts[1] in ("white", "black")):
            candidate = parts[0]
            if len(candidate) in (8, 12) and candidate.isalnum() and GAME_ID.fullmatch(candidate[:8]):
                return candidate[:8]
    raise ValueError("Use an eight-character Lichess game ID or a lichess.org game URL.")


def api_sources(mode: str, value: str, filters: GameFilter, **options) -> list[Source]:
    if mode == "user":
        username = value.strip()
        if not USER.fullmatch(username):
            raise ValueError("Enter a valid Lichess username.")
        query = {"ongoing": "false", "finished": "true", "sort": "dateDesc"}
        for name, condition in (("since", filters.since), ("until", filters.until)):
            if condition:
                edge = date.fromisoformat(condition)
                stamp = datetime.combine(edge, time.min, timezone.utc)
                if name == "until":
                    stamp += timedelta(days=1, milliseconds=-1)
                query[name] = int(stamp.timestamp() * 1000)
        cutoff = int(datetime.now(timezone.utc).timestamp() * 1000)
        query["until"] = min(query.get("until", cutoff), cutoff)
        if filters.rated is not None:
            query["rated"] = str(filters.rated).lower()
        if filters.speeds:
            query["perfType"] = ",".join(filters.speeds)
        opponent = str(options.get("vs", "")).strip()
        if opponent:
            if not USER.fullmatch(opponent):
                raise ValueError("Enter a valid opponent username.")
            query["vs"] = opponent
        if options.get("color") in ("white", "black"):
            query["color"] = options["color"]
        if options.get("max", 0):
            query["max"] = int(options["max"])
        return [Source("api_user", username, query)]
    if mode == "url":
        return [Source("api_game", parse_game_id(value))]
    if mode != "ids":
        raise ValueError("Unsupported Lichess API acquisition mode.")
    ids = list(dict.fromkeys(parse_game_id(part) for part in re.split(r"[\s,]+", value.strip()) if part))
    if not ids:
        raise ValueError("Enter at least one Lichess game ID.")
    return [Source("api_ids", ",".join(ids[i:i + 300])) for i in range(0, len(ids), 300)]


def _cache_budget(destination: Path, limit: int) -> int:
    directory = destination.parent
    directory.mkdir(parents=True, exist_ok=True)
    siblings = [
        p for p in directory.iterdir()
        if p != destination and p.name != destination.name + ".part"
        and ARCHIVE_FILE.fullmatch(p.name.removesuffix(".part"))
        and p.is_file() and not p.is_symlink()
    ]
    budget = limit - sum(p.stat().st_size for p in siblings)
    current = sum(p.stat().st_size for p in (destination, destination.with_name(destination.name + ".part"))
                  if p.is_file() and not p.is_symlink())
    if budget < 1 or current > budget:
        raise ValueError("The monthly cache has reached its disk limit. Increase the limit or clear older archives.")
    return budget - (destination.stat().st_size if destination.is_file() else 0)


def _cached_archive(source, network, progress) -> Path:
    filename = source.locator.rsplit("/", 1)[-1]
    if not ARCHIVE_FILE.fullmatch(filename):
        raise ValueError("Invalid monthly archive name.")
    cache = Path(source.params["cache"]) / filename
    limit = source.params["limit_bytes"]
    budget = _cache_budget(cache, limit)
    return fetch_file(network, source.locator, cache, sha256=source.params["sha256"], limit=budget,
                      progress=lambda n, total: progress(n, total, "Downloading verified archive"))


@contextmanager
def open_pgn(source: Source, network: Network, progress=lambda *_: None, check=lambda: None):
    if source.kind in ("local", "archive_cache"):
        path = Path(source.locator) if source.kind == "local" else _cached_archive(source, network, progress)
        if source.kind == "local":
            current = path.stat()
            if (current.st_size, current.st_mtime_ns) != (source.params["size"], source.params["mtime_ns"]):
                raise ValueError(f"The original PGN changed since this scan was created: {path}")
        with path.open("rb") as raw:
            compressed = path.name.lower().endswith(".zst")
            uncompressed = zstandard.ZstdDecompressor(max_window_size=128 * 1024 * 1024).stream_reader(raw) if compressed else raw
            with io.TextIOWrapper(uncompressed, encoding="utf-8-sig", errors="replace") as text:
                yield text
        return

    query = {"moves": "true", "tags": "true", "clocks": "true", "opening": "true", "evals": "false"}
    headers = {"Accept": "application/x-chess-pgn"}
    method, data = "GET", None
    compressed = source.kind == "archive_stream"
    if compressed:
        url, query, headers = source.locator, {}, {}
    elif source.kind == "api_user":
        url = f"{LICHESS}/api/games/user/{source.locator}"
        query.update(source.params)
    elif source.kind == "api_ids":
        url, method, data = f"{LICHESS}/api/games/export/_ids", "POST", source.locator
        headers["Content-Type"] = "text/plain"
    elif source.kind == "api_game":
        url = f"{LICHESS}/game/export/{source.locator}"
    else:
        raise ValueError(f"Unsupported game source: {source.kind}")

    expected = source.params.get("sha256") if compressed else None
    digest = hashlib.sha256() if expected else None
    received, complete = 0, False
    with network.stream(method, url, params=query, headers=headers, content=data) as response:
        length = response.headers.get("Content-Length", "")
        total = int(length) if length.isdigit() else None

        def chunks():
            nonlocal received, complete
            for chunk in response.iter_bytes(CHUNK if compressed else None):
                check()
                received += len(chunk)
                if digest is not None:
                    digest.update(chunk)
                progress(received, total, "Streaming archive" if compressed else "Streaming Lichess PGN")
                yield chunk
            if digest is not None and digest.hexdigest() != expected:
                raise IntegrityError("The completed archive stream did not match its official SHA-256.")
            complete = True

        with io.BufferedReader(ChunkReader(chunks())) as raw:
            stream = zstandard.ZstdDecompressor(max_window_size=128 * 1024 * 1024).stream_reader(raw) if compressed else raw
            with io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace") as text:
                yield text
                if complete and received == 0:
                    LOG.info("The game export returned no data: %s", source.locator)


def delete_cached(source: Source) -> None:
    if source.kind == "archive_cache" and source.params.get("delete_after"):
        filename = source.locator.rsplit("/", 1)[-1]
        if ARCHIVE_FILE.fullmatch(filename):
            (Path(source.params["cache"]) / filename).unlink(missing_ok=True)


def _known(value: str | None) -> str | None:
    return value if value and value not in ("?", "-", "??") else None


def metadata(headers, known_rated: bool | None = None) -> dict:
    def elo(name: str) -> int | None:
        value = headers.get(name, "")
        return int(value) if value.isascii() and value.isdecimal() and len(value) <= 5 else None

    event = headers.get("Event", "").lower()
    rated = True if re.match(r"^rated\b", event) else False if re.match(r"^casual\b", event) else None
    tc = _known(headers.get("TimeControl"))
    declared = re.match(r"^(?:rated|casual) (ultrabullet|bullet|blitz|rapid|classical|correspondence)\b", event)
    speed = declared[1] if declared else None
    if speed is None and tc and re.fullmatch(r"\d{1,7}\+\d{1,7}", tc):
        start, inc = (int(part) for part in tc.split("+"))
        duration = start + 40 * inc
        speed = next(name for ceiling, name in (
            (30, "ultrabullet"), (180, "bullet"), (480, "blitz"), (1500, "rapid"),
            (float("inf"), "classical"),
        ) if duration < ceiling)
    day = _known(headers.get("UTCDate")) or _known(headers.get("Date"))
    if day:
        try:
            day = date.fromisoformat(day.replace(".", "-")).isoformat()
        except ValueError:
            day = None
    site = _known(headers.get("Site"))
    game_id = None
    if site:
        try:
            game_id = parse_game_id(site) if "://" in site else None
        except ValueError:
            pass
    if game_id and rated is None:
        # Tournament names can replace Lichess's usual "Rated … game" Event tag.
        if any(re.fullmatch(r"[+-]?\d{1,5}", headers.get(tag, "")) for tag in ("WhiteRatingDiff", "BlackRatingDiff")):
            rated = True
    if rated is None:
        rated = known_rated
    return {
        "white": _known(headers.get("White")), "black": _known(headers.get("Black")),
        "white_elo": elo("WhiteElo"), "black_elo": elo("BlackElo"),
        "day": day, "utc_time": _known(headers.get("UTCTime")), "time_control": tc,
        "speed": speed, "rated": rated, "eco": _known(headers.get("ECO")),
        "opening": _known(headers.get("Opening")), "result": _known(headers.get("Result")),
        "site": site, "lichess_id": game_id,
    }


def matches(meta: dict, filters: GameFilter) -> bool:
    if filters.min_rating is not None or filters.max_rating is not None:
        ratings = [
            rating is not None and (filters.min_rating is None or rating >= filters.min_rating)
            and (filters.max_rating is None or rating <= filters.max_rating)
            for rating in (meta["white_elo"], meta["black_elo"])
        ]
        if not (all(ratings) if filters.rating_mode == "both" else any(ratings)):
            return False
    if filters.speeds and meta["speed"] not in filters.speeds:
        return False
    if filters.rated is not None and meta["rated"] is not filters.rated:
        return False
    for value, name in ((filters.time_control, "time_control"), (filters.eco.upper(), "eco")):
        if value and meta[name] != value:
            return False
    if filters.since and (not meta["day"] or meta["day"] < filters.since):
        return False
    if filters.until and (not meta["day"] or meta["day"] > filters.until):
        return False
    if filters.utc_start:
        started = meta.get("utc_time") or ""
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?", started):
            return False
        if not filters.utc_start <= started[:5] < filters.utc_end:
            return False
    if filters.opening and filters.opening.casefold() not in (meta["opening"] or "").casefold():
        return False
    if filters.username and filters.username.casefold() not in [(meta[name] or "").casefold() for name in ("white", "black")]:
        return False
    return True


class RecordReader:
    def __init__(self, text, check):
        self.text = text
        self.check = check
        self.lines: list[str] = []
        self.size = 0
        self.keep = True

    def reset(self, keep: bool = True):
        self.lines.clear()
        self.size = 0
        self.keep = keep

    def readline(self) -> str:
        self.check()
        line = self.text.readline(PGN_LIMIT + 1)
        if len(line) > PGN_LIMIT:
            raise ValueError("A PGN line exceeded the 8 MiB parsing limit.")
        if self.keep:
            self.size += len(line)
            if self.size > PGN_LIMIT:
                raise ValueError("A PGN record exceeded the 8 MiB parsing limit.")
            self.lines.append(line)
        return line


def records(text, filters: GameFilter, start: int = 0, check=lambda: None, *, remote=False, known_rated=None,
            days: set[str] | None = None):
    """Yield a record index and outcome for every complete imported PGN."""
    reader = RecordReader(text, check)
    index = 0

    class FilteredBuilder(chess.pgn.GameBuilder):
        has_result = False

        def end_headers(self):
            self.reason = ""
            if index <= start:
                self.reason = "replay"
            elif self.game.headers.get("Variant", "standard").lower() not in STANDARD:
                self.reason = "variant"
            else:
                meta = metadata(self.game.headers, known_rated)
                if (days is not None and meta["day"] not in days) or not matches(meta, filters):
                    self.reason = "filtered"
            if self.reason:
                reader.reset(False)
                return chess.pgn.SKIP
            return None

        def handle_error(self, error):
            self.game.errors.append(error)

        def visit_result(self, result):
            self.has_result = True
            super().visit_result(result)

        def result(self):
            return self.game, getattr(self, "reason", ""), self.has_result

    while True:
        reader.reset(keep=index >= start)
        index += 1
        entry = chess.pgn.read_game(reader, Visitor=FilteredBuilder)
        if entry is None:
            return
        game, reason, complete = entry
        if reason:
            yield index, None, reason
            continue
        if game.errors or (remote and not complete) or game.board().chess960:
            yield index, None, "malformed" if game.errors or not complete else "variant"
            continue
        nodes = list(game.mainline())
        if not nodes:
            yield index, None, "empty"
            continue
        clocks = [node.clock() for node in nodes]
        if filters.clocks and not any(clock is not None for clock in clocks):
            yield index, None, "filtered"
            continue
        meta = metadata(game.headers, known_rated)
        root = game.board().fen(en_passant="fen")
        moves = [node.move.uci() for node in nodes]
        identity = key([root, moves, meta["lichess_id"] or meta])
        yield index, {
            **meta, "id": identity, "root": root, "moves": moves,
            "clocks": clocks, "original": "".join(reader.lines).strip() + "\n",
        }, "selected"
