"""Sign-in, sessions, roles and approval limits.

Why an approval tool needs this: an approval is only worth something if we know WHO approved,
and only the right people may approve (and only up to their limit). So:

  * Accounts are created by an admin. There is no public "sign up": strangers must never be able
    to create themselves an account in a finance approval tool.
  * Passwords are stored as scrypt hashes (salted, slow to brute-force), never as text.
  * After 5 wrong passwords an account is locked for 15 minutes.
  * A session is a random token in an HttpOnly, SameSite=Strict cookie (JavaScript cannot read it;
    other sites cannot send it). The database keeps only a hash of the token. Sessions expire after
    8 hours without activity.
  * Every change request must carry a custom header, which a forged cross-site form cannot add (CSRF).
  * Roles:  viewer   - can see everything, change nothing (auditors)
            approver - can upload, approve up to their approval limit, reject anything
            admin    - everything, plus users, vendors, purchase orders and demo data
  * Integrations (Power Automate, SharePoint) use a service token instead of a password.
  * Logins, failed logins, lockouts, password and user changes go to the audit log.

In InPro's real world the natural next step is "Sign in with Microsoft" (Entra ID single sign-on),
because InPro customers already have Microsoft 365 accounts; this module is where it would plug in.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import env, env_float

ROLES = ("viewer", "approver", "admin")
MAX_FAILED = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 10
COOKIE = "inpro_session"
CSRF_HEADER = "x-inpro-csrf"

PERMISSIONS = {
    "viewer": {"read"},
    "approver": {"read", "upload", "decide"},
    "service": {"read", "upload"},
    "admin": {"read", "upload", "decide", "manage"},
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ------------------------------------------------------------------ passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt, h = (stored or "").split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(h) // 2)
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


_DUMMY = hash_password(secrets.token_hex(8))       # used so unknown e-mails take as long as known ones


def password_problem(password: str) -> str | None:
    """Plain-language reason a new password is not acceptable, or None."""
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        return "Mix upper- and lower-case letters and at least one number."
    return None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_hours() -> float:
    return env_float("INPRO_SESSION_HOURS", 8)


def demo_mode() -> bool:
    return env("INPRO_DEMO_MODE", "1") == "1"


def can(user: dict[str, Any] | None, permission: str) -> bool:
    return bool(user) and permission in PERMISSIONS.get(user["role"], set())


def within_limit(user: dict[str, Any], amount: float | None) -> bool:
    lim = user.get("approval_limit")
    return lim is None or amount is None or float(amount) <= float(lim)


# ------------------------------------------------------------------ demo accounts

DEMO_USERS = [
    # name, email, role, approval limit (None = no limit), what it shows in the demo
    ("Priya Shetty", "priya@demo.inpro", "admin", None, "Finance manager: approves any amount, manages users"),
    ("Rahul Kamath", "rahul@demo.inpro", "approver", 500.0, "Approver: can approve up to 500"),
    ("Meera Nayak", "meera@demo.inpro", "viewer", None, "Auditor: can look, cannot change anything"),
]


def demo_password() -> str:
    return env("INPRO_DEMO_PASSWORD", "InPro-Demo-2026") or "InPro-Demo-2026"


def ensure_first_users(store) -> list[str]:
    """First start: create the demo accounts (demo mode) or one admin from .env settings."""
    if store.user_count():
        return []
    made = []
    if demo_mode():
        for name, email, role, limit, _ in DEMO_USERS:
            store.add_user(name, email, role, hash_password(demo_password()), limit)
            made.append(email)
    else:
        email, pw = env("INPRO_ADMIN_EMAIL"), env("INPRO_ADMIN_PASSWORD")
        if email and pw:
            store.add_user(env("INPRO_ADMIN_NAME", "Administrator"), email, "admin", hash_password(pw), None)
            made.append(email)
    return made


# ------------------------------------------------------------------ login / sessions

class LoginError(Exception):
    pass


def login(store, email: str, password: str, ip: str = "") -> tuple[dict[str, Any], str]:
    """Check the password and open a session. Returns (user, session token). Raises LoginError."""
    email = (email or "").strip().lower()
    u = store.user_by_email(email)
    now = utcnow()
    if u is None:
        verify_password(password, _DUMMY)            # same time as a real check: no account probing
        store.audit(None, email or "unknown", "login_failed", f"unknown account; ip {ip}")
        raise LoginError("Email or password is incorrect.")
    if not u["active"]:
        store.audit(None, email, "login_failed", f"account disabled; ip {ip}")
        raise LoginError("This account is disabled. Ask an administrator.")
    if u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > now:
        mins = max(1, int((datetime.fromisoformat(u["locked_until"]) - now).total_seconds() // 60) + 1)
        raise LoginError(f"Too many wrong passwords. Try again in {mins} minute{'s' if mins > 1 else ''}.")
    if not verify_password(password, u["password_hash"]):
        if env("INPRO_PUBLIC") == "1" and email in {e for _, e, *_ in DEMO_USERS}:
            # public demo: nobody may lock the shared demo accounts by guessing on purpose
            # (guessing is still slowed down per computer by the sign-in throttle)
            store.audit(None, email, "login_failed", f"ip {ip}")
            raise LoginError("Email or password is incorrect.")
        failed = (u["failed_logins"] or 0) + 1
        locked = iso(now + timedelta(minutes=LOCK_MINUTES)) if failed >= MAX_FAILED else None
        store.set_login_failure(u["id"], 0 if locked else failed, locked)
        store.audit(None, email, "account_locked" if locked else "login_failed", f"ip {ip}")
        if locked:
            raise LoginError(f"Too many wrong passwords. The account is locked for {LOCK_MINUTES} minutes.")
        left = MAX_FAILED - failed
        raise LoginError("Email or password is incorrect." + (f" {left} attempt{'s' if left > 1 else ''} left before a 15-minute lock." if left <= 2 else ""))
    token = secrets.token_urlsafe(32)
    store.create_session(token_hash(token), u["id"], iso(now), iso(now + timedelta(hours=session_hours())))
    store.set_login_success(u["id"], iso(now))
    store.audit(None, u["name"], "signed_in", f"{email}; ip {ip}")
    return store.user_by_id(u["id"]), token


def user_for_token(store, token: str | None) -> dict[str, Any] | None:
    """The signed-in user for this cookie, sliding the idle timeout forward. None if missing/expired."""
    if not token:
        return None
    s = store.get_session(token_hash(token))
    if not s:
        return None
    now = utcnow()
    if datetime.fromisoformat(s["expires_at"]) < now:
        store.delete_session(token_hash(token))
        return None
    u = store.user_by_id(s["user_id"])
    if not u or not u["active"]:
        return None
    if (now - datetime.fromisoformat(s["last_seen"])).total_seconds() > 60:
        store.touch_session(token_hash(token), iso(now), iso(now + timedelta(hours=session_hours())))
    return u


def service_user(bearer: str | None) -> dict[str, Any] | None:
    """Integrations (e.g. a Power Automate flow) send:  Authorization: Bearer <INPRO_SERVICE_TOKEN>."""
    expected = env("INPRO_SERVICE_TOKEN")
    if expected and bearer and hmac.compare_digest(bearer, expected):
        return {"id": 0, "name": "Integration (service token)", "email": "service", "role": "service",
                "approval_limit": None, "active": 1}
    return None


def public_user(u: dict[str, Any]) -> dict[str, Any]:
    return {k: u.get(k) for k in ("id", "name", "email", "role", "approval_limit", "active", "last_login", "created_at")}
