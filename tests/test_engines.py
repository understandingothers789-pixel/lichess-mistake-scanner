import hashlib
import io
import os
import stat
import struct
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

from mistake_scanner import engines
from mistake_scanner.database import Library
from mistake_scanner.model import file_digest
from mistake_scanner.network import IntegrityError, Network
from conftest import executable_from_env


def pe_header(machine=engines.PE_X64):
    dos = bytearray(64)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 60, 64)
    pe = bytearray(26)
    pe[:4] = b"PE\0\0"
    struct.pack_into("<H", pe, 4, machine)
    struct.pack_into("<H", pe, 24, 0x20B)
    return bytes(dos + pe)


def zip_bytes(name, contents):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, contents)
    return output.getvalue()


def network(handler):
    return Network(httpx.Client(transport=httpx.MockTransport(handler)), lambda _: None)


def feed(sha, digest, *, release_name=None):
    tag = release_name or f"stockfish-dev-20261120-{sha[:8]}"
    filename = "stockfish-windows-x86-64-universal.zip"
    return {
        "prerelease": True, "tag_name": tag, "name": "Stockfish dev", "published_at": "2026-11-20T16:31:43Z",
        "assets": [
            {"name": filename, "browser_download_url": f"{engines.REPO}/releases/download/{tag}/{filename}",
             "size": 700, "digest": digest},
            {"name": "stockfish-windows-arm64-universal.zip", "size": 4, "digest": digest},
        ],
    }


def test_latest_official_release_resolves_full_commit_date_and_asset():
    sha = "1819b9ea" + "a" * 32
    release = feed(sha, "sha256:" + "e" * 64)
    old = feed("b" * 40, "sha256:" + "d" * 64)
    old["published_at"] = "2026-11-19T14:00:00Z"
    stable = {**release, "prerelease": False, "published_at": "2026-11-21T00:00:00Z"}
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("releases"):
            return httpx.Response(200, json=[stable, old, release])
        assert request.url.path.endswith(release["tag_name"])
        return httpx.Response(200, json={"sha": sha, "commit": {"committer": {"date": "2026-11-20T14:43:00Z"}}})

    with network(respond).client as client:
        result = engines.latest_development(Network(client))
    assert len(calls) == 2
    assert result.sha == sha and result.date == "2026-11-20T14:43:00Z"
    assert result.asset is not None and result.asset.variant == "universal"
    assert result.asset.sha256 == "e" * 64 and result.asset.size == 700
    assert result.published == release["published_at"]


def test_reject_mismatched_commit_and_require_official_asset_metadata():
    release = feed("a" * 40, None)
    assert engines._asset(release) is None

    def respond(request):
        return httpx.Response(200, json=[release] if request.url.path.endswith("releases") else {
            "sha": "b" * 40, "commit": {"committer": {"date": "2026-11-20T14:43:00Z"}},
        })

    with network(respond).client as client, pytest.raises(IntegrityError, match="disagree"):
        engines.latest_development(Network(client))


@pytest.mark.parametrize("name", ["../bad", "/bad", "bad\\file", "C:/bad", "safe/con.txt", "safe/engine.exe:stream"])
def test_extraction_rejects_unsafe_windows_zip_paths(tmp_path, name):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(zip_bytes(name, b"payload"))
    with pytest.raises(ValueError, match="Unsafe ZIP"):
        engines.extract_stockfish(archive, tmp_path / "staging", "https://example.test")


def test_zip_symlinks_and_incorrect_windows_machine(tmp_path):
    archive = tmp_path / "bad.zip"
    symlink = zipfile.ZipInfo("stockfish-link")
    symlink.create_system = 3
    symlink.external_attr = stat.S_IFLNK << 16
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr(symlink, "../outside")
    with pytest.raises(ValueError, match="Unsafe ZIP"):
        engines.extract_stockfish(archive, tmp_path / "stage", "https://example.test")
    archive.write_bytes(zip_bytes("stockfish/stockfish-arm.exe", pe_header(0xAA64)))
    with pytest.raises(ValueError, match="Windows x64"):
        engines.extract_stockfish(archive, tmp_path / "other-stage", "https://example.test")


