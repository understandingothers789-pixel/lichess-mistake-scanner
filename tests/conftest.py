from pathlib import Path

import chess
import pytest

from mistake_scanner.model import file_digest

SAMPLE = """[Event "Rated Rapid game"]
[Site "https://lichess.org/AbcD1234"]
[UTCDate "2025.01.20"]
[White "ExampleWhite"]
[Black "ExampleBlack"]
[WhiteElo "1843"]
[BlackElo "1737"]
[TimeControl "600+5"]
[ECO "C23"]
[Opening "Bishop's Opening"]
[Result "1-0"]

1. e4 { [%clk 0:10:03] } e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7# 1-0

"""


@pytest.fixture
def pgn(tmp_path):
    file = tmp_path / "games.pgn"
    file.write_text(SAMPLE, encoding="utf-8")
    return file


@pytest.fixture
def fake_profile(tmp_path):
    executable = tmp_path / "stockfish-identity"
    executable.write_bytes(b"version one")
    return {"id": "fish1", "name": "Stockfish test", "version": "Stockfish test", "sha256": file_digest(executable),
            "path": str(executable), "architecture": "test", "origin": "manual", "installed": "2026-11-21",
            "commit": "a" * 40, "networks": []}


def game_moves(*moves):
    position = chess.Board()
    uci = [position.push_san(move).uci() for move in moves]
    return {"root": chess.STARTING_FEN, "moves": uci, "clocks": [None] * len(uci)}


def executable_from_env(name):
    import os

    path = os.environ.get(name)
    if not path:
        pytest.skip(f"Set {name} to run this real integration test.")
    return Path(path).resolve(strict=True)
