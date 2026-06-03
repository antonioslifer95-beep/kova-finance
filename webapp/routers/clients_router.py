import shutil
from collections import defaultdict, OrderedDict
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from auth import current_user
from database import get_db
from services.scanner import client_stats
from services import identifier as _identifier
from config import TEMPLATE_DIR, STANDARD_FOLDERS, REQUIRED_CATEGORIES, REQUIRED_PER_PERSON, REQUIRED_SHARED

router = APIRouter()
tmpl   = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _subclient_names(db, client_id: int) -> list[str]:
    rows = db.execute(
        "SELECT DISTINCT subclient FROM documents WHERE client_id=? AND subclient IS NOT NULL",
        (client_id,)
    ).fetchall()
    return sorted(r["subclient"] for r in rows)


def _compute_missing(db, client_id: int, stats: dict) -> list[str]:
    """
    Return missing required categories.
    For clients with named sub-clients, check personal categories per person
    and shared categories (RGPD) at client level.
    """
    sc_names = _subclient_names(db, client_id)
    if not sc_names:
        present = {k for k, v in stats["by_category"].items() if v and k}
        return sorted(REQUIRED_CATEGORIES - present)

    missing = set()
    for sc in sc_names:
        sc_cats = {r["category"] for r in db.execute(
            "SELECT DISTINCT category FROM documents "
            "WHERE client_id=? AND subclient=? AND category IS NOT NULL",
            (client_id, sc)
        ).fetchall()}
        for req in REQUIRED_PER_PERSON:
            if req not in sc_cats:
                missing.add(f"{req} ({sc})")
    # Shared required: present anywhere in the client
    all_cats = {k for k, v in stats["by_category"].items() if v and k}
    for req in REQUIRED_SHARED:
        if req not in all_cats:
            missing.add(req)
    return sorted(missing)


@router.get("/clients/pending-partial", response_class=HTMLResponse)
def pending_partial(request: Request):
    current_user(request)
    pending = _identifier.get_pending()
    return tmpl.TemplateResponse("_pending_partial.html", {
        "request": request, "pending": pending,
    })


@router.post("/clients/new", response_class=HTMLResponse)
async def new_client(request: Request):
    current_user(request)
    folder_name = _identifier.create_pending()
    from config import BASE_DIR
    folder_path = str(BASE_DIR / folder_name)
    pending = _identifier.get_pending()
    return tmpl.TemplateResponse("_pending_partial.html", {
        "request": request, "pending": pending,
        "new_folder_path": folder_path,
    })


@router.post("/clients/identify", response_class=HTMLResponse)
async def identify_client(request: Request):
    current_user(request)
    form = await request.form()
    folder_name = form.get("folder_name")
    run_id = _identifier.start_identification(folder_name)
    return HTMLResponse(
        f'<div id="id-log-{run_id}" '
        f'class="font-mono text-xs text-green-400 bg-gray-900 rounded p-3 h-40 overflow-y-auto mt-2"'
        f' hx-ext="sse" sse-connect="/clients/identify/stream/{run_id}"'
        f' sse-swap="message" hx-swap="beforeend scroll:bottom"></div>'
    )


@router.get("/clients/identify/stream/{run_id}")
def identify_stream(request: Request, run_id: int):
    current_user(request)
    return StreamingResponse(
        _identifier.stream_run(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/clients/identify/confirm", response_class=HTMLResponse)
async def confirm_identification(request: Request):
    current_user(request)
    form = await request.form()
    folder_name    = form.get("folder_name")
    confirmed_name = form.get("confirmed_name", "").strip()
    if not confirmed_name:
        return HTMLResponse("<p class='text-red-400 text-xs'>Name cannot be empty.</p>")
    run_id = _identifier.confirm_identification(folder_name, confirmed_name)
    return HTMLResponse(
        f'<div id="id-log-{run_id}" '
        f'class="font-mono text-xs text-green-400 bg-gray-900 rounded p-3 h-40 overflow-y-auto mt-2"'
        f' hx-ext="sse" sse-connect="/clients/identify/stream/{run_id}"'
        f' sse-swap="message" hx-swap="beforeend scroll:bottom"></div>'
    )


@router.post("/clients/identify/cancel", response_class=HTMLResponse)
async def cancel_identification(request: Request):
    current_user(request)
    form = await request.form()
    folder_name = form.get("folder_name")
    _identifier.cancel_pending(folder_name)
    pending = _identifier.get_pending()
    return tmpl.TemplateResponse("_pending_partial.html", {
        "request": request, "pending": pending,
    })


@router.post("/clients/{client_id}/delete", response_class=HTMLResponse)
async def delete_client(request: Request, client_id: int):
    current_user(request)
    with get_db() as db:
        client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            return HTMLResponse("")
        folder_path = Path(client["folder_path"])
        # FTS table has no FK cascade — delete manually
        db.execute("DELETE FROM documents_fts WHERE client_id=?", (client_id,))
        # Deletes client + documents (ON DELETE CASCADE)
        db.execute("DELETE FROM clients WHERE id=?", (client_id,))
    if folder_path.exists():
        shutil.rmtree(str(folder_path))
    return HTMLResponse("")   # HTMX swaps the card element with nothing


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
            stats   = client_stats(db, c["id"])
            missing = _compute_missing(db, c["id"], stats)
            cards.append({
                "id": c["id"], "name": c["folder_name"],
                "total": stats["total"],
                "by_cat": stats["by_category"],
                "missing": missing,
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
            return RedirectResponse("/")

        docs = db.execute(
            "SELECT * FROM documents WHERE client_id=? ORDER BY subclient, category, filename",
            (client_id,)
        ).fetchall()

        # Build subclients: {sc_name -> {category -> [docs]}}
        # "" = shared / root-level docs
        raw: dict = {}
        for d in docs:
            sc  = d["subclient"] or ""
            cat = d["category"]  or "Sem categoria"
            raw.setdefault(sc, {}).setdefault(cat, []).append(dict(d))

        # Order: named sub-clients alphabetically first, "" (shared) last
        named = sorted(k for k in raw if k)
        order = named + ([""] if "" in raw else [])
        subclients = OrderedDict((k, raw[k]) for k in order)

        # Per-sub-client missing (named only)
        sc_missing: dict[str, list[str]] = {}
        for sc in named:
            present = {c for c in raw[sc] if c != "Sem categoria"}
            m = sorted(REQUIRED_PER_PERSON - present)
            if m:
                sc_missing[sc] = m

        stats   = client_stats(db, client_id)
        missing = _compute_missing(db, client_id, stats)

    return tmpl.TemplateResponse("client_detail.html", {
        "request": request, "user": user,
        "client":    dict(client),
        "subclients": subclients,
        "sc_missing": sc_missing,
        "has_subclients": bool(named),
        "stats":   stats,
        "missing": missing,
        "std_folders": STANDARD_FOLDERS,
    })
