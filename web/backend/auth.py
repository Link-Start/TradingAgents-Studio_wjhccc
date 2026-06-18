"""Cookie-based session auth for the Web Studio.

Replaces the old HTTP Basic Auth (which prompted the browser's native dialog and
behaved unreliably with the SPA's burst of parallel XHRs — some requests reached
the server without the Authorization header and got 401). Instead the frontend
posts credentials once to ``/api/auth/login``; on success we set a signed,
HttpOnly cookie that every same-origin request carries automatically.

The session token is **stateless**: a base64url JSON payload ``{u, exp}`` plus an
HMAC-SHA256 signature. The signing key is derived from
``TRADINGAGENTS_WEB_PASSWORD`` so:
  * tokens survive a server restart (no in-memory session store to lose), and
  * changing the password invalidates every existing session.

Auth is OFF when ``TRADINGAGENTS_WEB_PASSWORD`` is unset/blank (local dev),
matching the previous behaviour.
"""

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "ta_session"
# 30-day session by default; override with TRADINGAGENTS_WEB_SESSION_TTL (seconds).
_SESSION_TTL = int(os.getenv("TRADINGAGENTS_WEB_SESSION_TTL", str(30 * 24 * 3600)))


def _user() -> str:
    return os.getenv("TRADINGAGENTS_WEB_USER", "admin")


def _password() -> str:
    return os.getenv("TRADINGAGENTS_WEB_PASSWORD") or ""


def auth_required() -> bool:
    """True when a web password is configured. Read live so a restart picks up env changes."""
    return bool(_password().strip())


def _secret_key() -> bytes:
    # Derive a stable signing key from the password (salted so the raw password
    # is never used directly as the key).
    return hashlib.sha256(b"ta-session-v1|" + _password().encode("utf-8")).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str) -> str:
    return _b64e(hmac.new(_secret_key(), body.encode("ascii"), hashlib.sha256).digest())


def issue_token(user: str) -> str:
    payload = {"u": user, "exp": int(time.time()) + _SESSION_TTL}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_sign(body)}"


def verify_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    body, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, _sign(body)):
        return False
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return False
    return int(payload.get("exp", 0)) > int(time.time())


def is_authenticated(request: Request) -> bool:
    """True if auth is disabled, or the request carries a valid session cookie."""
    if not auth_required():
        return True
    return verify_token(request.cookies.get(COOKIE_NAME, ""))


def _set_cookie(response: Response, token: str) -> None:
    # Secure is intentionally omitted: deployments run over plain http://IP:8000,
    # where a Secure cookie would never be stored. SameSite=Lax is fine since the
    # SPA is same-origin with the API. HttpOnly keeps JS from reading the token.
    response.set_cookie(
        COOKIE_NAME, token, max_age=_SESSION_TTL,
        httponly=True, samesite="lax", path="/",
    )


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


@router.get("/status")
async def status(request: Request):
    """Lets the SPA decide whether to show the login page."""
    return {"auth_required": auth_required(), "authenticated": is_authenticated(request)}


@router.post("/login")
async def login(body: LoginBody, response: Response):
    if not auth_required():
        # No password configured → nothing to log into.
        return {"ok": True, "auth_required": False}
    # Compare on UTF-8 bytes (compare_digest rejects non-ASCII str), constant-time.
    user_ok = hmac.compare_digest(body.username.encode("utf-8"), _user().encode("utf-8"))
    pw_ok = hmac.compare_digest(body.password.encode("utf-8"), _password().encode("utf-8"))
    if not (user_ok and pw_ok):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    _set_cookie(response, issue_token(body.username))
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
