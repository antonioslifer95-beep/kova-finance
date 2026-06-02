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