def test_install_reuses_verified_revision_preserves_history(tmp_path, monkeypatch):
    library = Library(tmp_path / "library.sqlite3")
    manager = engines.EngineManager(library, tmp_path)
    monkeypatch.setattr(engines, "native_windows", lambda: True)
    monkeypatch.setattr(engines, "mark_download", lambda *_: None)
    calls = []

    def inspect(path, **details):
        assert engines.pe_machine(path) == engines.PE_X64
        return {**details, "id": details["commit"], "path": str(path), "sha256": file_digest(path),
                "networks": [], "uci_options": {}, "name": "Stockfish", "installed": details["commit_date"]}

    monkeypatch.setattr(engines, "inspect_engine", inspect)

    def install(char):
        data = zip_bytes("stockfish/stockfish.exe", pe_header() + char.encode())
        asset = engines.Asset("stockfish-windows-x86-64-universal.zip", "https://example.test/archive",
                              len(data), hashlib.sha256(data).hexdigest(), "universal")
        revision = engines.Revision(char * 40, "2026-11-20T14:43:00Z", char, asset=asset)
        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=data)

        with network(handler).client as client:
            return manager.install(Network(client), revision)

    first = install("a")
    assert install("a") == first
    second = install("b")
    assert len(calls) == 2
    assert {p["commit"] for p in library.engines()} == {"a" * 40, "b" * 40}
    assert Path(first["path"]).is_file() and Path(second["path"]).is_file()
    manager.activate(second)
    assert library.setting("active_engine") == second["id"]
    manager.remove(first)
    assert not Path(first["path"]).exists() and Path(second["path"]).is_file()


def test_nnue_integrity_and_build_failure_output(tmp_path):
    data = b"official network bytes for integrity regression"
    sha = hashlib.sha256(data).hexdigest()
    filename = f"nn-{sha[:12]}.nnue"
    output = tmp_path / filename
    with network(lambda _: httpx.Response(200, content=data)).client as client:
        assert engines._download_network(Network(client), filename, output) == sha
    assert output.read_bytes() == data
    output.unlink()
    with network(lambda _: httpx.Response(200, content=b"corrupted")).client as client:
        with pytest.raises(IntegrityError, match="official identifier"):
            engines._download_network(Network(client), filename, output)
    assert not output.exists()
    with pytest.raises(ValueError, match="MSYS2"):
        engines.windows_toolchain(tmp_path)
    logs = []
    with pytest.raises(RuntimeError, match="compiler detail"):
        engines._run([sys.executable, "-c", "print('compiler detail'); raise SystemExit(3)"], tmp_path, os.environ, logs.append)
    assert logs[-1] == "compiler detail"


@pytest.mark.stockfish
def test_validate_real_uci_fingerprint_and_detect_changed_binary():
    profile = engines.inspect_engine(executable_from_env("STOCKFISH_EXE"))
    assert profile["version"].lower().startswith("stockfish")
    assert profile["sha256"] == file_digest(Path(profile["path"]))
    assert profile["networks"] and profile["networks"][0]["name"].endswith(".nnue")
    with pytest.raises(ValueError, match="changed"):
        engines.verify_engine_files({**profile, "sha256": "a" * 64})


@pytest.mark.windows
def test_real_official_package_windows_architecture(tmp_path):
    archive = executable_from_env("STOCKFISH_WINDOWS_ZIP")
    exe = engines.extract_stockfish(archive, tmp_path, engines.REPO)
    assert engines.pe_machine(exe) == engines.PE_X64
    if os.name == "nt":
        assert "stockfish" in engines.inspect_engine(exe)["version"].lower()
