"""Runs organize_kova.py as a subprocess and streams output via a queue."""
import sys, subprocess, threading, queue, time
from pathlib import Path
from datetime import datetime
from database import get_db, setting
from config import BASE_DIR

_runs: dict[int, queue.Queue] = {}

def start_run(client_name: str = None) -> int:
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO organizer_runs(client_scope) VALUES(?)", (client_name,)
        )
        run_id = cur.lastrowid

    q: queue.Queue = queue.Queue()
    _runs[run_id] = q

    def _worker():
        script = BASE_DIR / "organize_kova.py"
        # The organizer reads the API key from the DB itself — no need to pass it here.
        # Only pass --client if scoped; always apply.
        cmd = [sys.executable, str(script), "--apply"]
        if client_name:
            cmd += ["--client", client_name]
        log_lines = []
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=str(BASE_DIR)
            )
            for line in proc.stdout:
                line = line.rstrip()
                q.put(line)
                log_lines.append(line)
            proc.wait()
            status = "done" if proc.returncode == 0 else "error"
        except Exception as e:
            status = "error"
            log_lines.append(f"ERROR: {e}")
            q.put(f"ERROR: {e}")

        # Re-sync the DB so the webapp reflects any moved/created files immediately
        try:
            from services.scanner import sync_all
            q.put("[scanner] Updating document database...")
            sync_all()
            q.put("[scanner] Done.")
        except Exception as e:
            q.put(f"[scanner] Error: {e}")

        q.put(None)  # sentinel
        with get_db() as db:
            db.execute(
                "UPDATE organizer_runs SET finished_at=?, status=?, log_text=? WHERE id=?",
                (datetime.utcnow().isoformat(), status, "\n".join(log_lines), run_id)
            )

    threading.Thread(target=_worker, daemon=True).start()
    return run_id

def stream_run(run_id: int):
    """Generator yielding SSE-formatted lines."""
    q = _runs.get(run_id)
    if q is None:
        # Return last log from DB
        with get_db() as db:
            row = db.execute("SELECT log_text FROM organizer_runs WHERE id=?", (run_id,)).fetchone()
        if row and row["log_text"]:
            for line in row["log_text"].splitlines():
                yield f"data: {line}\n\n"
        yield "data: [done]\n\n"
        return

    heartbeat = time.time()
    while True:
        try:
            line = q.get(timeout=15)
        except queue.Empty:
            if time.time() - heartbeat > 14:
                yield ": keep-alive\n\n"
                heartbeat = time.time()
            continue
        if line is None:
            yield "data: [done]\n\n"
            del _runs[run_id]
            break
        yield f"data: {line}\n\n"
        heartbeat = time.time()

def recent_runs(db, limit: int = 10):
    return db.execute(
        "SELECT * FROM organizer_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
