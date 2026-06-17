from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from auth import current_user
from database import get_db

router = APIRouter()

MIME_INLINE = {"application/pdf", "image/jpeg", "image/png"}

@router.get("/documents/{doc_id}/preview")
def preview(request: Request, doc_id: int):
    current_user(request)
    with get_db() as db:
        doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        raise HTTPException(404)
    path = Path(doc["abs_path"])
    if not path.exists():
        raise HTTPException(404, "File not found on disk")
    mime = doc["mime_type"] or "application/octet-stream"
    disposition = "inline" if mime in MIME_INLINE else "attachment"
    return FileResponse(str(path), media_type=mime,
                        headers={"Content-Disposition": f'{disposition}; filename="{path.name}"'})

@router.get("/documents/{doc_id}/download")
def download(request: Request, doc_id: int, token: str = None):
    current_user(request)
    with get_db() as db:
        doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        raise HTTPException(404)
    path = Path(doc["abs_path"])
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path),
                        headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

@router.get("/search")
def search_page(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/simulation", status_code=301)
