from __future__ import annotations

import subprocess
import threading
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import chess
import chess.engine
import psutil

from .model import Score, SearchSettings, Variation, board_at, file_digest, key, position_key

if TYPE_CHECKING:
    from .database import Library


def loss(best: Score, played: Score, rules: SearchSettings) -> tuple[str, int | None] | None:
    """Compare two scores oriented to the player who made the move."""
    if best.mate is not None or played.mate is not None:
        if best.winner == 1 and played.winner != 1:
            return "Missed forced mate", None
        if played.winner == -1 and best.winner != -1:
            return "Allowed forced mate", None
        if best.winner and best.winner == played.winner:
            if (played.mate - best.mate) * best.winner >= 4:
                return "Mate distance worsened", None
        return None
    if best.cp is None or played.cp is None:
        return None
    threshold = rules.decisive_cp
    if best.cp >= threshold and played.cp >= threshold:
        return None
    if best.cp <= -threshold and played.cp <= -threshold:
        return None
    change = best.cp - played.cp
    return ("Evaluation loss", change) if change >= rules.loss_cp else None


def decisive_side(score: Score, threshold: int) -> int:
    if score.mate is not None:
        return score.winner
    if score.cp is not None and abs(score.cp) >= threshold:
        return 1 if score.cp > 0 else -1
    return 0


class AdaptiveDepth:
    def __init__(self, rules: SearchSettings):
        self.rules = rules
        self.side = self.streak = 0

    @property
    def fast(self) -> bool:
        return self.streak >= self.rules.decisive_plies

    def collapse(self, before: Score, after: Score) -> bool:
        if decisive_side(after, self.rules.decisive_cp) != self.side:
            return True
        if before.mate is not None and before.winner == self.side and after.mate is None:
            return True
        if before.cp is not None and after.cp is not None:
            return (before.cp - after.cp) * self.side >= self.rules.decisive_cp
        return False

    def observe(self, score: Score) -> None:
        side = decisive_side(score, self.rules.decisive_cp)
        self.streak = self.streak + 1 if side and side == self.side else int(bool(side))
        self.side = side


def terminal(board: chess.Board) -> Variation | None:
    outcome = board.outcome()
    if outcome is None:
        return None
    score = Score(cp=0) if outcome.winner is None else Score(mate=0, winner=1 if outcome.winner else -1)
    return Variation((), score, 0, 0)


