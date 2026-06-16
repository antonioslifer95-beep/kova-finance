"""Extracts text from PDFs and images, writes to documents_fts."""
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from database import get_db

try:
    import fitz
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="indexer")

def _ocr_image(abs_path: str, api_key: str) -> str:
    """Extract text from an image via Claude Vision. Returns empty string on any failure."""
    path = Path(abs_path)
    try:
        if path.stat().st_size > 5 * 1024 * 1024:
            return ""  # skip files over 5 MB to stay within vision limits
        import base64, anthropic
        ext  = path.suffix.lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        b64  = base64.standard_b64encode(path.read_bytes()).decode()
        ai   = anthropic.Anthropic(api_key=api_key)
        msg  = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text", "text": (
                        "Extract all text from this document image. "
                        "Return only the raw text, preserving layout as much as possible. "
                        "Include all numbers, dates, names, and labels. "
                        "If this is not a document, return an empty string."
                    )},
                ]
            }]
        )
        return msg.content[0].text.strip()
    except Exception:
        return ""


def extract_text(abs_path: str) -> tuple[str, int]:
    """Returns (text, page_count). Never raises."""
    path = Path(abs_path)
    ext  = path.suffix.lower()
    try:
        if ext == ".pdf" and HAS_FITZ:
            doc   = fitz.open(abs_path)
            pages = len(doc)
            text  = "\n".join(page.get_text() for page in doc)
            doc.close()
            return text.strip(), pages
        elif ext in {".jpg", ".jpeg", ".png"}:
            from database import setting
            api_key = setting("anthropic_api_key")
            if api_key:
                return _ocr_image(abs_path, api_key), 1
            return "", 1
    except Exception:
        pass
    return "", 0

def index_document(doc_id: int, abs_path: str, client_id: int, category: str, filename: str):
    text, pages = extract_text(abs_path)
    with get_db() as db:
        # Remove old FTS entry if exists
        db.execute("DELETE FROM documents_fts WHERE doc_id=?", (doc_id,))
        db.execute(
            "INSERT INTO documents_fts(doc_id,client_id,category,filename,body) VALUES(?,?,?,?,?)",
            (doc_id, client_id, category or "", filename, text)
        )
        db.execute(
            "UPDATE documents SET indexed_at=?, page_count=? WHERE id=?",
            (datetime.utcnow().isoformat(), pages, doc_id)
        )

def index_all_pending():
    """Index documents that haven't been indexed yet."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, abs_path, client_id, category, filename FROM documents "
            "WHERE indexed_at IS NULL"
        ).fetchall()
    futures = [
        _EXECUTOR.submit(index_document, r["id"], r["abs_path"], r["client_id"], r["category"], r["filename"])
        for r in rows
    ]
    return len(futures)

def reindex_all():
    with get_db() as db:
        rows = db.execute(
            "SELECT id, abs_path, client_id, category, filename FROM documents"
        ).fetchall()
    futures = [
        _EXECUTOR.submit(index_document, r["id"], r["abs_path"], r["client_id"], r["category"], r["filename"])
        for r in rows
    ]
    return len(futures)

def reindex_images(client_id: int = None):
    """Re-index image documents. Pass client_id to limit to one client."""
    with get_db() as db:
        if client_id:
            rows = db.execute(
                "SELECT id, abs_path, client_id, category, filename FROM documents "
                "WHERE mime_type IN ('image/jpeg', 'image/png') AND client_id=?",
                (client_id,)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, abs_path, client_id, category, filename FROM documents "
                "WHERE mime_type IN ('image/jpeg', 'image/png')"
            ).fetchall()
    futures = [
        _EXECUTOR.submit(index_document, r["id"], r["abs_path"], r["client_id"], r["category"], r["filename"])
        for r in rows
    ]
    return len(futures)


def index_single_file(abs_path: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM documents WHERE abs_path=?", (abs_path,)).fetchone()
    if row:
        _EXECUTOR.submit(index_document, row["id"], row["abs_path"], row["client_id"], row["category"], row["filename"])

def search(query: str, client_id: int = None, limit: int = 10) -> list:
    with get_db() as db:
        if client_id:
            rows = db.execute(
                """SELECT d.id, d.filename, d.abs_path, d.category, c.folder_name,
                          snippet(documents_fts, 4, '<mark>', '</mark>', '…', 24) AS snippet
                   FROM documents_fts
                   JOIN documents d ON d.id = documents_fts.doc_id
                   JOIN clients c ON c.id = d.client_id
                   WHERE documents_fts MATCH ? AND documents_fts.client_id=?
                   ORDER BY rank LIMIT ?""",
                (query, client_id, limit)
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT d.id, d.filename, d.abs_path, d.category, c.folder_name,
                          snippet(documents_fts, 4, '<mark>', '</mark>', '…', 24) AS snippet
                   FROM documents_fts
                   JOIN documents d ON d.id = documents_fts.doc_id
                   JOIN clients c ON c.id = d.client_id
                   WHERE documents_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (query, limit)
            ).fetchall()
        return [dict(r) for r in rows]
