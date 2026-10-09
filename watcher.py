"""Фоновая авто-индексация: watchdog следит за сохранениями и обновляет только изменённый файл."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ignore import is_ignored, load_ignore_spec
from indexer import PROJECT_ROOT, index_single_file, is_indexable_file, remove_indexed_file

DEBOUNCE_SECONDS = 0.6


class DebouncedIndexHandler(FileSystemEventHandler):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}
        self._spec = load_ignore_spec(PROJECT_ROOT)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._schedule(event)

    def on_created(self, event: FileSystemEvent) -> None:
        self._schedule(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if self._skip(path):
            return
        print(remove_indexed_file(path), flush=True)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = Path(event.src_path)
        if not self._skip(src):
            print(remove_indexed_file(src), flush=True)
        dest = Path(event.dest_path)
        if dest.exists():
            self._schedule_path(dest)

    def _schedule(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._schedule_path(Path(event.src_path))

    def _schedule_path(self, path: Path) -> None:
        if path.name == ".gitignore":
            self._spec = load_ignore_spec(PROJECT_ROOT)
        if self._skip(path):
            return
        key = str(path.resolve())
        with self._lock:
            existing = self._timers.pop(key, None)
            if existing:
                existing.cancel()
            timer = threading.Timer(DEBOUNCE_SECONDS, self._run, args=(path,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _skip(self, path: Path) -> bool:
        return is_ignored(PROJECT_ROOT, path, self._spec)

    def _run(self, path: Path) -> None:
        key = str(path.resolve())
        with self._lock:
            self._timers.pop(key, None)
        if not path.exists():
            print(remove_indexed_file(path), flush=True)
            return
        if not is_indexable_file(path):
            return
        print(index_single_file(path), flush=True)


def watch() -> None:
    handler = DebouncedIndexHandler()
    observer = Observer()
    observer.schedule(handler, str(PROJECT_ROOT), recursive=True)
    observer.start()
    print(f"Watcher запущен: {PROJECT_ROOT}", flush=True)
    print("Сохраните файл (Ctrl+S) — в индекс попадёт только он.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    watch()
