import json
from collections import OrderedDict
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from auth import current_user
from database import get_db
from config import TEMPLATE_DIR

router = APIRouter()
tmpl   = Jinja2Templates(directory=str(TEMPLATE_DIR))


@router.get("/clients/{client_id}/email", response_class=HTMLResponse)
def email_compose(request: Request, client_id: int, msg: str = ""):
    user = current_user(request)
    with get_db() as db:
        client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            return RedirectResponse("/")

        docs = db.execute(
            "SELECT * FROM documents WHERE client_id=? ORDER BY subclient, category, filename",
            (client_id,)
        ).fetchall()

        raw: dict = {}
        for d in docs:
            sc  = d["subclient"] or ""
            cat = d["category"]  or "Sem categoria"
            raw.setdefault(sc, {}).setdefault(cat, []).append(dict(d))
        named = sorted(k for k in raw if k)
        order = named + ([""] if "" in raw else [])
        subclients = OrderedDict((k, raw[k]) for k in order)

        contacts = db.execute(
            "SELECT * FROM bank_contacts WHERE is_active=1 ORDER BY bank_name, contact_name"
        ).fetchall()

    return tmpl.TemplateResponse("email_compose.html", {
        "request": request, "user": user,
        "client": dict(client),
        "subclients": subclients,
        "has_subclients": bool(named),
        "contacts": [dict(c) for c in contacts],
        "msg": msg,
    })


@router.post("/clients/{client_id}/email/send")
async def email_send(request: Request, client_id: int):
    user = current_user(request)
    form = await request.form()
    doc_ids = [int(x) for x in form.getlist("doc_ids")]
    recipient_email = (form.get("recipient_email") or "").strip()
    bank_contact_id = form.get("bank_contact_id") or None
    subject = (form.get("subject") or "").strip()
    body = form.get("body") or ""

    if not doc_ids or not recipient_email or not subject:
        return RedirectResponse(
            f"/clients/{client_id}/email?msg=Select+at+least+one+file,+a+recipient+and+a+subject",
            status_code=302,
        )

    with get_db() as db:
        placeholders = ",".join("?" * len(doc_ids))
        doc_rows = db.execute(
            f"SELECT * FROM documents WHERE id IN ({placeholders}) AND client_id=?",
            (*doc_ids, client_id)
        ).fetchall()
    doc_rows = [dict(d) for d in doc_rows]

    from services.gmail_service import check_size_guard, send_email
    ok, total_bytes = check_size_guard(doc_rows)
    if not ok:
        mb = total_bytes / (1024 * 1024)
        return RedirectResponse(
            f"/clients/{client_id}/email?msg=Files+total+{mb:.1f}MB,+exceeds+18MB+limit.+"
            f"Select+fewer+files+or+send+in+multiple+emails.",
            status_code=302,
        )

    filenames = [d["filename"] for d in doc_rows]
    try:
        gmail_id = send_email(recipient_email, subject, body, doc_rows)
        status_val, error_msg = "sent", None
    except Exception as e:
        status_val, error_msg, gmail_id = "failed", str(e), None

    with get_db() as db:
        db.execute(
            """INSERT INTO email_log
               (client_id, bank_contact_id, recipient_email, subject, body,
                doc_ids, filenames, total_bytes, status, error_message,
                gmail_message_id, sent_by_user_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (client_id, bank_contact_id, recipient_email, subject, body,
             json.dumps(doc_ids), json.dumps(filenames), total_bytes, status_val, error_msg,
             gmail_id, int(user["sub"]))
        )

    if status_val == "failed":
        return RedirectResponse(
            f"/clients/{client_id}/email?msg=Send+failed:+{error_msg}", status_code=302
        )
    return RedirectResponse(
        f"/clients/{client_id}?msg=email_sent&n={len(doc_ids)}", status_code=302
    )
