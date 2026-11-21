import hashlib
import io
from pathlib import Path

import httpx
import pytest
import zstandard

from mistake_scanner.acquisition import api_sources, archive_sources, open_pgn, parse_game_id, records
from mistake_scanner.model import GameFilter, Source
from mistake_scanner.network import IntegrityError, Network, fetch_file
from conftest import SAMPLE


def mock_network(handler, wait=lambda _: None):
    return Network(httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True), wait)


def test_metadata_filters_clock_annotation_and_variant_early():
    filtered = SAMPLE.replace("1843", "?")
    variant = SAMPLE.replace('[White "ExampleWhite"]', '[Variant "Atomic"]\n[White "ExampleWhite"]')
    text = io.StringIO(variant + filtered + SAMPLE)
    items = list(records(text, GameFilter(1700, 1900, clocks=True, speeds=("rapid",))))
    assert [item[2] for item in items] == ["variant", "filtered", "selected"]
    i, game, _ = items[-1]
    assert i == 3 and game["clocks"][0] == 603 and game["clocks"][1] is None
    assert game["white_elo"] == 1843 and game["black_elo"] == 1737
    assert game["day"] == "2025-01-20" and game["rated"]
    assert game["original"].rstrip() == SAMPLE.rstrip()
    either = list(records(io.StringIO(filtered), GameFilter(1700, 1900, "either")))
    assert either[0][1]["white_elo"] is None


def test_local_compressed_pgn_same_identity(tmp_path, pgn):
    compressed = tmp_path / "games.pgn.zst"
    compressed.write_bytes(zstandard.ZstdCompressor().compress(pgn.read_bytes()))
    network = mock_network(lambda _: pytest.fail("Local files should never use the network."))
    with network.client:
        extracted = []
        for path in (pgn, compressed):
            with open_pgn(Source.local(path), network) as stream:
                extracted.extend(records(stream, GameFilter()))
        assert extracted[0] == extracted[1]
        prior = Source.local(pgn)
        pgn.write_text("new data")
        with pytest.raises(ValueError, match="changed"), open_pgn(prior, network):
            pass


def test_discover_and_stream_archive_with_official_digest():
    filename = "lichess_db_standard_rated_2025-01.pgn.zst"
    compressed = zstandard.ZstdCompressor().compress(SAMPLE.encode())

    def handle(request):
        if request.url.path == "/":
            return httpx.Response(200, text=f'<a href="standard/{filename}">.pgn.zst</a>')
        if request.url.path.endswith("sha256sums.txt"):
            return httpx.Response(200, text=f"{hashlib.sha256(compressed).hexdigest()}  {filename}\n")
        assert request.url.path == f"/standard/{filename}"
        return httpx.Response(200, content=compressed)

    network = mock_network(handle)
    with network.client:
        source = archive_sources(network, "2025-01", "2025-01")[0]
        assert source.kind == "archive_stream" and source.immutable
        with open_pgn(source, network) as stream:
            imported = list(records(stream, GameFilter(), remote=True))
        assert imported[0][1]["lichess_id"] == "AbcD1234"


def test_api_endpoints_params_rate_limit_and_identity():
    calls, waits = [], []

    def request_handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "70"})
        return httpx.Response(200, text=SAMPLE.replace("lichess.org/AbcD1234", "lichess.org/game/AbcD1234"))

    filters = GameFilter(speeds=("rapid",), rated=True, since="2025-01-01", until="2025-02-01")
    assert parse_game_id("https://lichess.org/AbcD1234/black#45") == "AbcD1234"
    assert parse_game_id("https://lichess.org/AbcD1234abcd#45") == "AbcD1234"
    with pytest.raises(ValueError):
        parse_game_id("https://example.com/AbcD1234")
    network = mock_network(request_handler, waits.append)
    checked = []
    with network.client:
        for mode, value in (("user", "ExampleWhite"), ("ids", "AbcD1234, abcd1234"),
                             ("url", "https://lichess.org/game/AbcD1234")):
            source = api_sources(mode, value, filters)[0]
            with open_pgn(source, network) as stream:
                checked.append(list(records(stream, GameFilter(), remote=True))[0][1]["id"])
    assert len(set(checked)) == 1 and waits == [70]
    assert calls[1].url.path == "/api/games/user/ExampleWhite"
    assert calls[1].url.params["perfType"] == "rapid"
    assert calls[1].url.params["rated"] == "true"
    assert int(calls[1].url.params["since"]) == 1735689600000
    assert calls[2].method == "POST" and calls[2].content == b"AbcD1234,abcd1234"
    assert calls[3].url.path == "/game/export/AbcD1234"


def test_verified_range_download_and_checksum_failure(tmp_path):
    payload = b"abcdefghij"
    destination = tmp_path / "archive"
    part = destination.with_suffix(".part")
    part.write_bytes(payload[:4])
    requests = []

    def handle(request):
        requests.append(request.headers.get("Range"))
        return httpx.Response(206, headers={"Content-Range": "bytes 4-9/10"}, content=payload[4:])

    with mock_network(handle).client as client:
        network = Network(client, lambda _: None)
        expected = hashlib.sha256(payload).hexdigest()
        assert fetch_file(network, "https://example.com/archive", destination, sha256=expected, size=10) == destination
    assert requests == ["bytes=4-"] and destination.read_bytes() == payload and not part.exists()
    with mock_network(lambda _: httpx.Response(200, content=b"bad bytes!")).client as client:
        with pytest.raises(IntegrityError, match="SHA-256"):
            fetch_file(Network(client), "https://example.com/archive", destination, sha256="0" * 64, size=10)
    assert destination.read_bytes() == payload and not part.exists()


def test_remote_truncated_game_is_not_imported():
    incomplete = SAMPLE.replace("Qxf7# 1-0", "Qxf7#")
    assert list(records(io.StringIO(incomplete), GameFilter(), remote=True))[0][2] == "malformed"


def test_lichess_tournament_tags_and_authoritative_rated_filter():
    named_event = SAMPLE.replace("Rated Rapid game", "Sunday Arena")
    rated_event = named_event.replace('[BlackElo "1737"]', '[BlackElo "1737"]\n[BlackRatingDiff "-3"]')
    filters = GameFilter(rated=True)
    assert list(records(io.StringIO(named_event), filters, remote=True))[0][2] == "filtered"
    assert list(records(io.StringIO(named_event), filters, remote=True, known_rated=True))[0][1]["rated"]
    assert list(records(io.StringIO(rated_event), filters, remote=True))[0][1]["rated"]
