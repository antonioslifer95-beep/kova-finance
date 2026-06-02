from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from auth import current_user
from database import get_db
from services.scanner import client_stats
from config import TEMPLATE_DIR, STANDARD_FOLDERS, REQUIRED_CATEGORIES

router = APIRouter()
tmpl   = Jinja2Templates(directory=str(TEMPLATE_DIR))

@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = current_user(request)
    with get_db() as db:
        clients = db.execute(
            "SELECT * FROM clients WHERE is_standby=0 ORDER BY folder_name"
        ).fetchall()
        cards = []
        for c in clients:
            stats = client_stats(db, c["id"])
            missing = REQUIRED_CATEGORIES - set(
                k for k, v in stats["by_category"].items() if v > 0
            )
            cards.append({
                "id": c["id"], "name": c["folder_name"],
                "total": stats["total"],
                "by_cat": stats["by_category"],
                "missing": sorted(missing),
                "unorganized": stats["by_category"].get(None, 0),
            })
    return tmpl.TemplateResponse("dashboard.html", {
        "request": request, "user": user,
        "cards": cards, "std_folders": STANDARD_FOLDERS,
    })

@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail(request: Request, client_id: int):
    user = current_user(request)
    with get_db() as db:
        client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/")
        docs = db.execute(
            "SELECT * FROM documents WHERE client_id=? ORDER BY category, filename",
            (client_id,)
        ).fetchall()
        # Group by category
        from collections import defaultdict
        groups: dict = defaultdict(list)
        for d in docs:
            groups[d["category"] or "Sem categoria"].append(dict(d))
        stats = client_stats(db, client_id)
        missing = REQUIRED_CATEGORIES - set(k for k, v in stats["by_category"].items() if v > 0)
    return tmpl.TemplateResponse("client_detail.html", {
        "request": request, "user": user,
        "client": dict(client), "groups": dict(groups),
        "stats": stats, "missing": sorted(missing),
        "std_folders": STANDARD_FOLDERS,
    })