class UciSearch:
    def __init__(self, profile: dict, rules: SearchSettings, cpu: int | None = None):
        path = Path(profile["path"])
        self.engine = chess.engine.SimpleEngine.popen_uci(
            str(path), cwd=path.parent, timeout=max(60, rules.detail_depth * 4),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.game = object()
        try:
            process = psutil.Process(self.engine.transport.get_pid())
            if rules.below_normal:
                try:
                    process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS") else 5)
                except (psutil.Error, OSError):
                    pass
            if cpu is not None:
                try:
                    process.cpu_affinity([cpu])
                except (psutil.Error, OSError, AttributeError):
                    pass
            options = {"Threads": 1, "Hash": rules.hash_mb, **profile.get("uci_options", {})}
            self.engine.configure({name: value for name, value in options.items() if name in self.engine.options})
        except BaseException:
            self.engine.close()
            raise

    def __call__(self, board: chess.Board, depth: int, count: int = 1, move: str | None = None, *, seconds: float | None = None) -> list[Variation]:
        final = terminal(board)
        if final is not None:
            return [final]
        root_moves = [chess.Move.from_uci(move)] if move is not None else None
        data = self.engine.analyse(
            board, chess.engine.Limit(depth=depth, time=seconds), multipv=count, root_moves=root_moves, game=self.game,
        )
        lines = [
            Variation(tuple(m.uci() for m in info["pv"]), Score.from_uci(info["score"]),
                      info.get("depth", depth), info.get("nodes"))
            for info in data
            if "score" in info and info.get("pv")
        ]
        if not lines:
            raise RuntimeError("Stockfish returned no score or variation.")
        if move is not None and lines[0].pv[0] != move:
            raise RuntimeError("Stockfish returned a different move from the requested root move.")
        return lines

    def close(self) -> None:
        try:
            self.engine.quit()
        except (chess.engine.EngineError, OSError, TimeoutError):
            self.engine.close()


class EngineWorkers:
    """One retained UCI process per active executor thread."""

    def __init__(self, cpus: list[int], factory=UciSearch):
        self.cpus = cpus
        self.factory = factory
        self.lock = threading.Lock()
        self.engines = OrderedDict()
        self.verified = {}

    def acquire(self, profile: dict, rules: SearchSettings) -> UciSearch:
        worker = threading.get_ident()
        identity = (profile["id"], rules.hash_mb, rules.affinity, rules.below_normal)
        with self.lock:
            previous = self.engines.get(worker)
            if previous and previous[0] == identity:
                previous[1].game = object()
                return previous[1]
            if previous:
                self.engines.pop(worker)
                previous[1].close()
            paths = [profile["path"], *(n["path"] for n in profile.get("networks", []) if n.get("path"))]
            stats = [Path(path).stat() for path in paths]
            fingerprint = tuple((s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) for s in stats)
            if self.verified.get(profile["id"]) != fingerprint:
                verify_engine_files(profile)
                self.verified[profile["id"]] = fingerprint
            cpu = self.cpus[len(self.engines) % len(self.cpus)] if rules.affinity and self.cpus else None
            search = self.factory(profile, rules, cpu)
            self.engines[worker] = identity, search
            return search

    def discard(self) -> None:
        with self.lock:
            previous = self.engines.pop(threading.get_ident(), None)
        if previous:
            previous[1].close()

    def close(self) -> None:
        with self.lock:
            previous = list(self.engines.values())
            self.engines.clear()
            self.verified.clear()
        for _, engine in previous:
            engine.close()


def verify_engine_files(profile: dict) -> None:
    if not profile.get("sha256"):
        raise ValueError("The engine profile has no executable fingerprint.")
    if file_digest(Path(profile["path"])) != profile["sha256"]:
        raise ValueError("The Stockfish executable changed. Validate it as a new engine profile.")
    for network in profile.get("networks", []):
        if network.get("path") and file_digest(Path(network["path"])) != network["sha256"]:
            raise ValueError(f"An NNUE file changed: {network['path']}")


def discover(game: dict, rules: SearchSettings, search) -> tuple[list, list, list]:
    board = chess.Board(game["root"])
    adaptive = AdaptiveDepth(rules)
    traces, candidates, occurrences = [], [], []
    previous: dict[int, Variation] = {}

    for ply, actual in enumerate(game["moves"], 1):
        move = chess.Move.from_uci(actual)
        moving = board.turn
        eligible = board.fullmove_number >= rules.first_move
        prior_key = position_key(board)
        if eligible:
            occurrences.append((ply, prior_key, actual))

        depth = rules.fast_depth if adaptive.fast else rules.discovery_depth
        best = previous[depth] if depth in previous else search(board, depth)[0]
        board.push(move)
        post = search(board, depth)[0]
        board.pop()
        initial_post = {depth: post}
        if adaptive.fast and adaptive.collapse(best.score, post.score):
            adaptive.streak = 0
            depth = rules.discovery_depth
            if depth not in initial_post:
                best = previous[depth] if depth in previous else search(board, depth)[0]
                board.push(move)
                post = search(board, depth)[0]
                board.pop()
                initial_post[depth] = post

        if ply == 1:
            traces.append((0, asdict(best.score), best.depth))
        traces.append((ply, asdict(post.score), post.depth))
        if eligible and best.pv and best.pv[0] != actual:
            estimate = post.score.for_turn(moving)
            if estimate.mate is not None and estimate.winner == 1:
                estimate = Score(mate=estimate.mate + 1, winner=1)
            if loss(best.score.for_turn(moving), estimate, rules):
                played = search(board, depth, move=actual)[0]
                found = loss(best.score.for_turn(moving), played.score.for_turn(moving), rules)
                if found:
                    candidates.append({
                        "ply": ply, "position": prior_key, "played": actual,
                        "best": best.pv[0], "reason": found[0], "loss_cp": found[1],
                        "discovery_best": best.dump(), "discovery_played": played.dump(),
                        "clock": game["clocks"][ply - 1],
                    })

        adaptive.observe(post.score)
        previous = initial_post
        board.push(move)

    return traces, candidates, occurrences


def analyse_critical(
    library: Library, game: dict, mistake: dict, profile: dict, rules: SearchSettings, search,
) -> dict:
    before = board_at(game["root"], game["moves"], mistake["ply"] - 1)
    played_move = mistake["played"]
    after = before.copy()
    after.push_uci(played_move)

    def lookup(board: chess.Board, count: int, move: str | None = None) -> tuple[str, list[Variation]]:
        cache_id = key([
            "stockfish-v1", profile["id"], rules.hash_mb, 1, rules.detail_depth,
            count, move, board.root().fen(en_passant="fen"), [m.uci() for m in board.move_stack],
        ])
        lines = library.cached_search(cache_id)
        if lines is None:
            lines = search(board, rules.detail_depth, count, move)
            if all(line.depth >= rules.detail_depth or not line.pv for line in lines):
                lines = library.save_search(cache_id, lines)
            else:
                raise RuntimeError("Stockfish stopped before reaching the detailed depth.")
        return cache_id, lines

    before_key, lines = lookup(before, rules.detail_pv)
    after_key, replies = lookup(after, rules.detail_pv)
    best = lines[0]
    actual = next((line for line in lines if line.pv and line.pv[0] == played_move), None)
    if actual is None:
        _, restricted = lookup(before, 1, played_move)
        actual = restricted[0]
    found = loss(best.score.for_turn(before.turn), actual.score.for_turn(before.turn), rules)

    def acceptable(choices: list[Variation], color: chess.Color) -> tuple[list[str], str]:
        if not choices or not choices[0].pv:
            return [], "Terminal position"
        best_score = choices[0].score.for_turn(color)
        matches = []
        near = 0
        for line in choices:
            score = line.score.for_turn(color)
            if best_score.cp is not None and score.cp is not None:
                equal = abs(best_score.cp - score.cp) <= rules.acceptable_cp
                both_won = min(best_score.cp, score.cp) >= rules.decisive_cp
            else:
                equal = best_score.winner == score.winner and best_score.mate == score.mate
                both_won = best_score.winner == score.winner == 1
            if equal or both_won:
                matches.append(line.pv[0])
            near += int(equal)
        if len(matches) == 1:
            label = f"Apparent only move at d{choices[0].depth}"
            if len(choices) == 1:
                label = "Only legal move" if (before if color == before.turn else after).legal_moves.count() == 1 else "One line searched"
            if best_score.winner == 1:
                label += " · forced mate"
        elif best_score.winner == 1:
            label = "Multiple mating moves"
        elif near == len(choices):
            label = "Approximately equivalent moves"
        elif best_score.cp is not None and best_score.cp >= rules.decisive_cp:
            label = "Different winning continuations"
        else:
            label = "Several acceptable moves"
        return matches, label

    good_before, label_before = acceptable(lines, before.turn)
    good_after, label_after = acceptable(replies, after.turn)
    response_index = mistake["ply"]
    punishment = None
    followup = None
    if found and replies[0].pv and response_index < len(game["moves"]):
        response = game["moves"][response_index]
        response_line = next((line for line in replies if line.pv[0] == response), None)
        if response_line is None:
            _, restricted_reply = lookup(after, 1, response)
            response_line = restricted_reply[0]
        reply_loss = loss(replies[0].score.for_turn(after.turn), response_line.score.for_turn(after.turn), rules)
        punishment = {"best": replies[0].pv[0], "played": response, "actual": response_line.dump(),
                      "missed": reply_loss is not None}
        if reply_loss:
            followup = {
                "ply": response_index + 1, "position": position_key(after), "played": response,
                "best": replies[0].pv[0], "reason": reply_loss[0], "loss_cp": reply_loss[1],
                "discovery_best": replies[0].dump(), "discovery_played": response_line.dump(),
                "clock": game["clocks"][response_index], "linked": True,
            }
    return {
        "verified": found is not None,
        "reason": found[0] if found else "Rejected by deeper search",
        "loss_cp": found[1] if found else None,
        "before_key": before_key, "after_key": after_key,
        "best": best.pv[0], "actual": actual.dump(),
        "good_before": good_before, "good_after": good_after,
        "label_before": label_before, "label_after": label_after,
        "punishment": punishment, "followup": followup,
    }
