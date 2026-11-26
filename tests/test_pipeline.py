import hashlib
from io import StringIO
from pathlib import Path
from time import monotonic, sleep

import chess
import httpx
import pytest
import zstandard

from mistake_scanner.acquisition import archive_sources, records
from mistake_scanner.database import Library
from mistake_scanner.engines import inspect_engine
from mistake_scanner.exports import annotated_pgn, moment_data, write_export
from mistake_scanner.model import GameFilter, Score, SearchSettings, Source, Variation, board_at, file_digest, position_key
from mistake_scanner.network import Network
from mistake_scanner.runner import Runner
from conftest import SAMPLE, executable_from_env


def wait_for(predicate, timeout=30):
    until = monotonic() + timeout
    while monotonic() < until:
        if value := predicate():
            return value
        sleep(0.05)
    raise AssertionError("Pipeline did not reach its expected state.")


def test_library_requires_evidence_before_labeling_a_missed_reply(tmp_path, fake_profile, pgn):
    library = Library(tmp_path / "library.sqlite3")
    game = next(records(StringIO(SAMPLE), GameFilter()))[1]
    scan = library.create_scan("Missed reply check", [Source.local(pgn)], GameFilter(), SearchSettings(), fake_profile, 0)
    assert library.import_game(library.ready_source()["id"], scan, game, 1)
    with library.transaction() as db:
        for ply in (5, 6):
            db.execute("""
                INSERT INTO mistakes(scan_game, ply, position, played, state, discovery, detail)
                VALUES (1, ?, ?, ?, 'verified', '{}', '{"punishment":{"missed":false}}')
            """, (ply, position_key(board_at(game["root"], game["moves"], ply - 1)), game["moves"][ply - 1]))
    assert library.game_page()[0]["moments"] == 2
    assert library.game_page()[0]["missed"] == 0
    assert not any(row["missed"] for row in library.discovery_browser_page())
    with library.transaction() as db:
        db.execute("UPDATE mistakes SET detail=json_set(detail,'$.punishment.missed',1) WHERE ply=5")
    assert library.game_page()[0]["missed"] == 1
    assert [row["ply"] for row in library.discovery_browser_page() if row["missed"]] == [6]


class ScriptedStockfish:
    def __init__(self, profile, rules, cpu):
        self.profile = profile
        self.game = None
        self.searched = 0

    def __call__(self, board, depth, count=1, move=None, *, seconds=None):
        from mistake_scanner.analysis import terminal

        self.searched += 1
        final = terminal(board)
        if final:
            return [final]
        ply = len(board.move_stack)
        actual = move or ("g7g6" if ply == 5 else "h5f7" if ply == 6 else None)
        score = Score(mate=1, winner=1) if ply == 6 or (ply == 5 and move == "g8f6") else Score(cp=0)
        legal = [m.uci() for m in board.legal_moves if ply != 5 or move or m.uci() != "g8f6"]
        ordered = ([actual] if actual and actual in legal else []) + [m for m in legal if m != actual]
        return [Variation((m,), score, depth, 100) for m in ordered[:count]]

    def close(self):
        pass


def test_restart_deduplicates_game_and_preserves_review(tmp_path, pgn, fake_profile):
    with pgn.open(encoding="utf-8") as stream:
        original = list(records(stream, GameFilter()))[0][1]
    pgn.write_text(SAMPLE + SAMPLE, encoding="utf-8")
    library = Library(tmp_path / "library.sqlite3")
    rules = SearchSettings(workers=1, first_move=1)
    scan_id = library.create_scan("Resume", [Source.local(pgn)], GameFilter(), rules, fake_profile, 0)
    source = library.ready_source()
    library.import_game(source["id"], scan_id, original, 1)
    old_job = library.claim("discovery")
    assert old_job["state"] == "pending"
    assert library.scan_progress(scan_id)["jobs"]["discovery", "running"] == 1

    runner = Runner(library, rules, engine_factory=ScriptedStockfish)
    runner.maia_installed = False
    try:
        assert library.scan(scan_id)["state"] == "paused"
        library.set_state(scan_id, "running")
        wait_for(lambda: library.scan(scan_id)["verified"] == 1
                 and library.scan_progress(scan_id)["sources"][0]["state"] == "done")
        progress = library.scan_progress(scan_id)
        assert progress["jobs"]["discovery", "done"] == 1
        assert progress["jobs"]["maia", "pending"] == 1
        assert library.scan(scan_id)["selected"] == 1
        assert progress["sources"][0]["seen"] == 2
        game_id = library.game_page()[0]["id"]
        moment = library.moments(game_id)[0]
        library.review(game_id, game=True, bookmarked=True, reviewed=True, cursor=5, cursor_moment=moment["id"])
        library.review(moment["id"], reviewed=True, bookmarked=True)
        assert library.connection().execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert library.connection().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        runner.close()

    reopened = Library(library.path)
    assert reopened.game(game_id)["cursor"] == 5
    assert reopened.game(game_id)["cursor_moment"] == moment["id"]
    assert reopened.game_page(bookmarks=True)[0]["reviewed"]
    assert reopened.discovery_page(review="bookmarked")[0]["id"] == moment["id"]


