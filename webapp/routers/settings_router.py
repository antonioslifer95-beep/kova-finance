from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from auth import require_admin, hash_password
from database import get_db, setting, set_setting
from config import TEMPLATE_DIR

router = APIRouter()
tmpl   = Jinja2Templates(directory=str(TEMPLATE_DIR))

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    user = require_admin(request)
    with get_db() as db:
        users   = db.execute("SELECT id,username,email,role,is_active,created_at FROM users ORDER BY created_at").fetchall()
        api_key = setting("anthropic_api_key")
        watcher = setting("watcher_enabled")
        bank_contacts = db.execute("SELECT * FROM bank_contacts ORDER BY bank_name, contact_name").fetchall()
    return tmpl.TemplateResponse("settings.html", {
        "request": request, "user": user,
        "users":   [dict(u) for u in users],
        "api_key": api_key,
        "watcher_enabled": watcher == "1",
        "bank_contacts": [dict(c) for c in bank_contacts],
        "gmail_connected": bool(setting("gmail_refresh_token")),
        "gmail_connected_email": setting("gmail_connected_email"),
        "gmail_client_id": setting("gmail_client_id"),
        "gmail_client_secret": setting("gmail_client_secret"),
        "gmail_signature": setting("gmail_signature"),
        "msg": msg,
    })

@router.post("/settings/api-key")
async def save_api_key(request: Request, api_key: str = Form(...)):
    require_admin(request)
    set_setting("anthropic_api_key", api_key.strip())
    return RedirectResponse("/settings?msg=API+key+saved", status_code=302)

@router.post("/settings/watcher")
async def toggle_watcher(request: Request, enabled: str = Form(default="0")):
    require_admin(request)
    set_setting("watcher_enabled", "1" if enabled == "1" else "0")
    return RedirectResponse("/settings?msg=Watcher+updated", status_code=302)

@router.post("/settings/users/create")
async def create_user(request: Request,
                      username: str = Form(...), email: str = Form(default=""),
                      password: str = Form(...), role: str = Form(default="user")):
    require_admin(request)
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO users(username,email,hashed_pw,role) VALUES(?,?,?,?)",
                (username, email, hash_password(password), role)
            )
    except Exception as e:
        return RedirectResponse(f"/settings?msg=Error:+{e}", status_code=302)
    return RedirectResponse("/settings?msg=User+created", status_code=302)

@router.post("/settings/users/{uid}/toggle")
def toggle_user(request: Request, uid: int):
    require_admin(request)
    with get_db() as db:
        db.execute("UPDATE users SET is_active = 1 - is_active WHERE id=?", (uid,))
    return RedirectResponse("/settings?msg=User+updated", status_code=302)

@router.post("/settings/users/{uid}/delete")
def delete_user(request: Request, uid: int):
    require_admin(request)
    with get_db() as db:
        db.execute("DELETE FROM users WHERE id=?", (uid,))
    return RedirectResponse("/settings?msg=User+deleted", status_code=302)

@router.post("/indexer/reindex")
def reindex(request: Request):
    require_admin(request)
    from services.indexer import reindex_all
    n = reindex_all()
    return RedirectResponse(f"/settings?msg=Re-indexing+{n}+documents", status_code=302)

@router.post("/indexer/reindex-images")
def reindex_images(request: Request):
    require_admin(request)
    from services.indexer import reindex_images as _reindex_images
    n = _reindex_images()
    return RedirectResponse(f"/settings?msg=OCR+started+for+{n}+image+files", status_code=302)

@router.post("/settings/gmail/credentials")
async def save_gmail_credentials(request: Request,
                                  client_id: str = Form(...), client_secret: str = Form(...)):
    require_admin(request)
    set_setting("gmail_client_id", client_id.strip())
    set_setting("gmail_client_secret", client_secret.strip())
    return RedirectResponse("/settings?msg=Gmail+credentials+saved", status_code=302)

@router.post("/settings/bank-contacts/create")
async def create_bank_contact(request: Request,
                               bank_name: str = Form(...), contact_name: str = Form(default=""),
                               email: str = Form(...), notes: str = Form(default="")):
    require_admin(request)
    with get_db() as db:
        db.execute(
            "INSERT INTO bank_contacts(bank_name,contact_name,email,notes) VALUES(?,?,?,?)",
            (bank_name.strip(), contact_name.strip(), email.strip(), notes.strip())
        )
    return RedirectResponse("/settings?msg=Bank+contact+added", status_code=302)

@router.post("/settings/bank-contacts/{cid}/toggle")
def toggle_bank_contact(request: Request, cid: int):
    require_admin(request)
    with get_db() as db:
        db.execute("UPDATE bank_contacts SET is_active = 1 - is_active WHERE id=?", (cid,))
    return RedirectResponse("/settings?msg=Bank+contact+updated", status_code=302)

@router.post("/settings/bank-contacts/{cid}/delete")
def delete_bank_contact(request: Request, cid: int):
    require_admin(request)
    with get_db() as db:
        db.execute("DELETE FROM bank_contacts WHERE id=?", (cid,))
    return RedirectResponse("/settings?msg=Bank+contact+deleted", status_code=302)
