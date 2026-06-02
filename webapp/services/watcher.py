"""Watchdog file system observer — auto-syncs new, modified, moved and deleted files."""
import threading, time
from pathlib import Path
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
from config import BASE_DIR, DOC_EXTS, SKIP_NAMES

_pending:         dict = {}   # path -> timestamp  (create / modify)
_pending_deletes: dict = {}   # rel_path -> timestamp
_lock = threading.Lock()


def _is_tracked(path: str) -> bool:
    p = Path(path)
    if p.suffix.lower() not in DOC_EXTS:
        return False
    try:
        rel = p.relative_to(BASE_DIR)
        return rel.parts[0].lower() not in SKIP_NAMES
    except ValueError:
        return False


class _Handler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and _is_tracked(event.src_path):
            with _lock:
                _pending[event.src_path] = time.time()

    def on_modified(self, event):
        if not event.is_directory and _is_tracked(event.src_path):
            with _lock:
                _pending[event.src_path] = time.time()

    def on_deleted(self, event):
        if not event.is_directory and _is_tracked(event.src_path):
            try:
                rel = str(Path(event.src_path).relative_to(BASE_DIR))
                with _lock:
                    _pending_deletes[rel] = time.time()
            except ValueError:
                pass

    def on_moved(self, event):
        if event.is_directory:
            return
        # Treat as delete-source + create-destination
        if _is_tracked(event.src_path):
            try:
                rel = str(Path(event.src_path).relative_to(BASE_DIR))
                with _lock:
                    _pending_deletes[rel] = time.time()
            except ValueError:
                pass
        if _is_tracked(event.dest_path):
            with _lock:
                _pending[event.dest_path] = time.time()


def _flush_loop(scanner_sync, indexer_single):
    while True:
        time.sleep(3)
        now = time.time()
        with _lock:
            ready = [p for p, t in _pending.items() if now - t >= 3]
            for p in ready:
                del _pending[p]
            ready_deletes = [r for r, t in _pending_deletes.items() if now - t >= 3]
            for r in ready_deletes:
                del _pending_deletes[r]

        # Handle deletions — remove from DB immediately
        if ready_deletes:
            try:
                from database import get_db
                with get_db() as db:
                    for rel_path in ready_deletes:
                        db.execute("DELETE FROM documents WHERE rel_path=?", (rel_path,))
                        print(f"[watcher] removed: {rel_path}")
            except Exception as e:
                print(f"[watcher] delete error: {e}")

        # Handle creates / modifies — re-sync the owning client folder
        for path in ready:
            try:
                p = Path(path)
                client_folder = p.parent
                while client_folder.parent != BASE_DIR and client_folder.parent != client_folder:
                    client_folder = client_folder.parent
                if client_folder.parent == BASE_DIR:
                    scanner_sync(client_folder)
                indexer_single(path)
            except Exception as e:
                print(f"[watcher] error processing {path}: {e}")


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
