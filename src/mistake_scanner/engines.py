from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import chess
import chess.engine
import httpx

from .analysis import verify_engine_files
from .model import encode, file_digest, key
from .network import CHUNK, IntegrityError, Network, fetch_file

REPO = "https://github.com/official-stockfish/Stockfish"
API = "https://api.github.com/repos/official-stockfish/Stockfish"
NNUE = re.compile(r"nn-([a-f0-9]{12})\.nnue")
REVISION = re.compile(r"[a-f0-9]{40}")
PE_X64 = 0x8664
WINDOWS_HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOWS_BLOCKS = {225, 226, 577, 1260, 4551}
MAX_ZIP = 2 * 1024 ** 3


def native_windows() -> bool:
    return os.name == "nt" and platform.machine().lower() in ("amd64", "x86_64")


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str
    variant: str


@dataclass(frozen=True)
class Revision:
    sha: str
    date: str
    tag: str = ""
    published: str = ""
    release_url: str = ""
    asset: Asset | None = field(default=None)


def get_commit(network: Network, ref: str) -> Revision:
    data = json.loads(network.bytes(f"{API}/commits/{quote(ref, safe='')}"))
    sha = data["sha"]
    if not REVISION.fullmatch(sha):
        raise ValueError("GitHub did not return a full Stockfish commit SHA.")
    if re.fullmatch(r"[a-f0-9]{7,40}", ref) and not sha.startswith(ref):
        raise IntegrityError("GitHub resolved a different Stockfish commit.")
    timestamp = data["commit"]["committer"]["date"]
    datetime.fromisoformat(timestamp)
    return Revision(sha, timestamp)


def _asset(release: dict) -> Asset | None:
    supported = {"universal": 10, "x86-64": 2, "baseline": 2}
    if native_windows():
        feature = ctypes.windll.kernel32.IsProcessorFeaturePresent
        if feature(40):
            supported["avx2"] = 5
        if feature(37):
            supported["sse41-popcnt"] = 3
    candidates = []
    tag = release["tag_name"]
    for item in release.get("assets", []):
        name = item.get("name", "")
        match = re.fullmatch(r"stockfish[-_]windows[-_](?:x86[-_]64|x64|amd64)(?:[-_]([a-z0-9-]+))?\.zip",
                             name, re.IGNORECASE)
        if match is None:
            continue
        variant = (match[1] or "x86-64").lower()
        digest = item.get("digest") or ""
        url = f"{REPO}/releases/download/{quote(tag, safe='')}/{quote(name, safe='')}"
        size = item.get("size", 0)
        if variant not in supported or item.get("browser_download_url") != url:
            continue
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest) or not isinstance(size, int) or not 0 < size < MAX_ZIP:
            continue
        candidates.append((supported[variant], name, Asset(name, url, size, digest.removeprefix("sha256:"), variant)))
    return max(candidates, key=lambda candidate: candidate[:2])[2] if candidates else None


def latest_development(network: Network) -> Revision:
    releases = json.loads(network.bytes(f"{API}/releases?per_page=30"))
    compatible = [
        release for release in releases
        if release.get("prerelease") and not release.get("draft")
        and re.search(r"(?:^|[^a-z])dev(?:elopment)?(?:$|[^a-z])",
                      f"{release.get('tag_name', '')} {release.get('name') or ''}", re.IGNORECASE)
    ]
    if not compatible:
        raise ValueError("The official feed has no identified Stockfish development prerelease.")
    release = max(compatible, key=lambda item: item.get("published_at") or "")
    tag = release["tag_name"]
    match = re.search(r"[-_]([a-f0-9]{7,40})$", tag)
    try:
        commit = get_commit(network, tag)
    except httpx.HTTPStatusError as exc:
        if match is None or exc.response.status_code not in (404, 422):
            raise
        commit = get_commit(network, match[1])
    if match and not commit.sha.startswith(match[1]):
        raise IntegrityError("The official development tag and commit SHA disagree.")
    return Revision(commit.sha, commit.date, tag, release.get("published_at", ""),
                    f"{REPO}/releases/tag/{quote(tag, safe='')}", _asset(release))


