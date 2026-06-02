import json
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from auth import current_user
from database import get_db, setting
from services.ai_service import answer_stream
from config import TEMPLATE_DIR

router = APIRouter()
tmpl   = Jinja2Templates(directory=str(TEMPLATE_DIR))

@router.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request, client_id: int = None):
    user = current_user(request)
    with get_db() as db:
        clients = db.execute("SELECT id, folder_name FROM clients WHERE is_standby=0 ORDER BY folder_name").fetchall()
        history = []
        if client_id:
            history = db.execute(
                "SELECT * FROM chat_messages WHERE user_id=? AND client_id=? ORDER BY created_at DESC LIMIT 20",
                (user["sub"], client_id)
            ).fetchall()
        else:
            history = db.execute(
                "SELECT * FROM chat_messages WHERE user_id=? AND client_id IS NULL ORDER BY created_at DESC LIMIT 20",
                (user["sub"],)
            ).fetchall()
    has_key = bool(setting("anthropic_api_key"))
    return tmpl.TemplateResponse("assistant.html", {
        "request": request, "user": user,
        "clients": [dict(c) for c in clients],
        "history": [dict(h) for h in reversed(list(history))],
        "sel_client": client_id,
        "has_key": has_key,
    })

@router.get("/ai/stream")
def ai_stream(request: Request, q: str, client_id: int = None):
    user = current_user(request)
    # Fetch recent history for context
    with get_db() as db:
        hist_rows = db.execute(
            "SELECT role, content FROM chat_messages WHERE user_id=? AND client_id IS ? "
            "ORDER BY created_at DESC LIMIT 6",
            (user["sub"], client_id)
        ).fetchall()
    history = [dict(r) for r in reversed(list(hist_rows))]

    full_response = []
    sources_json  = []

    def _gen():
        for chunk in answer_stream(q, client_id=client_id, history=history):
            if chunk.startswith("<!--SOURCES:") and chunk.endswith("-->"):
                try:
                    sources_json.append(chunk[12:-3])
                except Exception:
                    pass
                continue
            full_response.append(chunk)
            yield f"data: {json.dumps(chunk)}\n\n"

        # Persist messages
        answer_text = "".join(full_response)
        src = sources_json[0] if sources_json else None
        with get_db() as db:
            db.execute(
                "INSERT INTO chat_messages(user_id,client_id,role,content) VALUES(?,?,?,?)",
                (user["sub"], client_id, "user", q)
            )
            db.execute(
                "INSERT INTO chat_messages(user_id,client_id,role,content,sources) VALUES(?,?,?,?,?)",
                (user["sub"], client_id, "assistant", answer_text, src)
            )
        yield f"data: [DONE]\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
