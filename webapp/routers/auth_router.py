from fastapi import APIRouter, Request, Response, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from auth import authenticate, create_token, hash_password, current_user
from database import get_db
from config import TEMPLATE_DIR

router = APIRouter()
tmpl   = Jinja2Templates(directory=str(TEMPLATE_DIR))

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    token = request.cookies.get("kova_token")
    if token:
        from auth import decode_token
        if decode_token(token):
            return RedirectResponse("/", status_code=302)
    return tmpl.TemplateResponse("login.html", {"request": request, "error": None})

@router.post("/login")
async def do_login(request: Request, response: Response,
                   username: str = Form(...), password: str = Form(...)):
    user = authenticate(username, password)
    if not user:
        return tmpl.TemplateResponse("login.html",
            {"request": request, "error": "Invalid username or password"}, status_code=401)
    token = create_token(user["id"], user["username"], user["role"])
    resp  = RedirectResponse("/", status_code=302)
    resp.set_cookie("kova_token", token, httponly=True, samesite="lax", max_age=3600*8)
    return resp

@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("kova_token")
    return resp

@router.post("/auth/change-password")
async def change_password(request: Request, old_password: str = Form(...),
                          new_password: str = Form(...)):
    user = current_user(request)
    from auth import verify_password
    with get_db() as db:
        row = db.execute("SELECT hashed_pw FROM users WHERE id=?", (user["sub"],)).fetchone()
    if not row or not verify_password(old_password, row["hashed_pw"]):
        return {"ok": False, "error": "Wrong current password"}
    with get_db() as db:
        db.execute("UPDATE users SET hashed_pw=? WHERE id=?", (hash_password(new_password), user["sub"]))
    return RedirectResponse("/settings?msg=Password+changed", status_code=302)
