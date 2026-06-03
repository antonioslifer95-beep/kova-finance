"""New-client identification: creates _novo_ folders, runs AI name detection."""
import re, base64, shutil, threading, queue, time, unicodedata
from pathlib import Path
from typing import Optional
from database import get_db, setting
from config import BASE_DIR

_runs: dict[int, queue.Queue] = {}

VISION_MODEL  = "claude-sonnet-4-6"   # Sonnet for identification — one-time per client, needs accuracy
IMAGE_EXTS    = {".jpg", ".jpeg", ".png"}
DOC_EXTS      = {".pdf"} | IMAGE_EXTS

# Personal categories where files get a person suffix — shared ones (Imóvel, Proposta) don't
_PERSONAL_CATS = {"Documentos Pessoais", "Rendimentos", "Extratos Bancários", "IRS", "Mapa CRC", "RGPD"}

# Known bank/institution tokens that appear in filenames but are NOT person names
_KNOWN_NON_PERSON = {
    "BCP", "BPI", "CGD", "Santander", "NovoBanco", "Revolut", "Wise",
    "ActivoBank", "Montepio", "Itau", "Millennium", "Bankinter",
    "CA", "CRC", "BP", "AL", "SS",
}

CATEGORIZE_AND_NAME_PROMPT = """\
This is a document from a Portuguese mortgage application dossier.
Provide exactly two lines:
Category: <one of: Documentos Pessoais, Rendimentos, Extratos Bancários, IRS, Imóvel, Mapa CRC, RGPD, Proposta Crédito>
Filename: <standardized stem — use these conventions>

Rendimentos:    payslip → RecVenc_YYYY-MM_FirstName  |  employer declaration → DeclPatronal_FirstName
Extratos:       bank statement → Extrato_YYYY-MM_BankName_FirstName  (omit FirstName if joint/unclear)
IRS:            tax return → IRS_YYYY_FirstName  |  liquidation note → NotaLiq_IRS_YYYY_FirstName
Documentos:     CC/BI → CC_FirstName  |  passport → Passaporte_FirstName  |  residence permit → TituloResidencia_FirstName
                address proof → CompMorada_FirstName  |  IBAN proof → CompIBAN_BankName_FirstName
Mapa CRC:       → MapaCRC_YYYY-MM_FirstName
RGPD:           → RGPD_FirstName  (or just RGPD if joint)
Imóvel:         property cert → CertidaoPredial  |  energy cert → CertificadoEnergetico  |  deed → Escritura
Proposta:       proposal → Proposta_BankName_YYYY-MM

Rules:
- FirstName = the person's first name ONLY as it appears in the document (e.g. Tiago, Dalila, Vera)
- Use _ between parts, no spaces, YYYY-MM for dates
- If the document clearly belongs to a specific person, include their FirstName as the LAST part
- If shared/joint or person unclear, omit FirstName
- Reply with ONLY the two lines above, nothing else
"""


def _person_from_stem(stem: str) -> Optional[str]:
    """Extract person first name from a standardized filename stem.
    Person name is always the last _ token, alphabetic only."""
    parts = stem.split("_")
    for part in reversed(parts):
        if not part:
            continue
        if re.match(r'^\d{4}(-\d{2})?$', part):   # date
            continue
        if re.match(r'^v\d+$', part, re.IGNORECASE):  # version
            continue
        if part.upper() in {n.upper() for n in _KNOWN_NON_PERSON}:
            continue
        if len(part) < 3:
            continue
        if re.match(r'^[A-Za-zÀ-ÿ]+$', part):     # pure letters = name
            return part.title()
    return None


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
        matches = sum(1 for t in tokens if t in existing_norm)
        # Require 2+ matching tokens to avoid false positives on common first names
        if matches >= 2:
            return row["folder_name"]
    return None


# ── AI helpers ───────────────────────────────────────────────────────────────

