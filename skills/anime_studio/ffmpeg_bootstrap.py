"""Auto-download static ffmpeg/ffprobe binaries when not found on the system.

Checks system PATH first, then a local ``state_dir/bin/`` cache, and falls
back to downloading platform-appropriate static builds from
ffmpeg.martin-riedl.de.  Thread-safe; concurrent callers block on the same
lock rather than racing downloads.

Integrity: after extracting each binary, its SHA256 is computed and persisted
to ``state_dir/bin/{tool}.sha256``.  On subsequent loads the stored hash is
verified before execution.  The download source is HTTPS (transport-protected);
the stored hash detects post-download corruption or tampering on disk.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import httpx

log = logging.getLogger("anime_studio.ffmpeg_bootstrap")

_lock = threading.Lock()

_DOWNLOAD_TIMEOUT = 300  # seconds

_BASE_URL = "https://ffmpeg.martin-riedl.de/redirect/latest"

ProgressCallback = Optional[Callable[[int, Optional[int], str], None]]


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _platform_segment() -> Tuple[str, str]:
    """Return ``(os_segment, arch_segment)`` for the current platform."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        os_seg = "macos"
    elif system == "linux":
        os_seg = "linux"
    else:
        raise RuntimeError(f"Unsupported OS for ffmpeg bootstrap: {system}")

    if machine in ("arm64", "aarch64"):
        arch_seg = "arm64"
    elif machine in ("x86_64", "amd64"):
        arch_seg = "amd64"
    else:
        raise RuntimeError(f"Unsupported architecture for ffmpeg bootstrap: {machine}")

    return os_seg, arch_seg


def _download_url(tool: str) -> str:
    """Build the redirect URL for *tool* (``ffmpeg`` or ``ffprobe``)."""
    os_seg, arch_seg = _platform_segment()
    return f"{_BASE_URL}/{os_seg}/{arch_seg}/release/{tool}.zip"


# ---------------------------------------------------------------------------
# Lookup helpers (no download)
# ---------------------------------------------------------------------------

def _configured_path(tool: str) -> Optional[str]:
    """Check if a pre-configured local path exists in environment variables."""
    val = os.environ.get(f"{tool.upper()}_PATH")
    if val:
        candidate = Path(val)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            log.debug("Found pre-configured %s at %s", tool, val)
            return str(candidate)
    return None


def _system_path(tool: str) -> Optional[str]:
    path = shutil.which(tool)
    if path:
        log.debug("Found system %s at %s", tool, path)
    return path


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _save_hash(bin_dir: Path, tool: str, digest: str) -> None:
    """Persist the SHA256 of a downloaded binary for later verification."""
    (bin_dir / f"{tool}.sha256").write_text(digest)


def _verify_hash(bin_dir: Path, tool: str, binary_path: Path) -> bool:
    """Verify a cached binary against its stored SHA256. Returns False on mismatch or missing hash."""
    hash_path = bin_dir / f"{tool}.sha256"
    if not hash_path.exists():
        return False
    stored = hash_path.read_text().strip()
    actual = _sha256_file(binary_path)
    if stored != actual:
        log.warning("SHA256 mismatch for %s: expected %s, got %s", tool, stored, actual)
        return False
    return True


def _local_path(bin_dir: Path, tool: str) -> Optional[str]:
    candidate = bin_dir / tool
    if candidate.is_file() and os.access(candidate, os.X_OK):
        if _verify_hash(bin_dir, tool, candidate):
            log.debug("Found cached %s at %s (hash verified)", tool, candidate)
            return str(candidate)
        log.warning("Cached %s at %s failed hash verification — will re-download", tool, candidate)
        return None
    return None


def get_ffmpeg_path(state_dir: str | Path) -> Optional[str]:
    """Return ffmpeg path (pre-configured, system, or local cache) without downloading."""
    return _configured_path("ffmpeg") or _system_path("ffmpeg") or _local_path(Path(state_dir) / "bin", "ffmpeg")


