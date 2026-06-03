"""New-client identification: creates _novo_ folders, runs AI name detection."""
import re, base64, shutil, threading, queue, time, unicodedata
from pathlib import Path
from typing import Optional
from database import get_db, setting
from config import BASE_DIR

_runs: dict[int, queue.Queue] = {}

VISION_MODEL  = "claude-haiku-4-5-20251001"
IMAGE_EXTS    = {".jpg", ".jpeg", ".png"}
DOC_EXTS      = {".pdf"} | IMAGE_EXTS

IDENTIFY_PROMPT = (
    "You are looking at documents from a Portuguese mortgage application dossier.\n"
    "Based on these documents, identify the full name(s) of the client(s).\n\n"
    "Reply with ONLY the name(s):\n"
    "- Single client: \"João Silva\"\n"
    "- Couple/joint:  \"João Silva e Maria Santos\"\n\n"
    "Use the name exactly as shown in official documents (ID card, passport, payslip).\n"
    "For couples use \" e \" between names.\n"
    "Reply with the name(s) only, nothing else."
)


# ── CRUD ────────────────────────────────────────────────────────────────────

def create_pending() -> str:
    """Create a _novo_TIMESTAMP folder on disk + DB record. Returns folder_name."""
    ts = int(time.time())
    folder_name = f"_novo_{ts}"
    folder_path = BASE_DIR / folder_name
    folder_path.mkdir(exist_ok=True)
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO pending_clients(folder_name, folder_path) VALUES(?,?)",
            (folder_name, str(folder_path))
        )
    return folder_name


def refresh_file_count(folder_name: str):
    """Count doc files in a _novo_ folder; mark ready when > 0."""
    folder_path = BASE_DIR / folder_name
    if not folder_path.exists():
        return
    count = sum(
        1 for f in folder_path.iterdir()
        if f.is_file() and f.suffix.lower() in DOC_EXTS
    )
    with get_db() as db:
        row = db.execute(
            "SELECT status FROM pending_clients WHERE folder_name=?", (folder_name,)
        ).fetchone()
        if not row:
            return
        new_status = row["status"]
        if count > 0 and row["status"] == "waiting":
            new_status = "ready"
        elif count == 0 and row["status"] == "ready":
            new_status = "waiting"
        db.execute(
            "UPDATE pending_clients SET file_count=?, status=?, updated_at=datetime('now') "
            "WHERE folder_name=?",
            (count, new_status, folder_name)
        )


def get_pending() -> list:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM pending_clients WHERE status != 'done' ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def cancel_pending(folder_name: str):
    folder_path = BASE_DIR / folder_name
    if folder_path.exists():
        shutil.rmtree(str(folder_path))
    with get_db() as db:
        db.execute("DELETE FROM pending_clients WHERE folder_name=?", (folder_name,))


# ── Conflict detection ───────────────────────────────────────────────────────

def _norm(s: str) -> str:
    nfd = unicodedata.normalize("NFD", s.lower())
    s2 = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return re.sub(r"[\s_\-]+", " ", s2).strip()


def _conflict_check(detected_name: str) -> Optional[str]:
    tokens = [t for t in _norm(detected_name).split() if len(t) >= 4]
    if not tokens:
        return None
    with get_db() as db:
        rows = db.execute(
            "SELECT folder_name FROM clients WHERE is_standby=0"
        ).fetchall()
    for row in rows:
        existing_norm = _norm(row["folder_name"])
        if any(token in existing_norm for token in tokens):
            return row["folder_name"]
    return None


# ── AI helpers ───────────────────────────────────────────────────────────────

