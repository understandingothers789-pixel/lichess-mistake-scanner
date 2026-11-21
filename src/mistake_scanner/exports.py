from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path

import chess
import chess.engine
import chess.pgn

from .model import Score, board_at


def san_line(board: chess.Board, moves) -> str:
    return board.variation_san([chess.Move.from_uci(move) for move in moves])


def provenance(scan: dict) -> dict:
    engine = scan["profile"]
    return {
        "engine": {name: engine.get(name) for name in ("name", "version", "sha256", "commit",
                    "commit_date", "origin", "architecture", "build_mode")},
        "networks": [{k: v for k, v in net.items() if k != "path"} for net in engine.get("networks", [])],
        "settings": scan["rules"], "scan": scan["id"],
    }


def _pov(score: Score) -> chess.engine.PovScore:
    if score.mate is not None:
        if score.mate == 0:
            value = chess.engine.MateGiven if score.winner == 1 else chess.engine.Mate(0)
        else:
            value = chess.engine.Mate(score.mate * score.winner)
    else:
        value = chess.engine.Cp(score.cp)
    return chess.engine.PovScore(value, chess.WHITE)


def annotated_pgn(library, scan_game: int) -> str:
    game = library.game(scan_game)
    scan = library.scan(game["scan_id"])
    pgn = chess.pgn.read_game(io.StringIO(game["original"]))
    engine = provenance(scan)["engine"]
    pgn.headers["AnalysisEngine"] = engine["version"] or engine["name"] or "Stockfish"
    pgn.headers["AnalysisCommit"] = engine["commit"] or "unknown"
    pgn.headers["AnalysisSHA256"] = engine["sha256"]
    pgn.headers["AnalysisSettings"] = (
        f"d{scan['rules']['discovery_depth']}/d{scan['rules']['fast_depth']}; "
        f"d{scan['rules']['detail_depth']} MultiPV {scan['rules']['detail_pv']}; "
        f"Threads 1; Hash {scan['rules']['hash_mb']} MiB"
    )
    nodes = [pgn, *pgn.mainline()]
    for ply, item in library.evaluations(scan_game).items():
        nodes[ply].set_eval(_pov(Score(**item["score"])), item["depth"])
    for moment in library.moments(scan_game):
        detail = moment["detail"]
        if not detail or not detail["verified"]:
            continue
        index = moment["ply"]
        child = nodes[index]
        child.nags.add(chess.pgn.NAG_MISTAKE)
        annotation = detail["reason"]
        if detail["loss_cp"] is not None:
            annotation += f" ({detail['loss_cp'] / 100:.2f} pawns)"
        if moment["probability"] is not None:
            annotation += f"; Maia {moment['maia_elo']} predicted the played move {moment['probability']:.2%}"
        child.comment = f"{child.comment} {annotation}.".strip()
        actual = detail["actual"]
        child.set_eval(_pov(Score(**actual["score"])), actual["depth"])
        for index, field in ((index - 1, "before_key"), (index, "after_key")):
            for variation in library.cached_search(detail[field]) or []:
                if not variation.pv:
                    continue
                current = nodes[index]
                for ply, notation in enumerate(variation.pv):
                    move = chess.Move.from_uci(notation)
                    try:
                        current = current.variation(move)
                    except KeyError:
                        current = current.add_variation(move)
                    if ply == 0 and current not in nodes:
                        current.set_eval(_pov(variation.score), variation.depth)
    return pgn.accept(chess.pgn.StringExporter(headers=True, variations=True, comments=True, columns=88))


def moment_data(library, identity: int) -> dict:
    moment = library.mistake(identity)
    game = library.game(moment["scan_game"])
    board = board_at(game["root"], game["moves"], moment["ply"] - 1)
    detail = moment["detail"]
    before = [v.dump() for v in library.cached_search(detail["before_key"])] if detail else []
    after = [v.dump() for v in library.cached_search(detail["after_key"])] if detail else []
    return {
        "id": identity, "scan_game": moment["scan_game"], "fen": board.fen(),
        "ply": moment["ply"], "played": moment["played"], "state": moment["state"],
        "best": detail["best"] if detail else moment["discovery"]["best"],
        "solution": list(before[0]["pv"]) if before else list(moment["discovery"]["discovery_best"]["pv"]),
        "loss_cp": moment["loss_cp"], "clock_seconds": moment["clock"], "maia_rating": moment["maia_elo"],
        "maia_probability": moment["probability"], "before_policy": library.policy(moment["before_policy"]),
        "after_policy": library.policy(moment["after_policy"]), "before": before, "after": after,
        "punishment": detail.get("punishment") if detail else None,
        "game": {name: game.get(name) for name in ("site", "lichess_id", "white", "black", "day",
                 "white_elo", "black_elo", "speed", "time_control", "opening", "eco", "result")},
        "analysis": provenance(library.scan(game["scan_id"])),
    }


def write_export(library, destination: Path, kind: str, *, games=(), moments=()) -> int:
    def items():
        if kind in ("pgn", "original"):
            if games:
                yield from games
            else:
                previous = set()
                for identity in moments:
                    scan_game = library.mistake(identity)["scan_game"]
                    if scan_game not in previous:
                        yield scan_game
                        previous.add(scan_game)
        elif moments:
            yield from moments
        else:
            for identity in games:
                yield from (moment["id"] for moment in library.moments(identity))

    count = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=destination.parent,
                                     prefix=".export-", delete=False) as stream:
        temporary = Path(stream.name)
        flat_fields = ["id", "fen", "ply", "played", "best", "loss_cp", "state", "maia_rating",
                       "maia_probability", "clock_seconds", "source", "stockfish_commit", "solution"]
        csv_output = csv.DictWriter(stream, fieldnames=flat_fields, extrasaction="ignore") if kind == "csv" else None
        if csv_output:
            csv_output.writeheader()
        if kind in ("json", "puzzle"):
            stream.write("[\n")
        try:
            for identity in items():
                if kind == "pgn":
                    stream.write(annotated_pgn(library, identity) + "\n\n")
                elif kind == "original":
                    stream.write(library.game(identity)["original"].rstrip() + "\n\n")
                else:
                    item = moment_data(library, identity)
                    if kind == "fen":
                        stream.write(item["fen"] + "\n")
                    elif kind == "csv":
                        item["source"] = item["game"]["site"]
                        item["stockfish_commit"] = item["analysis"]["engine"]["commit"]
                        item["solution"] = " ".join(item["solution"])
                        csv_output.writerow(item)
                    elif kind in ("json", "puzzle"):
                        if count:
                            stream.write(",\n")
                        if kind == "puzzle":
                            keep = ("fen", "best", "solution", "state", "game", "analysis")
                            item = {name: item[name] for name in keep}
                        stream.write(json.dumps(item, ensure_ascii=False))
                    else:
                        raise ValueError("Unknown export format.")
                count += 1
            if kind in ("json", "puzzle"):
                stream.write("\n]\n")
            if count == 0:
                raise ValueError("There are no matching items to export.")
            stream.close()
            os.replace(temporary, destination)
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)
    return count
