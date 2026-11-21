from __future__ import annotations

import importlib.util
import math
import multiprocessing
import os
import subprocess
import sys
from collections import deque
from pathlib import Path

import chess
import psutil

from .model import board_at, file_digest, key
from .network import Network, fetch_file

CHECKPOINT_NAME = "maia3-79m.pt"
CHECKPOINT_SIZE = 315651851
CHECKPOINT_SHA = "3fc6181d5db789b45a15305732148757ae74efa3e0028e81ba335b462dac45c2"
CHECKPOINT_REVISION = "a107d6ceb7b298cb04ae1da4edffe2939858b894"
UPSTREAM_REVISION = "1e13597c42d4858b7cfd7cfdae01e297263364b2"
CHECKPOINT_URL = f"https://huggingface.co/UofTCSSLab/Maia3-79M/resolve/{CHECKPOINT_REVISION}/{CHECKPOINT_NAME}"
HISTORY = 8


def round_rating(white: int | None, black: int | None) -> int | None:
    if white is None or black is None:
        return None
    return max(600, min(3000, ((min(white, black) + 100) // 200) * 200))


def runtime_installed() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("maia3", "torch"))


def install_cpu_runtime(status) -> None:
    """Install the pinned runtime in the Python edition's existing virtual environment."""
    if getattr(sys, "frozen", False) or sys.prefix == sys.base_prefix:
        raise RuntimeError("Open the Python edition with launch.py to install the Maia CPU runtime.")
    upstream = (f"https://codeload.github.com/CSSLab/maia3/zip/{UPSTREAM_REVISION}"
                "#sha256=f5210ea0481aba514d58524f50283496b13e219a715143f97d2d0a62b5e9e3ce")
    for label, packages in (
        ("Installing PyTorch CPU · first setup may take several minutes…",
         ["--index-url", "https://download.pytorch.org/whl/cpu", "torch==2.8.0+cpu"]),
        ("Installing the official Maia 3 runtime…", [f"maia3 @ {upstream}"]),
    ):
        status(label)
        result = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *packages],
                                capture_output=True, text=True, errors="replace", timeout=1200,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            excerpt = "\n".join((result.stderr or result.stdout).splitlines()[-12:])
            raise RuntimeError(f"Maia runtime setup failed.\n{excerpt}")
    importlib.invalidate_caches()


def download_checkpoint(path: Path, network: Network, progress=lambda *_: None) -> Path:
    return fetch_file(network, CHECKPOINT_URL, path, sha256=CHECKPOINT_SHA, size=CHECKPOINT_SIZE, progress=progress)


def recent_fens(board: chess.Board) -> tuple[str, ...]:
    position = board.copy(stack=HISTORY - 1)
    fens = [position.fen(en_passant="fen")]
    while position.move_stack:
        position.pop()
        fens.append(position.fen(en_passant="fen"))
    return tuple(reversed(fens))


def policy_key(board: chess.Board, self_elo: int, opponent_elo: int) -> str:
    return key(["maia-policy-v2", UPSTREAM_REVISION, CHECKPOINT_SHA, self_elo, opponent_elo,
                board.is_game_over(), recent_fens(board)])


class MaiaModel:
    """Official Chessformer architecture and tokenization with a full legal policy."""

    def __init__(self, checkpoint: Path, threads: int = 2):
        if checkpoint.stat().st_size != CHECKPOINT_SIZE or file_digest(checkpoint) != CHECKPOINT_SHA:
            raise ValueError("The checkpoint is not the pinned official Maia 3 79M file. Download it again.")
        try:
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 5)
        except (psutil.Error, OSError):
            pass

        import torch
        from maia3.dataset import get_historical_tokens, tokenize_board
        from maia3.uci import load_model, parse_args
        from maia3.utils import get_all_possible_moves, mirror_move

        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        self.torch = torch
        self.cfg = parse_args(["--model", "79m", "--checkpoint-path", str(checkpoint), "--device", "cpu", "--no-use-amp"])
        self.model = load_model(self.cfg)
        self.tokenize = tokenize_board
        self.historical = get_historical_tokens
        self.mirror = mirror_move
        self.indices = {move: i for i, move in enumerate(get_all_possible_moves())}

    def predict(self, fens: tuple[str, ...], self_elo: int, opponent_elo: int) -> dict[str, float]:
        boards = [chess.Board(fen) for fen in fens]
        board = boards[-1]
        moves = [move.uci() for move in board.legal_moves]
        if not moves:
            return {}
        history = deque((self.tokenize(item) for item in boards), maxlen=HISTORY)
        tokens = self.historical(history, self.cfg, base=0, inc=0, clk_left_before=0, clk_ponder=0)
        indexes = [self.indices[move if board.turn else self.mirror(move)] for move in moves]
        torch = self.torch
        with torch.inference_mode():
            logits, _, _ = self.model(tokens.unsqueeze(0), torch.tensor([self_elo]), torch.tensor([opponent_elo]))
            probabilities = torch.softmax(logits[0, indexes].float(), dim=0).tolist()
        if any(not math.isfinite(p) or p < 0 for p in probabilities) or not math.isclose(sum(probabilities), 1, abs_tol=1e-5):
            raise RuntimeError("Maia returned an invalid legal-move distribution.")
        return dict(zip(moves, probabilities, strict=True))


def _serve(pipe, checkpoint: str, threads: int) -> None:
    model = None
    with pipe:
        try:
            while (request := pipe.recv()) is not None:
                try:
                    if model is None:
                        model = MaiaModel(Path(checkpoint), threads)
                    pipe.send((True, model.predict(*request)))
                except Exception as exc:
                    pipe.send((False, f"{type(exc).__name__}: {exc}"))
                    if model is None:
                        return
        except (EOFError, BrokenPipeError):
            pass


class MaiaProcess:
    """Owned by a single inference thread and retained across requests."""

    def __init__(self, checkpoint: Path, threads: int = 2):
        self.checkpoint = checkpoint
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_serve, args=(child, str(checkpoint), threads), daemon=True)
        self.process.start()
        child.close()

    def predict(self, board: chess.Board, self_elo: int, opponent_elo: int) -> dict:
        if not self.process.is_alive():
            raise RuntimeError("The Maia worker stopped. Check the model setup and retry the scan.")
        self.pipe.send((recent_fens(board), self_elo, opponent_elo))
        if not self.pipe.poll(150):
            self.close()
            raise TimeoutError("The Maia worker did not respond within 150 seconds.")
        ok, result = self.pipe.recv()
        if not ok:
            raise RuntimeError(result)
        if set(result) != {move.uci() for move in board.legal_moves}:
            raise RuntimeError("Maia returned a policy for a different position.")
        return result

    def close(self) -> None:
        if not self.pipe.closed:
            try:
                self.pipe.send(None)
            except (BrokenPipeError, EOFError, OSError):
                pass
            self.pipe.close()
        self.process.join(2)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(2)


def predict_mistake(library, game: dict, mistake: dict, predict) -> tuple[str, str, float]:
    before = board_at(game["root"], game["moves"], mistake["ply"] - 1)
    after = before.copy()
    after.push_uci(mistake["played"])
    elo = mistake["maia_elo"]

    def cached(board):
        identity = policy_key(board, elo, elo)
        policy = library.policy(identity)
        if policy is None:
            # An automatic draw can end a game with legal moves still on the board.
            policy = predict(board, elo, elo) if not board.is_game_over() else {}
            policy = library.save_policy(identity, policy)
        return identity, policy

    first, before_policy = cached(before)
    second, _ = cached(after)
    return first, second, before_policy[mistake["played"]]
