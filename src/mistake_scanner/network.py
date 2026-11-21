from __future__ import annotations

import io
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from .model import file_digest

CHUNK = 256 * 1024
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class Interrupted(Exception):
    """An acquisition was paused or canceled."""


class IntegrityError(ValueError):
    pass


class Network:
    def __init__(self, client: httpx.Client | None = None, wait=time.sleep):
        self.client = client or httpx.Client(
            headers={"User-Agent": "SecondLookChess/0.1 (local chess analysis)", "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(30, connect=15), follow_redirects=True,
        )
        self.wait = wait

    def close(self) -> None:
        self.client.close()

    def delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        default = 60 if response is not None and response.status_code == 429 else 2 ** attempt
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            try:
                return max(default, float(retry_after))
            except ValueError:
                try:
                    date = parsedate_to_datetime(retry_after)
                    return max(default, (date - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError):
                    pass
        return default

    @contextmanager
    def stream(self, method: str, url: str, *, accepted: tuple[int, ...] = (), **request):
        for attempt in range(4):
            try:
                response = self.client.send(self.client.build_request(method, url, **request), stream=True)
            except httpx.TransportError:
                if attempt == 3:
                    raise
                self.wait(self.delay(attempt))
                continue
            if response.status_code in RETRYABLE and attempt < 3:
                response.close()
                self.wait(self.delay(attempt, response))
                continue
            break
        try:
            if response.status_code not in accepted:
                response.raise_for_status()
            yield response
        finally:
            response.close()

    def bytes(self, url: str, limit: int = 8 * 1024 * 1024) -> bytes:
        for attempt in range(3):
            try:
                with self.stream("GET", url) as response:
                    data = bytearray()
                    for chunk in response.iter_bytes(CHUNK):
                        data.extend(chunk)
                        if len(data) > limit:
                            raise ValueError("Server response exceeded its size limit.")
                    return bytes(data)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                self.wait(self.delay(attempt))
        raise AssertionError("Unreachable")


class ChunkReader(io.RawIOBase):
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.remaining = memoryview(b"")

    def readable(self) -> bool:
        return True

    def readinto(self, target) -> int:
        while not self.remaining:
            batch = next(self.chunks, None)
            if batch is None:
                return 0
            self.remaining = memoryview(batch)
        size = min(len(target), len(self.remaining))
        target[:size] = self.remaining[:size]
        self.remaining = self.remaining[size:]
        return size


def fetch_file(
    network: Network, url: str, destination: Path, *, sha256: str, size: int | None = None,
    limit: int | None = None, progress=lambda *_: None,
) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise IntegrityError("No authoritative SHA-256 checksum was provided.")
    if destination.is_symlink():
        raise ValueError(f"Download destination must be a regular file: {destination}")
    if destination.is_file() and (size is None or destination.stat().st_size == size):
        if file_digest(destination) == sha256:
            return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    if part.is_symlink():
        raise ValueError(f"Partial download must be a regular file: {part}")
    if limit is not None and size is not None and size > limit:
        raise ValueError("The download exceeds the configured disk limit.")
    if part.is_file() and size is not None and part.stat().st_size > size:
        part.unlink()

    for attempt in range(4):
        offset = part.stat().st_size if part.exists() else 0
        if size is not None and offset == size and part.exists():
            break
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with network.stream("GET", url, headers=headers, accepted=(416,)) as response:
                if response.status_code == 416:
                    if offset and re.fullmatch(f"bytes \\*/{offset}", response.headers.get("Content-Range", "")):
                        break
                    part.unlink(missing_ok=True)
                    if attempt == 3:
                        raise IntegrityError("The server refused the download range.")
                    continue

                content_range = response.headers.get("Content-Range", "")
                remaining = response.headers.get("Content-Length", "")
                total = int(remaining) if remaining.isdigit() else None
                if response.status_code == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if not match or int(match[1]) != offset or int(match[2]) + 1 != int(match[3]):
                        raise IntegrityError("Unexpected Content-Range for the requested download.")
                    total = int(match[3])
                else:
                    offset = 0
                if size is not None and total is not None and size != total:
                    raise IntegrityError(f"Server advertised {total:,} bytes; expected {size:,}.")
                if limit is not None and total is not None and total > limit:
                    raise ValueError("The download exceeds the configured disk limit.")
                with part.open("ab" if offset else "wb") as target:
                    progress(offset, size or total)
                    for chunk in response.iter_bytes(CHUNK):
                        offset += len(chunk)
                        if limit is not None and offset > limit:
                            raise ValueError("The download reached the configured disk limit.")
                        if size is not None and offset > size:
                            raise IntegrityError("The download exceeded its published size.")
                        target.write(chunk)
                        progress(offset, size or total)
            break
        except httpx.TransportError:
            if attempt == 3:
                raise
            network.wait(network.delay(attempt))
        except IntegrityError:
            part.unlink(missing_ok=True)
            raise

    if not part.is_file() or (size is not None and part.stat().st_size != size) or file_digest(part) != sha256:
        part.unlink(missing_ok=True)
        raise IntegrityError("SHA-256 verification failed. The incomplete or invalid file was discarded.")
    part.replace(destination)
    return destination
