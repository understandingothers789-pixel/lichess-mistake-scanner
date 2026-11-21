"""Optional integration check for a disposable native Windows CI runner."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mistake_scanner.database import Library
from mistake_scanner.engines import PE_X64, EngineManager, latest_development, pe_machine
from mistake_scanner.network import Network


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--msys", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This is a native Windows integration check.")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = Library(root / "library.sqlite3")
        manager = EngineManager(database, root)
        network = Network()
        try:
            if args.compile:
                profile = manager.build(network, pgo=False, msys=args.msys, target="x86-64", log=print)
                assert profile["bench_nps"] > 0
            else:
                release = latest_development(network)
                profile = manager.install(network, release, log=print)
                assert manager.install(network, release, log=print)["id"] == profile["id"]
            manager.activate(profile)
            assert pe_machine(Path(profile["path"])) == PE_X64
            env = {**os.environ, "STOCKFISH_EXE": profile["path"]}
            subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "stockfish"], check=True, env=env)
        finally:
            network.close()
            database.close_thread()


if __name__ == "__main__":
    main()
