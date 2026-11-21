"""Run Second Look with a normal Python installation and a local virtual environment."""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-maia", action="store_true", help="Install the optional Maia CPU runtime")
    parser.add_argument("--check", action="store_true", help="Set up dependencies and test Qt startup")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12) or struct.calcsize("P") != 8:
        parser.error("Install Python 3.12 x64 from python.org and run py -3.12 launch.py.")

    environment = ROOT / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def run(*command):
        subprocess.run([str(python), *command], cwd=ROOT, check=True)

    try:
        if not python.is_file():
            venv.EnvBuilder(with_pip=True).create(environment)

        stamp = environment / "second-look-installed.toml"
        manifest = (ROOT / "pyproject.toml").read_bytes()
        if not stamp.exists() or stamp.read_bytes() != manifest:
            run("-m", "pip", "install", ".")
            stamp.write_bytes(manifest)

        if args.with_maia:
            run("-m", "pip", "install", "--index-url", "https://download.pytorch.org/whl/cpu", "torch==2.8.0+cpu")
            run("-m", "pip", "install", ".[maia]")
            run("-c", "import torch, maia3")

        if args.check:
            os.environ["QT_QPA_PLATFORM"] = "offscreen"
            run("-c", "from PySide6.QtWidgets import QApplication; "
                "from mistake_scanner.gui import Window; "
                "from pathlib import Path; from tempfile import TemporaryDirectory; "
                "app = QApplication([]); folder = TemporaryDirectory(); "
                "window = Window(Path(folder.name)); window.show(); app.processEvents(); "
                "window.close(); app.exec(); folder.cleanup()")
            print("Second Look started and closed successfully.")
        else:
            run("-m", "mistake_scanner")
        return 0
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Second Look launcher stopped: {exc}", file=sys.stderr)
        print("If Windows Security names a blocked component, keep that protection enabled and note the file name.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