def pe_machine(path: Path) -> int:
    with path.open("rb") as stream:
        header = stream.read(64)
        if not header.startswith(b"MZ") or len(header) < 64:
            raise ValueError("The executable is not a Windows PE file.")
        stream.seek(struct.unpack_from("<I", header, 60)[0])
        signature = stream.read(26)
        if (len(signature) < 26 or not signature.startswith(b"PE\0\0")
                or struct.unpack_from("<H", signature, 24)[0] != 0x20B):
            raise ValueError("The file lacks a valid 64-bit Windows PE header.")
        return struct.unpack_from("<H", signature, 4)[0]


def mark_download(path: Path, source_url: str, archive: Path | None = None) -> None:
    if os.name == "nt":
        original = None
        if archive is not None:
            try:
                original = Path(str(archive) + ":Zone.Identifier").read_bytes()
            except FileNotFoundError:
                pass
        mark = original or f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={source_url}\r\n".encode()
        Path(str(path) + ":Zone.Identifier").write_bytes(mark)


def extract_stockfish(archive: Path, target: Path, url: str) -> Path:
    executables = []
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > 20_000 or sum(entry.file_size for entry in entries) > MAX_ZIP:
            raise ValueError("The Stockfish ZIP exceeds expected extraction limits.")
        paths = set()
        for entry in entries:
            parts = PurePosixPath(entry.filename).parts
            mode = (entry.external_attr >> 16) & 0xF000
            if (not parts or entry.filename.startswith(("/", "\\")) or "\\" in entry.orig_filename
                    or any(part in ("..", ".") or not part.rstrip(" .") == part
                           or any(ch in part for ch in '<>:"|?*')
                           or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", part, re.I)
                           for part in parts)
                    or mode not in (0, stat.S_IFDIR, stat.S_IFREG)):
                raise ValueError(f"Unsafe ZIP member: {entry.filename}")
            relative = "/".join(parts).casefold()
            if relative in paths:
                raise ValueError(f"Duplicate ZIP member: {entry.filename}")
            paths.add(relative)
            output = target.joinpath(*parts)
            if entry.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, output.open("xb") as destination:
                    shutil.copyfileobj(source, destination, CHUNK)
                if output.suffix.lower() == ".exe" and output.name.lower().startswith("stockfish"):
                    mark_download(output, url, archive)
                    executables.append(output)
    x64 = [candidate for candidate in executables if pe_machine(candidate) == PE_X64]
    if len(x64) != 1:
        raise ValueError("The package did not contain exactly one Stockfish Windows x64 executable.")
    return x64[0]


def _startup_error(exc: OSError) -> RuntimeError:
    if getattr(exc, "winerror", None) in WINDOWS_BLOCKS:
        return RuntimeError(f"Windows security blocked the executable ({exc}). Use an allowed Stockfish build or consult Windows Security history.")
    if getattr(exc, "winerror", None) == 193:
        return RuntimeError(f"Windows cannot run this executable format or CPU architecture ({exc}).")
    if getattr(exc, "winerror", None) == 126:
        return RuntimeError(f"Windows could not load an executable dependency ({exc}).")
    return RuntimeError(f"Stockfish could not start: {exc}")


class EngineSearchError(RuntimeError):
    def __init__(self, message: str, required: list[tuple[str, str]]):
        super().__init__(message)
        self.required = required


