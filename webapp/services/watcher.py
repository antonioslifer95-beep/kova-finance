"""Watchdog file system observer — auto-syncs new/modified files."""
import threading, time
from pathlib import Path
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
from config import BASE_DIR, DOC_EXTS, SKIP_NAMES

_pending: dict = {}
_lock = threading.Lock()

class _Handler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            self._debounce(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._debounce(event.src_path)

    def _debounce(self, path: str):
        p = Path(path)
        if p.suffix.lower() not in DOC_EXTS:
            return
        # Skip anything inside the webapp or hidden folders
        try:
            rel = p.relative_to(BASE_DIR)
            if rel.parts[0].lower() in SKIP_NAMES:
                return
        except ValueError:
            return
        with _lock:
            _pending[path] = time.time()

def _flush_loop(scanner_sync, indexer_single):
    while True:
        time.sleep(3)
        now = time.time()
        with _lock:
            ready = [p for p, t in _pending.items() if now - t >= 3]
            for p in ready:
                del _pending[p]
        for path in ready:
            try:
                p = Path(path)
                # Resync the client folder
                client_folder = p.parent
                while client_folder.parent != BASE_DIR and client_folder.parent != client_folder:
                    client_folder = client_folder.parent
                if client_folder.parent == BASE_DIR:
                    scanner_sync(client_folder)
                indexer_single(path)
            except Exception as e:
                print(f"[watcher] Error processing {path}: {e}")

_observer = None

def start(scanner_sync_fn, indexer_single_fn):
    global _observer
    _observer = PollingObserver(timeout=5)
    _observer.schedule(_Handler(), str(BASE_DIR), recursive=True)
    _observer.start()
    t = threading.Thread(target=_flush_loop, args=(scanner_sync_fn, indexer_single_fn), daemon=True)
    t.start()
    print(f"[watcher] Watching {BASE_DIR}")

def stop():
    if _observer:
        _observer.stop()
        _observer.join()
