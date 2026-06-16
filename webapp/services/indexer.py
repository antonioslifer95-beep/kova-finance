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


def _ocr_pdf(abs_path: str, page_count: int, api_key: str) -> str:
    """
    OCR an image-based PDF by rendering each page as JPEG and sending to Claude Vision.
    Caps at 4 pages to control cost. Returns concatenated text.
    """
    import fitz, base64, anthropic
    try:
        doc = fitz.open(abs_path)
        content = []
        for i, page in enumerate(doc):
            if i >= 4:
                break
            pix      = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            img_bytes = pix.tobytes("jpeg")
            if len(img_bytes) > 5 * 1024 * 1024:
                continue
            b64 = base64.standard_b64encode(img_bytes).decode()
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
        doc.close()
        if not content:
            return ""
        content.append({"type": "text", "text": (
            "Extract all text from these document page image(s). "
            "Return only the raw text, preserving layout. "
            "Include all numbers, dates, names, and labels."
        )})
        ai  = anthropic.Anthropic(api_key=api_key)
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": content}]
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
            if text.strip():
                return text.strip(), pages
            # Image-based PDF — try Vision OCR
            from database import setting
            api_key = setting("anthropic_api_key")
            if api_key:
                return _ocr_pdf(abs_path, pages, api_key), pages
            return "", pages
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
    """Re-index images and image-based PDFs (scanned docs with no extractable text)."""
    with get_db() as db:
        base = """
            SELECT d.id, d.abs_path, d.client_id, d.category, d.filename
            FROM documents d
            LEFT JOIN documents_fts fts ON fts.doc_id = d.id
            WHERE (
                d.mime_type IN ('image/jpeg', 'image/png')
                OR (d.mime_type = 'application/pdf' AND (fts.body IS NULL OR fts.body = ''))
            )
        """
        if client_id:
            rows = db.execute(base + " AND d.client_id=?", (client_id,)).fetchall()
        else:
            rows = db.execute(base).fetchall()
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