def _render_first_page(path: Path) -> tuple[Optional[str], Optional[str]]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            import fitz
            doc = fitz.open(str(path))
            pix = doc[0].get_pixmap(dpi=120)
            b64 = base64.standard_b64encode(pix.tobytes("jpeg")).decode()
            doc.close()
            return b64, "image/jpeg"
        except Exception:
            return None, None
    elif ext in IMAGE_EXTS:
        try:
            with open(path, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode()
            return b64, ("image/png" if ext == ".png" else "image/jpeg")
        except Exception:
            return None, None
    return None, None


# ── Identification run ────────────────────────────────────────────────────────

def start_identification(folder_name: str) -> int:
    with get_db() as db:
        db.execute(
            "UPDATE pending_clients SET status='identifying', updated_at=datetime('now') "
            "WHERE folder_name=?", (folder_name,)
        )
        run_id = db.execute(
            "SELECT id FROM pending_clients WHERE folder_name=?", (folder_name,)
        ).fetchone()["id"]

    q: queue.Queue = queue.Queue()
    _runs[run_id] = q

    def _worker():
        try:
            _run_identification(folder_name, q)
        except Exception as e:
            q.put(f"ERROR: {e}")
            with get_db() as db:
                db.execute(
                    "UPDATE pending_clients SET status='error', updated_at=datetime('now') "
                    "WHERE folder_name=?", (folder_name,)
                )
        finally:
            q.put(None)

    threading.Thread(target=_worker, daemon=True).start()
    return run_id


def _run_identification(folder_name: str, q: queue.Queue):
    folder_path = BASE_DIR / folder_name
    q.put(f"Scanning {folder_name}...")

    api_key = setting("anthropic_api_key")
    if not api_key:
        q.put("ERROR: No API key — configure it in Settings.")
        with get_db() as db:
            db.execute(
                "UPDATE pending_clients SET status='error' WHERE folder_name=?", (folder_name,)
            )
        return

    try:
        import anthropic as _anthropic
        ai = _anthropic.Anthropic(api_key=api_key)
    except ImportError:
        q.put("ERROR: 'anthropic' package not installed.")
        return

    # Pick up to 5 files — prefer PDFs
    files = sorted(
        [f for f in folder_path.iterdir()
         if f.is_file() and f.suffix.lower() in DOC_EXTS],
        key=lambda f: (0 if f.suffix.lower() == ".pdf" else 1, f.name)
    )[:5]

    if not files:
        q.put("No documents found.")
        with get_db() as db:
            db.execute(
                "UPDATE pending_clients SET status='waiting' WHERE folder_name=?", (folder_name,)
            )
        return

    q.put(f"Found {len(files)} document(s). Rendering for AI...")

    content = []
    rendered = 0
    for f in files:
        b64, mt = _render_first_page(f)
        if b64:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mt, "data": b64}
            })
            rendered += 1

    if not rendered:
        q.put("Could not render any documents.")
        with get_db() as db:
            db.execute(
                "UPDATE pending_clients SET status='error' WHERE folder_name=?", (folder_name,)
            )
        return

    content.append({"type": "text", "text": IDENTIFY_PROMPT})

    q.put(f"Asking AI ({rendered} page(s) sent)...")
    resp = ai.messages.create(
        model=VISION_MODEL,
        max_tokens=60,
        messages=[{"role": "user", "content": content}],
    )
    detected_name = resp.content[0].text.strip().strip('"\'').strip()
    q.put(f"Detected name: {detected_name}")

    conflict = _conflict_check(detected_name)
    if conflict:
        q.put(f"[conflict] Similar folder exists: {conflict}")
        with get_db() as db:
            db.execute(
                "UPDATE pending_clients SET status='conflict', detected_name=?, "
                "conflict_with=?, updated_at=datetime('now') WHERE folder_name=?",
                (detected_name, conflict, folder_name)
            )
        return

    _apply_identification(folder_name, detected_name, q)


def _apply_identification(folder_name: str, confirmed_name: str, q: queue.Queue):
    old_path = BASE_DIR / folder_name
    new_path = BASE_DIR / confirmed_name

    q.put(f"Renaming folder → {confirmed_name}")
    try:
        old_path.rename(new_path)
    except Exception as e:
        q.put(f"ERROR renaming: {e}")
        with get_db() as db:
            db.execute(
                "UPDATE pending_clients SET status='error' WHERE folder_name=?", (folder_name,)
            )
        return

    with get_db() as db:
        db.execute(
            "UPDATE pending_clients SET status='done', detected_name=?, updated_at=datetime('now') "
            "WHERE folder_name=?",
            (confirmed_name, folder_name)
        )

    q.put("Syncing database...")
    try:
        from services.scanner import sync_all
        sync_all()
    except Exception as e:
        q.put(f"Sync warning: {e}")

    q.put(f"Starting organizer for {confirmed_name}...")
    try:
        from services.organizer_runner import start_run
        start_run(confirmed_name)
    except Exception as e:
        q.put(f"Organizer warning: {e}")

    q.put(f"[done] '{confirmed_name}' created — organizer running.")


def confirm_identification(folder_name: str, confirmed_name: str) -> int:
    """Force-apply a name (bypasses conflict warning)."""
    with get_db() as db:
        db.execute(
            "UPDATE pending_clients SET status='identifying', detected_name=?, "
            "conflict_with=NULL, updated_at=datetime('now') WHERE folder_name=?",
            (confirmed_name, folder_name)
        )
        run_id = db.execute(
            "SELECT id FROM pending_clients WHERE folder_name=?", (folder_name,)
        ).fetchone()["id"]

    q: queue.Queue = queue.Queue()
    _runs[run_id] = q

    def _worker():
        try:
            _apply_identification(folder_name, confirmed_name, q)
        except Exception as e:
            q.put(f"ERROR: {e}")
        finally:
            q.put(None)

    threading.Thread(target=_worker, daemon=True).start()
    return run_id


def stream_run(run_id: int):
    """SSE generator for identification progress."""
    q = _runs.get(run_id)
    if q is None:
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
            _runs.pop(run_id, None)
            break
        yield f"data: {line}\n\n"
        heartbeat = time.time()
