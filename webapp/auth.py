from datetime import datetime, timedelta
from typing import Optional
from fastapi import Request, HTTPException, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from config import SECRET_KEY, ALGORITHM, TOKEN_EXPIRE_H
from database import get_db

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(pw: str) -> str:
    return pwd_ctx.hash(pw)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)

def create_token(user_id: int, username: str, role: str) -> str:
    exp = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_H)
    return jwt.encode(
        {"sub": str(user_id), "username": username, "role": role, "exp": exp},
        SECRET_KEY, algorithm=ALGORITHM
    )

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return {}

def get_token(request: Request) -> Optional[str]:
    cookie = request.cookies.get("kova_token")
    if cookie:
        return cookie
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    # Query param fallback — used by iframe/embedded previews where cookies aren't sent
    return request.query_params.get("token") or None

def current_user(request: Request) -> dict:
    token = get_token(request)
    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return payload

def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user

def authenticate(username: str, password: str) -> Optional[dict]:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM users WHERE username=? AND is_active=1", (username,)
        ).fetchone()
    if row and verify_password(password, row["hashed_pw"]):
        return dict(row)
    return None

def ensure_admin_exists():
    """Create default admin on first run."""
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            db.execute(
                "INSERT INTO users(username,email,hashed_pw,role) VALUES(?,?,?,?)",
                ("admin", "admin@kova.local", hash_password("kova2024"), "admin")
            )
            print("\n[KOVA] Default admin created: username=admin  password=kova2024")
            print("[KOVA] Please change the password in Settings after first login.\n")