class BrokenFeed(httpx.SyncByteStream):
    def __iter__(self):
        yield SAMPLE.encode()
        yield SAMPLE.replace("AbcD1234", "newG1234").encode()
        raise httpx.ReadError("Interrupted mid-transfer")


def test_api_replay_after_transport_error_does_not_duplicate_jobs(tmp_path, fake_profile):
    attempts = []

    def respond(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(200, stream=BrokenFeed())
        data = SAMPLE + SAMPLE.replace("AbcD1234", "newG1234") + SAMPLE.replace("AbcD1234", "yetG1234")
        return httpx.Response(200, text=data)

    library = Library(tmp_path / "library.sqlite3")
    rules = SearchSettings(workers=1, first_move=1)

    def network(**_):
        return Network(httpx.Client(transport=httpx.MockTransport(respond)), wait=lambda _: None)

    runner = Runner(library, rules, network_factory=network, engine_factory=ScriptedStockfish)
    try:
        identity = library.create_scan("API retry", [Source("api_user", "ExampleWhite")],
                                       GameFilter(), rules, fake_profile, 0)
        wait_for(lambda: library.scan(identity)["analyzed"] == 3)
        assert len(attempts) == 2
        assert library.scan(identity)["selected"] == 3
        assert len(library.game_page()) == 3
        assert not library.scan_progress(identity)["errors"]
    finally:
        runner.close()


@pytest.mark.parametrize("resuming", [False, True])
def test_archive_batch_streams_selected_days_with_independent_quotas(tmp_path, fake_profile, resuming):
    data = SAMPLE + SAMPLE.replace("AbcD1234", "same1234")
    data += SAMPLE.replace("AbcD1234", "skip1234").replace("2025.01.20", "2025.01.21")
    data += SAMPLE.replace("AbcD1234", "next1234").replace("2025.01.20", "2025.01.22")
    data += SAMPLE.replace("AbcD1234", "late1234").replace("2025.01.20", "2025.01.22")
    body = zstandard.ZstdCompressor().compress(data.encode())
    calls = []
    filename = "lichess_db_standard_rated_2025-01.pgn.zst"

    def respond(request):
        if request.url.path == "/":
            return httpx.Response(200, text=f"{filename} lichess_db_standard_rated_2025-02.pgn.zst")
        if request.url.path.endswith("sha256sums.txt"):
            return httpx.Response(200, text=f"{hashlib.sha256(body).hexdigest()}  {filename}")
        calls.append(request.url.path)
        return httpx.Response(200, content=body)

    def network(**_):
        return Network(httpx.Client(transport=httpx.MockTransport(respond)), wait=lambda _: None)

    library = Library(tmp_path / "library.sqlite3")
    rules = SearchSettings(workers=1)
    plan = network()
    try:
        sources = archive_sources(plan, "2025-01", "2025-02", days=("2025-01-22", "2025-01-20"), daily_limit=1)
    finally:
        plan.close()
    assert len(sources) == 1
    identity = library.create_scan("Two UTC days", sources, GameFilter(), rules, fake_profile, 0)
    if resuming:
        first = next(records(StringIO(SAMPLE), GameFilter()))[1]
        library.import_game(library.ready_source()["id"], identity, first, 1)
    runner = Runner(library, rules, network_factory=network, engine_factory=ScriptedStockfish)
    try:
        library.set_state(identity, "running")
        wait_for(lambda: library.scan_progress(identity)["sources"][0]["state"] == "done")
        wait_for(lambda: library.scan(identity)["analyzed"] == 2)
        assert calls == [f"/standard/{filename}"]
        assert library.scan_day_counts(identity) == {"2025-01-20": 1, "2025-01-22": 1}
        assert library.scan_progress(identity)["sources"][0]["seen"] == 4
    finally:
        runner.close()


def test_scans_keep_engine_snapshot_and_removal_protects_jobs(tmp_path, fake_profile, pgn):
    library = Library(tmp_path / "library.sqlite3")
    library.save_engine(fake_profile)
    library.set_setting("active_engine", fake_profile["id"])
    other_path = tmp_path / "new-fish"
    other_path.write_bytes(b"version two")
    second = {**fake_profile, "path": str(other_path), "sha256": file_digest(other_path),
              "id": "fish2", "commit": "b" * 40}
    library.save_engine(second)
    rules = SearchSettings(workers=1, first_move=1)
    first_id = library.create_scan("One", [Source.local(pgn)], GameFilter(), rules, fake_profile, 1)
    library.set_setting("active_engine", second["id"])
    second_id = library.create_scan("Two", [Source.local(pgn)], GameFilter(), rules, second, 1)
    assert library.scan(first_id)["profile"]["id"] == fake_profile["id"]
    assert library.scan(second_id)["profile"]["id"] == second["id"]
    with pytest.raises(ValueError, match="unfinished"):
        library.delete_engine(fake_profile["id"])
    assert f"#{first_id} One (running)" in library.engine_removal_block(fake_profile["id"])

    engines = []

    def make(profile, rules, cpu):
        engines.append(profile["id"])
        return ScriptedStockfish(profile, rules, cpu)

    runner = Runner(library, rules, engine_factory=make)
    try:
        for identity in (first_id, second_id):
            library.set_state(identity, "running")
        wait_for(lambda: all(library.scan(i)["verified"] == 1 for i in (first_id, second_id)))
        assert set(engines) == {fake_profile["id"], second["id"]}
        earlier, latest = [library.moments(row["id"])[0] for row in reversed(library.game_page())]
        assert earlier["detail"]["before_key"] != latest["detail"]["before_key"]
        for moment, probability in ((earlier, 0.05), (latest, 0.61)):
            scan_id = library.game(moment["scan_game"])["scan_id"]
            library.save_policy(f"before-{scan_id}", {moment["played"]: probability})
            library.save_policy(f"after-{scan_id}", {})
            job = library.connection().execute("SELECT id,scan_id,target FROM jobs WHERE stage='maia' AND target=?",
                                               (moment["id"],)).fetchone()
            library.finish_maia(dict(job), f"before-{scan_id}", f"after-{scan_id}", probability)
        assert [m["id"] for m in library.discovery_page()] == [latest["id"], earlier["id"]]
        assert library.frequency(earlier["position"])[earlier["played"]] == 1
        group, = library.discovery_groups(speed="rapid")
        assert (group["observed"], group["reached"], group["source_games"]) == (1, 1, 1)
        member, = library.group_members(group["position"], group["played"], speed="rapid")
        assert member["id"] == latest["id"] and member["probability"] == 0.61
    finally:
        runner.close()


@pytest.mark.stockfish
def test_real_stockfish_depth20_maia_pipeline_and_exports(tmp_path, pgn):
    from mistake_scanner.maia import runtime_installed

    executable = executable_from_env("STOCKFISH_EXE")
    import os

    checkpoint = Path(os.environ["MAIA_CHECKPOINT"]) if os.environ.get("MAIA_CHECKPOINT") else None
    library = Library(tmp_path / "library.sqlite3")
    profile = inspect_engine(executable)
    rules = SearchSettings(workers=2, first_move=1)
    runner = Runner(library, rules, checkpoint)
    try:
        live_board = chess.Board()
        live_board.push_san("e4")
        live = runner.analyze_position(profile, rules, live_board).result(timeout=20)
        assert len(live) == 5 and all(chess.Move.from_uci(line.pv[0]) in live_board.legal_moves for line in live)
        identity = library.create_scan("Depth 20", [Source.local(pgn)], GameFilter(), rules, profile, 1)
        def outcome():
            progress = library.scan_progress(identity)
            assert not progress["errors"], progress["errors"]
            return library.scan(identity)["verified"] > 0

        wait_for(outcome, timeout=80)
        game = library.game_page()[0]
        moment = next(m for m in library.moments(game["id"]) if m["ply"] == 6)
        if checkpoint and runtime_installed():
            wait_for(lambda: library.mistake(moment["id"])["state"] == "complete", timeout=60)
            moment = library.mistake(moment["id"])
            assert 0 < moment["probability"] < 1 and moment["maia_elo"] == 1800
        assert moment["detail"]["reason"] == "Allowed forced mate"
        before = library.cached_search(moment["detail"]["before_key"])
        after = library.cached_search(moment["detail"]["after_key"])
        assert all(line.depth >= 20 and line.nodes for line in before + after)
        assert len(before) == len(after) == 5
        assert after[0].pv[0] == "h5f7"
        assert library.evaluations(game["id"])[7]["score"] == {"cp": None, "mate": 0, "winner": 1}
        data = moment_data(library, moment["id"])
        assert data["analysis"]["engine"]["sha256"] == profile["sha256"]
        annotated = annotated_pgn(library, game["id"])
        assert "[AnalysisEngine " in annotated and "[%eval " in annotated and "Nf6" in annotated
        for kind in ("pgn", "original", "fen", "csv", "json", "puzzle"):
            output = tmp_path / f"export.{kind}"
            assert write_export(library, output, kind, games=[game["id"]], moments=[moment["id"]]) == 1
            assert output.stat().st_size > 10
    finally:
        runner.close()
