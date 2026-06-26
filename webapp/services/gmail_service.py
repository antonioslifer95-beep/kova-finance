"""Gmail API OAuth2 authentication and email sending with attachments."""
import base64, json, mimetypes
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from urllib.parse import urlencode

import requests

from database import setting, set_setting

GMAIL_AUTH_URL    = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_TOKEN_URL   = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL    = "https://www.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_PROFILE_URL = "https://www.googleapis.com/gmail/v1/users/me/profile"
GMAIL_SENDASME_URL = "https://www.googleapis.com/gmail/v1/users/me/settings/sendAs"
REDIRECT_URI      = "http://localhost:8080/settings/gmail/callback"
SCOPES            = (
    "https://www.googleapis.com/auth/gmail.send "
    "https://www.googleapis.com/auth/gmail.settings.basic"
)

# Gmail's hard cap is ~25MB on the full raw RFC2822 message *after* base64 encoding
# (base64 inflates binary data by ~4/3). 18MB of raw attachment bytes -> ~24MB encoded,
# leaving headroom for MIME headers/boundaries/body text.
SAFE_RAW_ATTACHMENT_BYTES = 18 * 1024 * 1024


def build_auth_url() -> str:
    params = {
        "client_id": setting("gmail_client_id"),
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",   # required to get a refresh_token
        "prompt": "consent",        # forces a refresh_token on every (re)connect
    }
    return f"{GMAIL_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> None:
    """Exchange an authorization code for access+refresh tokens, persist to settings."""
    resp = requests.post(GMAIL_TOKEN_URL, data={
        "code": code,
        "client_id": setting("gmail_client_id"),
        "client_secret": setting("gmail_client_secret"),
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    if "refresh_token" not in data:
        raise RuntimeError(
            "Google did not return a refresh token. Disconnect and reconnect "
            "(make sure to grant access when prompted)."
        )
    _persist_tokens(data)
    _fetch_and_store_connected_email(data["access_token"])
    err = fetch_and_store_signature(data["access_token"])
    if err:
        print(f"[gmail] signature fetch failed: {err}")


def _persist_tokens(data: dict) -> None:
    set_setting("gmail_access_token", data["access_token"])
    expiry = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))
    set_setting("gmail_token_expiry", expiry.isoformat())
    if "refresh_token" in data:
        set_setting("gmail_refresh_token", data["refresh_token"])


def _fetch_and_store_connected_email(access_token: str) -> None:
    try:
        resp = requests.get(
            GMAIL_PROFILE_URL,
            headers={"Authorization": f"Bearer {access_token}"}, timeout=10,
        )
        if resp.ok:
            set_setting("gmail_connected_email", resp.json().get("emailAddress", ""))
    except Exception:
        pass


def fetch_and_store_signature(access_token: str) -> str:
    """Fetch the primary sendAs address + signature from Gmail and persist both.
    Uses the sendAs list endpoint — no profile scope required.
    Returns empty string on success, or an error message on failure."""
    resp = requests.get(
        GMAIL_SENDASME_URL,
        headers={"Authorization": f"Bearer {access_token}"}, timeout=10,
    )
    if not resp.ok:
        return f"sendAs API error ({resp.status_code}): {resp.text[:200]}"
    send_as_list = resp.json().get("sendAs", [])
    primary = next((s for s in send_as_list if s.get("isPrimary")), None)
    if primary is None:
        primary = send_as_list[0] if send_as_list else None
    if primary is None:
        return "No sendAs addresses found in Gmail"
    email = primary.get("sendAsEmail", "")
    if email:
        set_setting("gmail_connected_email", email)
    set_setting("gmail_signature", primary.get("signature", ""))
    return ""


def get_valid_access_token() -> str:
    """Return a valid access token, refreshing via the refresh token if expired/near-expiry."""
    refresh_token = setting("gmail_refresh_token")
    if not refresh_token:
        raise RuntimeError("Gmail is not connected. Go to Settings and click 'Connect Gmail'.")

    expiry_str = setting("gmail_token_expiry")
    access_token = setting("gmail_access_token")
    if access_token and expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str)
        except ValueError:
            expiry = datetime.utcnow()
        if datetime.utcnow() < expiry - timedelta(seconds=60):
            return access_token

    resp = requests.post(GMAIL_TOKEN_URL, data={
        "refresh_token": refresh_token,
        "client_id": setting("gmail_client_id"),
        "client_secret": setting("gmail_client_secret"),
        "grant_type": "refresh_token",
    }, timeout=15)
    if not resp.ok:
        raise RuntimeError(
            "Gmail authorization expired or was revoked. Go to Settings and reconnect Gmail."
        )
    data = resp.json()
    _persist_tokens(data)
    return data["access_token"]


def check_size_guard(doc_rows: list) -> tuple[bool, int]:
    """Sum file_size of selected documents. Returns (ok, total_bytes)."""
    total = sum(int(d["file_size"] or 0) for d in doc_rows)
    return total <= SAFE_RAW_ATTACHMENT_BYTES, total


def build_message(to_email: str, subject: str, body: str, doc_rows: list) -> dict:
    """Build a base64url-encoded raw MIME message for the Gmail API 'raw' field."""
    msg = MIMEMultipart()
    msg["to"] = to_email
    msg["subject"] = subject

    signature = setting("gmail_signature") or ""
    body_html = (
        body
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )
    html = f'<div style="font-family:sans-serif;font-size:14px">{body_html}</div>'
    if signature:
        html += f"<br>{signature}"
    msg.attach(MIMEText(html, "html", "utf-8"))

    for d in doc_rows:
        path = Path(d["abs_path"])
        mime = d["mime_type"] or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        maintype, _, subtype = mime.partition("/")
        part = MIMEBase(maintype or "application", subtype or "octet-stream")
        with open(path, "rb") as f:
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=d["filename"])
        msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return {"raw": raw}


def send_email(to_email: str, subject: str, body: str, doc_rows: list) -> str:
    """Send via the Gmail API. Returns Gmail's message id on success. Raises on failure."""
    access_token = get_valid_access_token()
    message = build_message(to_email, subject, body, doc_rows)
    resp = requests.post(
        GMAIL_SEND_URL,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        data=json.dumps(message), timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"Gmail API error ({resp.status_code}): {resp.text[:300]}")
    return resp.json().get("id", "")