def inspect_engine(path: Path, external: dict[str, Path] | None = None, **metadata) -> dict:
    path = path.resolve(strict=True)
    architecture = metadata.get("architecture")
    if os.name == "nt":
        if pe_machine(path) != PE_X64:
            raise ValueError("Choose a native Windows x64 Stockfish executable.")
        architecture = architecture or "Windows x64"
    if not path.is_file():
        raise ValueError("Select a Stockfish executable file.")
    files, options, reports = [], {}, []
    try:
        engine = chess.engine.SimpleEngine.popen_uci(str(path), cwd=path.parent, timeout=20, creationflags=WINDOWS_HIDDEN)
    except OSError as exc:
        raise _startup_error(exc) from exc
    try:
        version = engine.id.get("name", "")
        if not re.search(r"stockfish", version, re.IGNORECASE):
            raise ValueError(f"The UCI engine identifies as {version or 'unknown'}, which is not Stockfish.")
        for option in engine.options.values():
            if not option.name.lower().startswith("evalfile"):
                continue
            name = option.default
            if not isinstance(name, str) or name in ("", "<empty>"):
                continue
            file = (external or {}).get(option.name)
            bundled = path.parent / name
            if file is None and Path(name).name == name and bundled.is_file():
                file = bundled
            info = {"option": option.name, "name": name, "source": "Engine default"}
            if file is not None:
                file = file.resolve(strict=True)
                digest = file_digest(file)
                expected = NNUE.fullmatch(name)
                if expected and not digest.startswith(expected[1]):
                    raise IntegrityError(f"{file.name} does not match {name}, the NNUE required by this engine.")
                info.update(path=str(file), sha256=digest, source="External")
                options[option.name] = str(file)
            files.append(info)
        if options:
            engine.configure(options)
        engine.ping()
        result = engine.analyse(chess.Board(), chess.engine.Limit(depth=4))
        if "string" in result:
            reports.append(result["string"])
        if "score" not in result or not result.get("pv"):
            raise RuntimeError("The UCI engine returned no legal search line. Check its NNUE configuration.")
    except chess.engine.EngineTerminatedError as exc:
        code = engine.transport.get_returncode()
        status = code & 0xFFFFFFFF if code is not None else None
        if os.name == "nt":
            if status in WINDOWS_BLOCKS or status == 0xC0000428:
                raise RuntimeError(f"Windows security blocked Stockfish (status {status:#x}). Check Windows Security history.") from exc
            if status in (0xC0000135, 0xC000007B, 0xC000001D):
                raise RuntimeError(f"Stockfish could not run on this Windows installation (status {status:#x}). Check required DLLs and CPU compatibility.") from exc
        required = [(item["option"], item["name"]) for item in files if "path" not in item and NNUE.fullmatch(item["name"])]
        diagnostics = "\n".join(reports[-6:])
        raise EngineSearchError("Stockfish stopped during its UCI search. Check the required NNUE files."
                                + (f"\n{diagnostics}" if diagnostics else ""), required) from exc
    finally:
        engine.close()

    digest = file_digest(path)
    commit = metadata.get("commit")
    raw_revision = re.search(r"[0-9]{8}-([a-f0-9]{7,40})\b", version)
    if raw_revision and commit and not commit.startswith(raw_revision[1]):
        raise IntegrityError("Stockfish reported a different development revision from the download or source commit.")
    if metadata.get("origin") in ("official precompiled", "locally compiled"):
        for item in files:
            if item["source"] == "Engine default":
                item["source"] = "Embedded default"
    profile = {
        "name": version, "version": version, "path": str(path), "sha256": digest,
        "networks": files, "uci_options": options, "architecture": architecture or platform.machine(),
        "origin": "manual", "installed": datetime.now(timezone.utc).isoformat(), "validation": reports,
        **metadata,
    }
    profile["id"] = key([digest, commit, metadata.get("origin", "manual"), str(path) if not commit else None,
                         [(file["option"], file["name"], file.get("sha256")) for file in files], options])
    if profile.get("commit") and profile.get("commit_date"):
        day = datetime.fromisoformat(profile["commit_date"]).strftime("%m-%d-%Y")
        profile["name"] = f"Stockfish dev {day} ({profile['commit'][:8]})"
    return profile


