import math

import chess
import pytest

from mistake_scanner.maia import MaiaProcess, policy_key, predict_mistake, recent_fens, round_rating
from mistake_scanner.database import Library
from mistake_scanner.model import board_at
from conftest import executable_from_env, game_moves


@pytest.mark.parametrize("white,black,answer", [
    (1843, 1737, 1800), (1700, 1843, 1800), (1699, 1843, 1600),
    (400, 3000, 600), (3100, 3500, 3000), (None, 1474, None),
])
def test_rating_inputs(white, black, answer):
    assert round_rating(white, black) == answer


def test_history_is_in_policy_cache_key_and_ratings_are_equal(tmp_path):
    a = game_moves("Nf3", "Nf6", "Nc3", "Nc6")
    b = game_moves("Nc3", "Nc6", "Nf3", "Nf6")
    first, second = (board_at(game["root"], game["moves"]) for game in (a, b))
    assert first.fen() == second.fen() and len(recent_fens(first)) == 5
    assert policy_key(first, 1800, 1800) != policy_key(second, 1800, 1800)
    assert policy_key(first, 1800, 1800) != policy_key(first, 1800, 1600)
    library = Library(tmp_path / "chess.sqlite3")
    calls = []

    def predict(board, elo, other):
        assert elo == other == 1800
        calls.append(len(board.move_stack))
        moves = list(board.legal_moves)
        return {move.uci(): 1 / len(moves) for move in moves}

    mistake = {"ply": 4, "played": a["moves"][-1], "maia_elo": 1800}
    before, after, probability = predict_mistake(library, a, mistake, predict)
    assert library.policy(before)[mistake["played"]] == probability
    assert library.policy(after) and calls == [3, 4]
    assert predict_mistake(library, a, mistake, predict) == (before, after, probability)
    assert calls == [3, 4]


@pytest.mark.maia
def test_real_official_model_shared_process_full_legal_distribution():
    worker = MaiaProcess(executable_from_env("MAIA_CHECKPOINT"))
    try:
        position = chess.Board()
        worker_pid = worker.process.pid
        first = worker.predict(position, 1800, 1800)
        assert set(first) == {move.uci() for move in position.legal_moves}
        assert math.isclose(sum(first.values()), 1, abs_tol=1e-5)
        assert first["e2e4"] > first["a2a3"]
        position.push_san("e4")
        second = worker.predict(position, 1800, 1800)
        assert math.isclose(sum(second.values()), 1, abs_tol=1e-5) and "e7e5" in second
        low = worker.predict(position, 600, 600)
        assert any(abs(second[move] - low[move]) > 1e-4 for move in second)
        promotion = chess.Board("8/8/4k3/8/8/8/p4K2/8 b - - 0 1")
        predicted = worker.predict(promotion, 1800, 1800)
        assert {"a2a1q", "a2a1b", "a2a1r", "a2a1n"} <= predicted.keys()
        assert worker.process.pid == worker_pid
    finally:
        worker.close()
