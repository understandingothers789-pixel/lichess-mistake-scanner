import os

import pytest

from mistake_scanner.acquisition import api_sources, archive_sources, open_pgn, records
from mistake_scanner.model import GameFilter
from mistake_scanner.network import Network


@pytest.mark.skipif(os.environ.get("LIVE_LICHESS") != "1", reason="Set LIVE_LICHESS=1 for public-network checks.")
def test_live_lichess_archive_game_and_user_export():
    network = Network()
    try:
        archive = archive_sources(network, "2013-01", "2013-01")[0]
        assert archive.params["sha256"]
        with open_pgn(archive, network) as text:
            _, first, reason = next(records(text, GameFilter(), remote=True))
        assert reason == "selected" and first["moves"] and first["lichess_id"]
        source = api_sources("url", first["site"], GameFilter())[0]
        with open_pgn(source, network) as text:
            _, response, _ = next(records(text, GameFilter(), remote=True))
        assert response["moves"] == first["moves"] and response["id"] == first["id"]
        source = api_sources("ids", first["lichess_id"], GameFilter())[0]
        with open_pgn(source, network) as text:
            _, by_id, _ = next(records(text, GameFilter(), remote=True))
        assert by_id["id"] == first["id"]
        source = api_sources("user", "DrNykterstein", GameFilter(rated=True, speeds=("blitz",)), max=1)[0]
        with open_pgn(source, network) as text:
            _, response, reason = next(records(text, GameFilter(), remote=True))
        assert reason == "selected" and response["rated"] and response["speed"] == "blitz"
        assert "drnykterstein" in (response["white"].lower(), response["black"].lower())
    finally:
        network.close()
