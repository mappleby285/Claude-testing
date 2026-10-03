"""Folder batch processing and the watch-folder worker.

Front/back pairing by file name: "smith_front.jpg" + "smith_back.jpg" (also "-front",
" front", ".front", case-insensitive) become one card. Everything else is one card per
image.

Watch mode polls the folder (reliable on Windows, network drives and cloud-synced
folders). A file is processed only after its size and modification time have stayed
unchanged for `stable_seconds` and it can be opened and decoded, so files that are
still being copied are not picked up early. Content hashes in the database make
restarts safe: already-processed images are recognised without an API call.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .imaging import ImageError, is_supported, verify_image
from .pipeline import Pipeline

log = logging.getLogger(__name__)

_SIDE_RE = re.compile(r"^(?P<base>.*?)[\s_\-.]*(?P<side>front|back)$", re.I)


def split_side(path: Path) -> tuple[str, str | None]:
    """('smith', 'front') for 'Smith_Front.jpg'; (stem, None) when no side marker."""
    m = _SIDE_RE.match(path.stem)
    if not m or not m.group("base"):
        return path.stem.casefold(), None
    return m.group("base").casefold(), m.group("side").lower()


def pair_by_name(paths: list[Path]) -> list[list[Path]]:
    """Group paths into cards: [front, back] pairs and single images, in a stable order."""
    by_key: dict[tuple[str, str], dict[str, Path]] = {}
    singles: list[Path] = []
    for p in sorted(paths, key=lambda x: str(x).casefold()):
        base, side = split_side(p)
        if side is None:
            singles.append(p)
            continue
        slot = by_key.setdefault((str(p.parent).casefold(), base), {})
        if side in slot:  # two "front" files with the same base: don't guess
            singles.append(p)
        else:
            slot[side] = p
    groups: list[list[Path]] = []
    for slot in by_key.values():
        if "front" in slot and "back" in slot:
            groups.append([slot["front"], slot["back"]])
        else:
            groups.extend([[p] for p in slot.values()])
    groups.extend([[p] for p in singles])
    groups.sort(key=lambda g: str(g[0]).casefold())
    return groups


def list_images(folder: Path, recursive: bool = False) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    return [
        p for p in Path(folder).glob(pattern)
        if p.is_file() and is_supported(p) and not p.name.startswith((".", "~$"))
    ]


def queue_folder(pipeline: Pipeline, folder: Path, recursive: bool = False, pair: bool = True) -> list:
    """Register all images in a folder as cards (without processing them yet)."""
    paths = list_images(folder, recursive)
    groups = pair_by_name(paths) if pair else [[p] for p in sorted(paths)]
    results = []
    for group in groups:
        try:
            results.append(pipeline.add_files(group, source=f"folder:{Path(folder).name}"))
        except OSError as exc:
            log.warning("Could not read %s (%s)", group[0].name, type(exc).__name__)
    return results


@dataclass
class _Seen:
    size: int
    mtime: float
    stable_since: float


@dataclass
class WatchStatus:
    running: bool = False
    folder: str = ""
    last_scan: float = 0.0
    waiting: int = 0
    added: int = 0
    identical: int = 0
    last_message: str = ""
    errors: list[str] = field(default_factory=list)


class FolderWatcher:
    def __init__(
        self,
        pipeline: Pipeline,
        folder: Path,
        recursive: bool = False,
        pair: bool = True,
        poll_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.pipeline = pipeline
        self.folder = Path(folder)
        self.recursive = recursive
        self.pair = pair
        self.poll_seconds = poll_seconds
        self.stable_seconds = pipeline.settings.watch_stable_seconds
        self.pair_wait = pipeline.settings.watch_pair_wait_seconds
        self.clock = clock
        self.status = WatchStatus(folder=str(self.folder))
        self._seen: dict[Path, _Seen] = {}
        self._done: dict[Path, tuple[int, float]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- one polling step (also used directly by tests) -------------------
    def _ready_files(self) -> list[Path]:
        now = self.clock()
        ready: list[Path] = []
        current = set()
        for p in list_images(self.folder, self.recursive):
            try:
                st = p.stat()
            except OSError:
                continue  # vanished or locked
            current.add(p)
            sig = (st.st_size, st.st_mtime)
            if self._done.get(p) == sig:
                continue
            seen = self._seen.get(p)
            if seen is None or (seen.size, seen.mtime) != sig:
                self._seen[p] = _Seen(st.st_size, st.st_mtime, now)
                continue
            if st.st_size > 0 and now - seen.stable_since >= self.stable_seconds:
                ready.append(p)
        for gone in set(self._seen) - current:
            self._seen.pop(gone, None)
        self.status.waiting = len([p for p in self._seen if p not in ready and self._done.get(p) is None])
        return ready

    def _readable(self, path: Path) -> bool:
        try:
            verify_image(path.read_bytes())
            return True
        except (OSError, ImageError):
            return False

    def scan_once(self) -> int:
        """Register newly completed files as cards and process the queue. Returns cards added."""
        ready = self._ready_files()
        now = self.clock()
        groups = pair_by_name(ready) if self.pair else [[p] for p in ready]
        added = 0
        for group in groups:
            if self.pair and len(group) == 1:
                _, side = split_side(group[0])
                waited = now - self._seen[group[0]].stable_since
                if side is not None and waited < self.pair_wait:
                    continue  # give the other side time to arrive
            if not all(self._readable(p) for p in group):
                # Stable but undecodable: maybe a slow copy. Give it one more stability
                # window, then let the pipeline record it as a corrupt image.
                if any(now - self._seen[p].stable_since < self.stable_seconds * 3 for p in group):
                    continue
            try:
                result = self.pipeline.add_files(group, source=f"watch:{self.folder.name}")
            except OSError as exc:
                self.status.errors.append(f"{group[0].name}: {type(exc).__name__}")
                continue
            for p in group:
                st = p.stat()
                self._done[p] = (st.st_size, st.st_mtime)
                self._seen.pop(p, None)
            if result.status == "identical":
                self.status.identical += 1
            else:
                added += 1
                self.status.added += 1
            self.status.last_message = result.message
        self.status.last_scan = time.time()
        if self.pipeline.store.list_cards(("queued",)):
            summary = self.pipeline.process_queue(stop=self._stop)
            if summary.stopped_reason and summary.stopped_reason != "Stopped by user":
                self.status.errors.append(summary.stopped_reason)
                self.status.last_message = summary.stopped_reason
        return added

    # -- thread control ------------------------------------------------------
    def _run(self) -> None:
        self.status.running = True
        log.info("Watching %s", self.folder)
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # keep watching
                log.exception("Watch scan failed")
                self.status.errors.append(f"Scan error: {type(exc).__name__}")
            self._stop.wait(self.poll_seconds)
        self.status.running = False

    def start(self) -> None:
        if not self.folder.is_dir():
            raise FileNotFoundError(f"Folder not found: {self.folder}")
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cardconv-watch", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self.status.running = False

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