def get_ffprobe_path(state_dir: str | Path) -> Optional[str]:
    """Return ffprobe path (pre-configured, system, or local cache) without downloading."""
    return _configured_path("ffprobe") or _system_path("ffprobe") or _local_path(Path(state_dir) / "bin", "ffprobe")


# ---------------------------------------------------------------------------
# Download + extract
# ---------------------------------------------------------------------------

def _download_and_extract(
    tool: str,
    bin_dir: Path,
    on_progress: ProgressCallback = None,
) -> str:
    """Download a static *tool* binary into *bin_dir* and return its path."""
    url = _download_url(tool)
    dest = bin_dir / tool

    log.info("Downloading %s from %s", tool, url)

    with httpx.Client(follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = (
                int(resp.headers["content-length"])
                if "content-length" in resp.headers
                else None
            )

            # Stream into a temp file next to the final destination so the
            # rename is atomic on the same filesystem.
            fd, tmp_zip = tempfile.mkstemp(dir=bin_dir, suffix=f".{tool}.zip")
            try:
                downloaded = 0
                with os.fdopen(fd, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if on_progress:
                            on_progress(downloaded, total, tool)

                # Extract the single binary from the zip.
                with zipfile.ZipFile(tmp_zip) as zf:
                    names = zf.namelist()
                    # The zip contains a single binary at the root level.
                    binary_name = next(
                        (n for n in names if os.path.basename(n) == tool), None
                    )
                    if binary_name is None:
                        raise RuntimeError(
                            f"Expected '{tool}' inside zip, got: {names}"
                        )
                    # Extract to a temp file then rename.
                    fd2, tmp_bin = tempfile.mkstemp(dir=bin_dir, suffix=f".{tool}")
                    try:
                        with os.fdopen(fd2, "wb") as out, zf.open(binary_name) as src:
                            shutil.copyfileobj(src, out)
                        os.chmod(tmp_bin, 0o755)
                        # Compute and persist SHA256 before final placement
                        digest = _sha256_file(Path(tmp_bin))
                        os.replace(tmp_bin, dest)
                        _save_hash(bin_dir, tool, digest)
                        log.info("SHA256(%s) = %s", tool, digest)
                    except BaseException:
                        # Clean up temp binary on failure.
                        with _suppress():
                            os.unlink(tmp_bin)
                        raise
            finally:
                # Always clean up the temp zip.
                with _suppress():
                    os.unlink(tmp_zip)

    log.info("Installed %s to %s", tool, dest)
    return str(dest)


class _suppress:
    """Tiny context manager that swallows OSError (avoids contextlib import)."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return issubclass(exc[0], OSError) if exc[0] else False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def ensure_ffmpeg(
    state_dir: str | Path,
    on_progress: ProgressCallback = None,
) -> Dict[str, str]:
    """Return ``{"ffmpeg": path, "ffprobe": path}``, downloading if needed.

    Thread-safe: concurrent callers serialize on a module-level lock so at
    most one download runs at a time.
    """
    state_dir = Path(state_dir)
    bin_dir = state_dir / "bin"
    result: Dict[str, str] = {}

    for tool in ("ffmpeg", "ffprobe"):
        # 0. Pre-configured environment variable path
        config_path = _configured_path(tool)
        if config_path:
            result[tool] = config_path
            continue

        # 1. System PATH
        sys_path = _system_path(tool)
        if sys_path:
            result[tool] = sys_path
            continue

        # 2. Local cache (fast path, no lock needed for a read)
        local = _local_path(bin_dir, tool)
        if local:
            result[tool] = local
            continue

        # 3. Download (serialized)
        with _lock:
            # Re-check after acquiring: another thread may have finished.
            local = _local_path(bin_dir, tool)
            if local:
                result[tool] = local
                continue
            bin_dir.mkdir(parents=True, exist_ok=True)
            result[tool] = _download_and_extract(tool, bin_dir, on_progress)

    return result
