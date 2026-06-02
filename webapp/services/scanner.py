"""Syncs the filesystem client folders into the clients + documents tables."""
from pathlib import Path
from datetime import datetime
from config import BASE_DIR, SKIP_NAMES, STANDARD_FOLDERS, DOC_EXTS
from database import get_db

MIME = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
}

def sync_all():
    with get_db() as db:
        for item in sorted(BASE_DIR.iterdir(), key=lambda p: p.name.lower()):
            if not item.is_dir():
                continue
            if item.name.lower() in SKIP_NAMES:
                continue
            _upsert_client(db, item, standby=False)

        standby = BASE_DIR / "Standby"
        if standby.exists():
            for item in sorted(standby.iterdir(), key=lambda p: p.name.lower()):
                if item.is_dir():
                    _upsert_client(db, item, standby=True)

def _upsert_client(db, folder: Path, standby: bool):
    db.execute(
        """INSERT INTO clients(folder_name, folder_path, is_standby, last_scanned)
           VALUES(?,?,?,?)
           ON CONFLICT(folder_name) DO UPDATE SET
             folder_path=excluded.folder_path,
             is_standby=excluded.is_standby,
             last_scanned=excluded.last_scanned""",
        (folder.name, str(folder), int(standby), datetime.utcnow().isoformat())
    )
    client_id = db.execute(
        "SELECT id FROM clients WHERE folder_name=?", (folder.name,)
    ).fetchone()["id"]

    _sync_documents(db, folder, client_id)

def _sync_documents(db, client_folder: Path, client_id: int):
    for path in client_folder.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in DOC_EXTS:
            continue
        # Determine category and subclient from path depth
        category  = None
        subclient = None
        rel   = path.relative_to(client_folder)
        parts = rel.parts
        if len(parts) >= 2 and parts[0] in STANDARD_FOLDERS:
            category  = parts[0]          # root-level: Rendimentos/file.pdf
        elif len(parts) >= 3 and parts[1] in STANDARD_FOLDERS:
            subclient = parts[0]          # e.g. Ipshita, Ribal, fiador
            category  = parts[1]

        rel_path = str(path.relative_to(BASE_DIR))
        stat = path.stat()
        db.execute(
            """INSERT INTO documents(client_id,category,subclient,filename,rel_path,abs_path,
                                     file_size,file_mtime,mime_type)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(rel_path) DO UPDATE SET
                 category=excluded.category,
                 subclient=excluded.subclient,
                 file_size=excluded.file_size,
                 file_mtime=excluded.file_mtime""",
            (client_id, category, subclient, path.name, rel_path, str(path),
             stat.st_size, str(stat.st_mtime),
             MIME.get(path.suffix.lower(), "application/octet-stream"))
        )

def client_stats(db, client_id: int) -> dict:
    rows = db.execute(
        "SELECT category, COUNT(*) as cnt FROM documents WHERE client_id=? GROUP BY category",
        (client_id,)
    ).fetchall()
    stats = {r["category"]: r["cnt"] for r in rows}
    total = sum(stats.values())
    return {"by_category": stats, "total": total}

def unorganized_docs(db, client_id: int = None):
    if client_id:
        return db.execute(
            "SELECT d.*, c.folder_name FROM documents d JOIN clients c ON c.id=d.client_id "
            "WHERE d.category IS NULL AND d.client_id=?", (client_id,)
        ).fetchall()
    return db.execute(
        "SELECT d.*, c.folder_name FROM documents d JOIN clients c ON c.id=d.client_id "
        "WHERE d.category IS NULL ORDER BY c.folder_name"
    ).fetchall()
