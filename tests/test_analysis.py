from dataclasses import replace

import chess
import chess.engine
import pytest

from mistake_scanner.analysis import AdaptiveDepth, analyse_critical, discover, loss
from mistake_scanner.database import Library
from mistake_scanner.model import Score, SearchSettings, Variation, board_at
from conftest import game_moves


@pytest.mark.parametrize("best,played,expected", [
    (80, 20, 60), (250, 180, 70), (350, 250, 100), (800, 200, 600),
    (800, 700, None), (800, 400, None), (-200, -800, 600), (-700, -800, None),
    (90, 40, 50), (89, 40, None), (350, 300, None), (300, 250, 50),
])
def test_threshold_and_decisive_results(best, played, expected):
    rules = SearchSettings()
    found = loss(Score(cp=best), Score(cp=played), rules)
    assert (found[1] if found else None) == expected


def test_mover_orientation_mate_and_zero():
    black = chess.engine.PovScore(chess.engine.Cp(800), chess.BLACK)
    white = Score.from_uci(black)
    assert white.cp == -800
    assert white.for_turn(chess.BLACK).cp == 800
    mate = Score.from_uci(chess.engine.PovScore(chess.engine.Mate(0), chess.BLACK))
    assert mate.winner == 1 and mate.mate == 0
    assert mate.for_turn(chess.BLACK).winner == -1
    assert loss(Score(mate=2, winner=1), Score(cp=800), SearchSettings()) == ("Missed forced mate", None)
    assert loss(Score(cp=-800), Score(mate=3, winner=-1), SearchSettings()) == ("Allowed forced mate", None)
    assert loss(Score(mate=2, winner=1), Score(mate=4, winner=1), SearchSettings()) is None
    assert loss(Score(mate=6, winner=-1), Score(mate=2, winner=-1), SearchSettings())[0] == "Mate distance worsened"


def test_six_plies_same_side_and_shallow_collapse():
    depth = AdaptiveDepth(SearchSettings())
    for _ in range(5):
        depth.observe(Score(cp=300))
    assert not depth.fast
    depth.observe(Score(cp=-300))
    assert not depth.fast and depth.streak == 1
    for _ in range(5):
        depth.observe(Score(cp=-400))
    assert depth.fast
    assert not depth.collapse(Score(cp=-800), Score(cp=-700))
    assert depth.collapse(Score(cp=-800), Score(cp=-200))
    assert depth.collapse(Score(cp=-800), Score(cp=350))


def test_no_periodic_normal_depth_and_collapse_investigation():
    game = game_moves("Nf3", "Nf6", "Nc3", "Nc6", "Nb1", "Nb8", "Ng1", "Ng8", "Nf3", "Nf6", "e4")
    calls = []

    def search(board, depth, count=1, move=None):
        ply = len(board.move_stack)
        calls.append((ply, depth, move))
        return [Variation((move or next(iter(board.legal_moves)).uci(),), Score(cp=150 if ply == 10 else 800), depth)]

    traces, _, _ = discover(game, SearchSettings(), search)
    depths = {ply: depth for ply, _, depth in traces}
    assert [depths[ply] for ply in range(1, 12)] == [12] * 6 + [4, 4, 4, 12, 12]
    assert (10, 4, None) in calls and (10, 12, None) in calls
    assert not any(ply in (7, 8) and depth == 12 for ply, depth, _ in calls)


def test_discovery_restricts_root_for_black_when_provisional_loss_is_enough():
    game = game_moves("e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#")
    calls = []

    def search(board, depth, count=1, move=None):
        ply = len(board.move_stack)
        calls.append((ply, depth, move))
        if move == "g8f6" and ply == 5:
            return [Variation((move,), Score(mate=1, winner=1), depth)]
        if ply == 5:
            return [Variation(("g7g6",), Score(cp=25), depth)]
        if ply == 6:
            return [Variation(("h5f7",), Score(mate=1, winner=1), depth)]
        if ply == 7:
            return [Variation((), Score(mate=0, winner=1), 0)]
        return [Variation((game["moves"][ply],), Score(cp=25), depth)]

    _, candidates, _ = discover(game, SearchSettings(), search)
    assert [item["ply"] for item in candidates] == [6]
    assert (5, 12, "g8f6") in calls
    assert candidates[0]["reason"] == "Allowed forced mate"
    assert candidates[0]["loss_cp"] is None


def test_detail_restricts_actual_move_caches_positions_and_detects_missed_reply(tmp_path):
    library = Library(tmp_path / "library.sqlite3")
    game = game_moves("e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qh3", "Be7")
    played, reply = game["moves"][5:7]
    candidate = {"ply": 6, "played": played}
    calls = []

    def search(board, depth, count=1, move=None):
        ply = len(board.move_stack)
        calls.append((ply, depth, count, move))
        if move:
            return [Variation((move,), Score(cp=430 if ply == 5 else 40), depth, 123)]
        best = "g7g6" if ply == 5 else "h5f7"
        alternatives = [m.uci() for m in board.legal_moves if m.uci() not in (best, played, reply)][:count - 1]
        return [Variation((m,), Score(cp=0 if ply == 5 else 450), depth, 1000) for m in [best, *alternatives]]

    rules = SearchSettings()
    first = analyse_critical(library, game, candidate, {"id": "one"}, rules, search)
    assert first["verified"] and first["loss_cp"] == 430
    assert first["punishment"]["missed"] and first["punishment"]["played"] == reply
    assert first["followup"]["ply"] == 7
    assert (5, 20, 5, None) in calls and (6, 20, 5, None) in calls
    assert (5, 20, 1, played) in calls and (6, 20, 1, reply) in calls
    assert len(library.cached_search(first["before_key"])) == 5
    assert len(library.cached_search(first["after_key"])) == 5
    assert first["actual"]["depth"] == 20 and first["actual"]["nodes"] == 123
    expected = list(calls)
    assert first == analyse_critical(library, game, candidate, {"id": "one"}, rules, search)
    assert calls == expected

    rejected = analyse_critical(library, game, candidate, {"id": "one"}, replace(rules, loss_cp=600), search)
    assert not rejected["verified"]
    assert calls == expected
    alternate = analyse_critical(library, game, candidate, {"id": "another"}, rules, search)
    assert alternate["before_key"] != first["before_key"]
    assert len(calls) == len(expected) * 2


def test_context_preserves_root_and_moves():
    a = game_moves("Nf3", "Nf6", "Nc3", "Nc6")
    b = game_moves("Nc3", "Nc6", "Nf3", "Nf6")
    first, second = (board_at(g["root"], g["moves"]) for g in (a, b))
    assert first.fen() == second.fen()
    assert [m.uci() for m in first.move_stack] != [m.uci() for m in second.move_stack]
