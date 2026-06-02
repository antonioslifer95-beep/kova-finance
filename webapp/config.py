from pathlib import Path
import secrets, os

BASE_DIR  = Path(r"C:\Users\anton\Desktop\Kova Finance")
WEBAPP_DIR = BASE_DIR / "webapp"
DB_PATH   = WEBAPP_DIR / "kova.db"
STATIC_DIR = WEBAPP_DIR / "static"
TEMPLATE_DIR = WEBAPP_DIR / "templates"

# Generate and persist a secret key so JWTs survive restarts
_KEY_FILE = WEBAPP_DIR / ".secret_key"
if _KEY_FILE.exists():
    SECRET_KEY = _KEY_FILE.read_text().strip()
else:
    SECRET_KEY = secrets.token_hex(32)
    _KEY_FILE.write_text(SECRET_KEY)

ALGORITHM       = "HS256"
TOKEN_EXPIRE_H  = 8

STANDARD_FOLDERS = [
    "Documentos Pessoais", "Rendimentos", "Extratos Bancários",
    "IRS", "Imóvel", "Mapa CRC", "RGPD", "Proposta Crédito",
]
REQUIRED_CATEGORIES = {"Documentos Pessoais", "Rendimentos", "Mapa CRC", "RGPD"}

SKIP_NAMES = {".claude", "standby", "despesas valencia", "nova pasta", "_claude_review", "webapp"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
DOC_EXTS   = {".pdf"} | IMAGE_EXTS