def _download_network(network: Network, filename: str, destination: Path) -> str:
    signature = NNUE.fullmatch(filename)
    if signature is None:
        raise ValueError("Unsupported Stockfish NNUE filename.")
    if destination.is_file() and file_digest(destination).startswith(signature[1]):
        return file_digest(destination)

    def retrieve(url):
        digest = hashlib.sha256()
        size = 0
        with network.stream("GET", url) as response, partial.open("wb") as target:
            for chunk in response.iter_bytes(CHUNK):
                size += len(chunk)
                if size > 512 * 1024 ** 2:
                    raise IntegrityError("The NNUE network is unexpectedly large.")
                digest.update(chunk)
                target.write(chunk)
        if not digest.hexdigest().startswith(signature[1]):
            raise IntegrityError(f"The NNUE download does not match its official identifier {filename}.")
        partial.replace(destination)
        return digest.hexdigest()

    partial = destination.with_suffix(".part")
    try:
        try:
            return retrieve(f"https://tests.stockfishchess.org/api/nn/{filename}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (403, 404, 410):
                raise
            return retrieve(f"https://raw.githubusercontent.com/official-stockfish/networks/master/{filename}")
    finally:
        partial.unlink(missing_ok=True)


class EngineManager:
    def __init__(self, library, root: Path):
        self.library = library
        self.root = root.resolve() / "engines"
        self.root.mkdir(parents=True, exist_ok=True)

    def activate(self, profile: dict) -> dict:
        verify_engine_files(profile)
        self.library.set_setting("active_engine", profile["id"])
        return profile

    def register(self, path: Path, external: dict[str, Path] | None = None) -> dict:
        profile = inspect_engine(path, external)
        existing = self.library.engine(profile["id"])
        if existing is None:
            self.library.save_engine(profile)
        return existing or profile

    def _existing(self, **identity) -> dict | None:
        for profile in self.library.engines():
            if all(profile.get(name) == value for name, value in identity.items()):
                verify_engine_files(profile)
                return profile
        return None

    def _reuse(self, final: Path) -> dict | None:
        if not final.exists():
            return None
        marker = final / "profile.json"
        if not marker.is_file():
            raise ValueError(f"Installation directory already exists without a profile: {final}")
        profile = json.loads(marker.read_text(encoding="utf-8"))
        if Path(profile["path"]).resolve().is_relative_to(final.resolve()):
            verify_engine_files(profile)
            self.library.save_engine(profile)
            return profile
        raise ValueError("The installation profile references an executable outside its managed directory.")

    def _publish(self, source: Path, final: Path, profile: dict) -> dict:
        options = profile["uci_options"]
        for name, value in list(options.items()):
            path = Path(value)
            if path.is_relative_to(source):
                options[name] = str(final / path.relative_to(source))
        for item in profile["networks"]:
            if item.get("path") and Path(item["path"]).is_relative_to(source):
                item["path"] = str(final / Path(item["path"]).relative_to(source))
        profile["path"] = str(final / Path(profile["path"]).relative_to(source))
        profile["managed_directory"] = str(final)
        if profile.get("build_log"):
            profile["build_log"] = str(final / "build.log")
        (source / "profile.json").write_text(encode(profile), encoding="utf-8")
        source.rename(final)
        self.library.save_engine(profile)
        return profile

    def install(self, network: Network, revision: Revision, log=lambda _: None, transfer=lambda *_: None) -> dict:
        if not native_windows():
            raise ValueError("Installing the official Windows release requires Windows x64 for UCI validation.")
        asset = revision.asset
        if asset is None:
            raise ValueError("This development prerelease has no compatible, checksummed Windows x64 ZIP. Try a native source build.")
        identity = {"origin": "official precompiled", "commit": revision.sha, "package_sha256": asset.sha256}
        final = self.root / f"{revision.sha}-official-{asset.sha256[:12]}"
        existing = self._existing(**identity) or self._reuse(final)
        if existing is not None:
            log("Reusing the verified installation.")
            return existing
        archive = self.root / ".downloads" / f"{revision.sha}-{asset.name}"
        log(f"Downloading {asset.name}")
        fetch_file(network, asset.url, archive, sha256=asset.sha256, size=asset.size, progress=transfer)
        with tempfile.TemporaryDirectory(prefix=".unpack-", dir=self.root) as temporary:
            staging = Path(temporary) / "contents"
            staging.mkdir()
            log("Extracting the verified Windows x64 package")
            binary = extract_stockfish(archive, staging, asset.url)
            log("Validating UCI and NNUE")
            details = dict(identity, commit_date=revision.date, release=revision.tag, published=revision.published,
                           release_url=revision.release_url, architecture=f"x86-64 {asset.variant}")
            try:
                profile = inspect_engine(binary, **details)
            except EngineSearchError as exc:
                if not exc.required:
                    raise
                external = {}
                for option, filename in exc.required:
                    log(f"Retrieving required Stockfish NNUE {filename}")
                    target = binary.parent / filename
                    _download_network(network, filename, target)
                    external[option] = target
                profile = inspect_engine(binary, external, **details)
            result = self._publish(staging, final, profile)
        archive.unlink(missing_ok=True)
        return result

    def remove(self, profile: dict) -> None:
        if message := self.library.engine_removal_block(profile["id"]):
            raise ValueError(message)
        directory = profile.get("managed_directory")
        if directory:
            path = Path(directory)
            if path.is_symlink() or path.resolve().parent != self.root:
                raise ValueError("The installation's directory is outside the scanner engine folder.")
            marker = path / "profile.json"
            if (not marker.is_file() or json.loads(marker.read_text(encoding="utf-8"))["id"] != profile["id"]):
                raise ValueError("Cannot confirm the managed installation directory.")
            shutil.rmtree(path)
        self.library.delete_engine(profile["id"])

    def build(self, network: Network, commit: str = "", *, pgo: bool = True, jobs: int = 4,
              msys: Path | None = None, target: str = "native", log=lambda _: None) -> dict:
        if not native_windows():
            raise ValueError("Native source builds require Windows x64 and an MSYS2 MinGW-w64 toolchain.")
        if commit and not re.fullmatch(r"[a-f0-9]{7,40}", commit):
            raise ValueError("Enter an official Stockfish commit SHA or leave it blank for latest source.")
        if target not in ("native", "x86-64", "x86-64-avx2"):
            raise ValueError("Choose native, x86-64, or x86-64-avx2.")
        if target == "x86-64-avx2" and not ctypes.windll.kernel32.IsProcessorFeaturePresent(40):
            raise ValueError("Windows does not report usable AVX2 instructions for this system.")
        tools, env = windows_toolchain(msys)
        git = shutil.which("git")
        if git is None:
            raise ValueError("Install Git for Windows and make git.exe available on PATH.")
        revision = get_commit(network, commit or "master")
        mode = "profile-build" if pgo else "build"
        compiler = subprocess.check_output([tools["cxx"], "--version"], env=env, text=True, timeout=15,
                                           creationflags=WINDOWS_HIDDEN).splitlines()[0]
        cpu = platform.processor() if target == "native" else ""
        build_id = key([revision.sha, compiler, mode, target, cpu])[:16]
        final = self.root / f"{revision.sha}-native-{build_id}"
        identity = {"origin": "locally compiled", "commit": revision.sha, "compiler": compiler, "build_mode": mode,
                    "build_target": target, "build_cpu": cpu}
        old = self._existing(**identity) or self._reuse(final)
        if old:
            log("Reusing the verified native build.")
            return old
        log_path = self.root / f"{revision.sha}-{build_id}.log"
        with tempfile.TemporaryDirectory(prefix=".build-", dir=self.root) as temporary, log_path.open("w", encoding="utf-8") as output:
            workspace = Path(temporary)
            source = workspace / "Stockfish"
            source.mkdir()

            def report(line):
                output.write(line + "\n")
                output.flush()
                log(line)

            def run(args, directory=source):
                return _run(args, directory, env, report)

            log(f"Building official Stockfish commit {revision.sha}")
            run([git, "init", "--quiet"])
            run([git, "-c", "fetch.fsckObjects=true", "fetch", "--depth=1", REPO + ".git", revision.sha])
            run([git, "-c", "advice.detachedHead=false", "-c", "core.autocrlf=false", "checkout", "--detach", "FETCH_HEAD"])
            if run([git, "rev-parse", "HEAD"]).strip() != revision.sha:
                raise IntegrityError("The downloaded Stockfish source did not match the official commit.")
            src = source / "src"
            evaluate = (src / "evaluate.h").read_text(encoding="utf-8")
            networks = sorted(set(re.findall(r"nn-[0-9a-f]{12}\.nnue", evaluate)))
            if not networks:
                raise ValueError("This Stockfish commit has an unrecognized NNUE layout.")
            for filename in networks:
                report(f"Verifying NNUE {filename}")
                _download_network(network, filename, src / filename)
            make_jobs = min(max(1, jobs), max(1, (os.cpu_count() or 2) // 2))
            arch_script = source / "scripts" / "get_native_properties.sh"
            arch = target
            if target == "native" and arch_script.is_file():
                properties = run([tools["bash"], "../scripts/get_native_properties.sh"], src).split()
                if properties and re.fullmatch(r"x86-64(?:-[a-z0-9-]+)?", properties[0]):
                    arch = properties[0]
            # NNUE files were verified with TLS and the official hash prefix before invoking make.
            cmd = [tools["make"], "-o", "net", f"-j{make_jobs}", mode,
                   f"ARCH={arch}", "COMP=mingw", "CXX=g++", "SHELL=/usr/bin/bash"]
            run(cmd, src)
            executable = src / "stockfish.exe"
            if pe_machine(executable) != PE_X64:
                raise ValueError("The compiler did not produce a native Windows x64 executable.")
            result = run([str(executable), "bench"], src)
            if not re.search(r"Nodes searched\s*:\s*[1-9]\d*", result):
                raise RuntimeError("Stockfish's bench did not report completed search nodes.")
            stage = workspace / "install"
            stage.mkdir()
            binary = stage / "stockfish.exe"
            shutil.copy2(executable, binary)
            for name in ("Copying.txt", "AUTHORS"):
                if (source / name).is_file():
                    shutil.copy2(source / name, stage / name)
            output.flush()
            shutil.copyfile(log_path, stage / "build.log")
            rate = re.search(r"Nodes/second\s*:\s*(\d+)", result)
            profile = inspect_engine(binary, **identity, commit_date=revision.date, architecture=arch,
                                     build_command=cmd, build_log=str(stage / "build.log"),
                                     bench_nps=int(rate[1]) if rate else None)
            return self._publish(stage, final, profile)


def windows_toolchain(root: Path | None = None):
    root = root or Path(r"C:\msys64")
    usr = root / "usr" / "bin"
    for flavor in ("ucrt64", "mingw64"):
        native = root / flavor / "bin"
        tools = {"cxx": str(native / "g++.exe"), "make": str(usr / "make.exe"), "bash": str(usr / "bash.exe")}
        if all(Path(value).is_file() for value in tools.values()):
            env = dict(os.environ)
            env["PATH"] = os.pathsep.join([str(native), str(usr), env.get("PATH", "")])
            target = subprocess.check_output([tools["cxx"], "-dumpmachine"], text=True, env=env, timeout=15,
                                             creationflags=WINDOWS_HIDDEN).strip()
            if target == "x86_64-w64-mingw32":
                return tools, env
    raise ValueError("Install MSYS2 with GNU make and the UCRT64 or MINGW64 x64 GCC package. Select its installation folder.")


def _run(args: list[str], folder: Path, env: dict, log) -> str:
    log(" ".join(args))
    recent = []
    with subprocess.Popen(args, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace", creationflags=WINDOWS_HIDDEN) as proc:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            log(line)
            recent.append(line)
            if len(recent) > 120:
                del recent[:60]
        status = proc.wait()
    if status:
        raise RuntimeError(f"Command failed ({status}): {args[0]}\n" + "\n".join(recent[-8:]))
    return "\n".join(recent)
