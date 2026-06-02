from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from auth import current_user
from database import get_db
from services import organizer_runner
from services.scanner import unorganized_docs
from config import TEMPLATE_DIR

router = APIRouter()
tmpl   = Jinja2Templates(directory=str(TEMPLATE_DIR))

@router.get("/organizer", response_class=HTMLResponse)
def organizer_page(request: Request):
    user = current_user(request)
    with get_db() as db:
        undone = unorganized_docs(db)
        runs   = organizer_runner.recent_runs(db)
        clients = db.execute("SELECT id, folder_name FROM clients WHERE is_standby=0 ORDER BY folder_name").fetchall()
    return tmpl.TemplateResponse("organizer.html", {
        "request": request, "user": user,
        "undone": [dict(u) for u in undone],
        "runs":   [dict(r) for r in runs],
        "clients": [dict(c) for c in clients],
    })

@router.post("/organizer/run")
async def run_organizer(request: Request):
    current_user(request)
    form = await request.form()
    client_name = form.get("client") or None
    run_id = organizer_runner.start_run(client_name)
    # Return HTMX partial that opens the SSE stream
    return HTMLResponse(
        f'<div id="log-box" class="font-mono text-xs text-green-400 bg-gray-900 rounded p-3 h-64 overflow-y-auto"'
        f' hx-ext="sse" sse-connect="/organizer/stream/{run_id}"'
        f' sse-swap="message" hx-swap="beforeend"></div>'
    )

@router.get("/organizer/stream/{run_id}")
def stream(request: Request, run_id: int):
    current_user(request)
    return StreamingResponse(
        organizer_runner.stream_run(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
