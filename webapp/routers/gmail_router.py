from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from auth import require_admin
from database import set_setting

router = APIRouter()


@router.get("/settings/gmail/connect")
def gmail_connect(request: Request):
    require_admin(request)
    from services.gmail_service import build_auth_url
    return RedirectResponse(build_auth_url())


@router.get("/settings/gmail/callback")
def gmail_callback(request: Request, code: str = None, error: str = None):
    require_admin(request)
    if error:
        return RedirectResponse(f"/settings?msg=Gmail+connection+cancelled:+{error}", status_code=302)
    if not code:
        return RedirectResponse("/settings?msg=Gmail+connection+failed:+no+code+returned", status_code=302)
    from services.gmail_service import exchange_code_for_tokens
    try:
        exchange_code_for_tokens(code)
    except Exception as e:
        return RedirectResponse(f"/settings?msg=Gmail+connection+failed:+{e}", status_code=302)
    return RedirectResponse("/settings?msg=Gmail+connected+successfully", status_code=302)


@router.post("/settings/gmail/disconnect")
def gmail_disconnect(request: Request):
    require_admin(request)
    for key in ("gmail_refresh_token", "gmail_access_token", "gmail_token_expiry", "gmail_connected_email", "gmail_signature"):
        set_setting(key, "")
    return RedirectResponse("/settings?msg=Gmail+disconnected", status_code=302)


@router.post("/settings/gmail/refresh-signature")
def gmail_refresh_signature(request: Request):
    require_admin(request)
    from services.gmail_service import get_valid_access_token, fetch_and_store_signature
    try:
        token = get_valid_access_token()
        err = fetch_and_store_signature(token)
        msg = f"Error:+{err}" if err else "Signature+updated"
    except Exception as e:
        msg = f"Error:+{e}"
    return RedirectResponse(f"/settings?msg={msg}", status_code=302)