def _render_first_page(path: Path) -> tuple[Optional[str], Optional[str]]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            import fitz
            doc = fitz.open(str(path))
            page = doc[0]
            rect = page.rect
            # Scale so the longest dimension is at most 1500 px — handles both
            # normal PDFs and high-res raster scans stored with huge page rects
            max_px = 1500
            scale = max_px / max(rect.width, rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
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


def _parse_name_response(raw: str) -> Optional[str]:
    """
    Extract a clean name from the AI response, stripping common preambles,
    markdown bold markers, and verbose explanations.
    """
    import re as _re
    text = raw.strip()
    # Strip markdown bold/italic
    text = _re.sub(r'\*+', '', text)
    # If model wrote "YES\n..." or "Yes, ...\n..." take everything after the first newline
    if '\n' in text:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        # Skip lines that are clearly preamble or junk
        for line in lines:
            low = line.lower()
            if low in ('yes', 'no', 'skip'):
                continue
            if low.startswith('yes,') or low.startswith('yes:'):
                continue
            if low.startswith('the complete') or low.startswith('the full'):
                continue
            # Reject MRZ lines (contain < separator), explanatory labels (contain :),
            # or lines starting with common English explanation words
            if '<' in line or ':' in line:
                continue
            if _re.match(r'^(looking|from|based|the |this |i |note|however|unfortunately)', low):
                continue
            # Must look like a name: only letters, spaces, hyphens — no digits or special chars
            if not _re.match(r"^[A-Za-zÀ-ÿ\s\-']+$", line):
                continue
            if len(line) < 3 or len(line) > 80:
                continue
            text = line
            break
        else:
            return None
    # Now clean the single line
    text = text.strip().strip('"\'').strip()
    # Remove trailing punctuation
    text = text.rstrip('.')
    # Skip if it's still a preamble word
    if text.lower() in ('yes', 'no', 'skip', 'not_an_id', ''):
        return None
    # Skip if too long (likely a sentence, not a name)
    if len(text) > 80:
        return None
    # Title-case if all-caps
    if text == text.upper():
        text = text.title()
    return text or None


def _deduplicate_names(names: list) -> list:
    """
    Merge entries that refer to the same person (same first name, slightly different
    representation). Two names are the same person iff their first tokens match —
    do NOT merge on shared surnames alone (e.g. Pedro Pereira ≠ Vera Pereira).
    """
    unique = []
    for name in names:
        first = _norm(name).split()[0] if name.strip() else ""
        is_dup = False
        for i, existing in enumerate(unique):
            existing_first = _norm(existing).split()[0] if existing.strip() else ""
            if first and first == existing_first:
                is_dup = True
                if len(name) > len(existing):  # keep the fuller version
                    unique[i] = name
                break
        if not is_dup:
            unique.append(name)
    return unique


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

    # Pick up to 6 files — prefer PDFs, then images
    files = sorted(
        [f for f in folder_path.iterdir()
         if f.is_file() and f.suffix.lower() in DOC_EXTS],
        key=lambda f: (0 if f.suffix.lower() == ".pdf" else 1, f.name)
    )[:6]

    if not files:
        q.put("No documents found.")
        with get_db() as db:
            db.execute(
                "UPDATE pending_clients SET status='waiting' WHERE folder_name=?", (folder_name,)
            )
        return

    q.put(f"Found {len(files)} document(s). Categorising and renaming each...")

    # Rename-first approach: get a standardised filename with the person's first name
    # embedded, then extract names from the filenames — much more reliable than
    # trying to parse CC fields directly.
    names_found = []
    for f in files:
        b64, mt = _render_first_page(f)
        if not b64:
            continue
        q.put(f"  {f.name}")
        try:
            resp = ai.messages.create(
                model=VISION_MODEL,
                max_tokens=60,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}},
                    {"type": "text", "text": CATEGORIZE_AND_NAME_PROMPT},
                ]}],
            )
            raw = resp.content[0].text.strip()
            # Parse "Category: X\nFilename: Y"
            category = stem = None
            for line in raw.splitlines():
                line = line.strip()
                if line.lower().startswith("category:"):
                    category = line.split(":", 1)[1].strip()
                elif line.lower().startswith("filename:"):
                    stem = line.split(":", 1)[1].strip().strip('"\'')
            if not category or not stem:
                q.put(f"  [skip] no structured response")
                continue
            # Only personal categories carry a person name
            if category not in _PERSONAL_CATS:
                q.put(f"  → {stem} ({category}, shared)")
                continue
            person = _person_from_stem(stem)
            if person:
                q.put(f"  → {stem}  →  {person}")
                names_found.append(person)
            else:
                q.put(f"  → {stem} ({category}, no name)")
        except Exception as e:
            q.put(f"  [error] {e}")

    unique_names = _deduplicate_names(names_found)

    if not unique_names:
        q.put("Could not determine names — please type the client name manually.")
        with get_db() as db:
            db.execute(
                "UPDATE pending_clients SET status='conflict', detected_name='', "
                "conflict_with='', updated_at=datetime('now') WHERE folder_name=?",
                (folder_name,)
            )
        return

    detected_name = " e ".join(unique_names[:2])
    q.put(f"Detected: {detected_name}")

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
