from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import chess
import chess.engine
from platformdirs import user_data_path

APP_NAME = "Second Look"
SPEEDS = ("bullet", "blitz", "rapid", "classical")


def data_directory() -> Path:
    return user_data_path("LichessMistakeScanner", appauthor=False, ensure_exists=True)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def key(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class SearchSettings:
    workers: int = 8
    hash_mb: int = 32
    discovery_depth: int = 12
    fast_depth: int = 4
    detail_depth: int = 20
    detail_pv: int = 5
    loss_cp: int = 50
    decisive_cp: int = 300
    decisive_plies: int = 6
    acceptable_cp: int = 50
    first_move: int = 1
    affinity: bool = False
    below_normal: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= 32 or not 1 <= self.hash_mb <= 2048:
            raise ValueError("Use 1–32 workers and 1–2048 MiB of Hash per worker.")
        if not 1 <= self.fast_depth <= self.discovery_depth <= self.detail_depth <= 40:
            raise ValueError("Use fast ≤ discovery ≤ detailed depth, each from 1 to 40.")
        if not 1 <= self.detail_pv <= 16 or not 1 <= self.decisive_plies <= 100:
            raise ValueError("Use 1–16 detailed lines and 1–100 consecutive decisive plies.")
        if min(self.first_move, self.loss_cp, self.decisive_cp, self.acceptable_cp) < 1:
            raise ValueError("Move numbers and evaluation thresholds must be positive.")


@dataclass(frozen=True)
class GameFilter:
    min_rating: int | None = None
    max_rating: int | None = None
    rating_mode: str = "both"
    speeds: tuple[str, ...] = ()
    rated: bool | None = None
    time_control: str = ""
    since: str = ""
    until: str = ""
    opening: str = ""
    eco: str = ""
    username: str = ""
    clocks: bool = False
    utc_start: str = ""
    utc_end: str = ""

    def __post_init__(self) -> None:
        from datetime import date

        if self.rating_mode not in ("both", "either"):
            raise ValueError("Rating match must be both or either player.")
        if self.min_rating is not None and self.max_rating is not None and self.min_rating > self.max_rating:
            raise ValueError("Minimum rating is above maximum rating.")
        for value in (self.since, self.until):
            if value:
                date.fromisoformat(value)
        if self.since and self.until and self.since > self.until:
            raise ValueError("Start date is after end date.")
        if self.utc_start or self.utc_end:
            clock = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
            if not re.fullmatch(clock, self.utc_start) or not re.fullmatch(clock + "|24:00", self.utc_end):
                raise ValueError("Enter both UTC times as HH:MM. Use 24:00 for the end of the day.")
            if self.utc_start >= self.utc_end:
                raise ValueError("The UTC end time must be later than the start time on the same date.")
        if any(speed not in SPEEDS for speed in self.speeds):
            raise ValueError("Unsupported time-control category.")


@dataclass(frozen=True)
class Source:
    kind: str
    locator: str
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def immutable(self) -> bool:
        return self.kind in ("local", "archive_stream", "archive_cache")

    @property
    def rated(self) -> bool | None:
        if self.kind in ("archive_stream", "archive_cache"):
            return True
        if self.kind == "api_user" and self.params.get("rated") in ("true", "false"):
            return self.params["rated"] == "true"
        return None

    @classmethod
    def local(cls, path: Path) -> Source:
        path = path.resolve(strict=True)
        if not path.is_file() or not path.name.lower().endswith((".pgn", ".pgn.zst")):
            raise ValueError(f"Choose a .pgn or .pgn.zst file: {path}")
        info = path.stat()
        return cls("local", str(path), {"size": info.st_size, "mtime_ns": info.st_mtime_ns})


@dataclass(frozen=True)
class Score:
    """Absolute score for White, or a forced mate with a separate winner sign."""

    cp: int | None = None
    mate: int | None = None
    winner: int = 0

    @classmethod
    def from_uci(cls, score: chess.engine.PovScore) -> Score:
        white = score.white()
        mate = white.mate()
        if mate is not None:
            return cls(mate=abs(mate), winner=1 if white > chess.engine.Cp(0) else -1)
        return cls(cp=white.score())

    def for_turn(self, color: chess.Color) -> Score:
        if color == chess.WHITE:
            return self
        return Score(None if self.cp is None else -self.cp, self.mate, -self.winner)

    def display(self) -> str:
        if self.mate is not None:
            return f"{'+' if self.winner > 0 else '−'}M{self.mate}"
        return f"{self.cp / 100:+.2f}" if self.cp is not None else "—"

    def pgn(self) -> str:
        if self.mate is not None:
            return f"#{self.winner * self.mate}" if self.mate else self.display()
        return f"{self.cp / 100:.2f}" if self.cp is not None else ""


@dataclass(frozen=True)
class Variation:
    pv: tuple[str, ...]
    score: Score
    depth: int
    nodes: int | None = None

    def dump(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, item: dict) -> Variation:
        return cls(tuple(item["pv"]), Score(**item["score"]), item["depth"], item["nodes"])


def board_at(root: str, moves: list[str], ply: int | None = None) -> chess.Board:
    board = chess.Board(root)
    for move in moves[:ply]:
        board.push_uci(move)
    return board


def position_key(board: chess.Board) -> str:
    return " ".join(board.fen().split()[:4])
