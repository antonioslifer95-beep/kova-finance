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

@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", client_id: int = None):
    from fastapi.templating import Jinja2Templates
    from config import TEMPLATE_DIR
    user = current_user(request)
    results = []
    if q:
        from services.indexer import search
        results = search(q, client_id=client_id, limit=20)
    from fastapi.templating import Jinja2Templates
    tmpl = Jinja2Templates(directory=str(TEMPLATE_DIR))
    with get_db() as db:
        clients = db.execute("SELECT id, folder_name FROM clients ORDER BY folder_name").fetchall()
    return tmpl.TemplateResponse("search.html", {
        "request": request, "user": user,
        "query": q, "results": results,
        "clients": [dict(c) for c in clients],
        "sel_client": client_id,
    })
