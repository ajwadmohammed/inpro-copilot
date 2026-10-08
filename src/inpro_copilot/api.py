"""The web service.

One upload endpoint does the whole job:

    POST /api/invoices   (a PDF or image)  ->  JSON with fields, checks, decision, trace

That is the integration point for InPro: a SharePoint workflow or a Power Automate flow
can call it with the scanned invoice (using the service token) and receive a structured
verdict, then route the task (auto-approve / send to approver with reasons / reject).

Everything else serves the screen. Every endpoint except sign-in requires a signed-in user,
and each action checks the user's role (see auth.py).
"""
from __future__ import annotations

import os
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import load_env

load_env()            # API keys and settings from .env (before anything reads them)

from . import auth, workflow  # noqa: E402
from .decision import Policy  # noqa: E402
from .pipeline import Pipeline  # noqa: E402
from .reader import OcrUnavailable  # noqa: E402
from .store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
UI_DIR = ROOT / "ui"
ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
MAX_BYTES = 15 * 1024 * 1024
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

def security_headers(public: bool) -> dict[str, str]:
    """Strict browser rules. No other website may show the app inside a frame, unless a host that needs it
    is named in INPRO_FRAME_ANCESTORS (e.g. https://huggingface.co for a Hugging Face Space page)."""
    allowed = (os.getenv("INPRO_FRAME_ANCESTORS") or "").strip()
    ancestors = allowed or "'none'"
    h = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            f"img-src 'self' data: blob:; connect-src 'self'; frame-ancestors {ancestors}; base-uri 'none'; form-action 'self'"
        ),
    }
    if not allowed:
        h["X-Frame-Options"] = "DENY"
    return h


# the interactive API docs (/docs, /redoc) load their viewer from a CDN
DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; worker-src blob:; connect-src 'self'; frame-ancestors 'none'")


class DecisionIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=1000)
    user: str = Field(default="reviewer", max_length=80)        # only used when sign-in is switched off


class RequestIn(BaseModel):
    item: str = Field(min_length=2, max_length=300)
    vendor: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    amount: float = Field(gt=0, lt=1e10)
    needed_by: str | None = Field(default=None, max_length=10)
    reason: str | None = Field(default=None, max_length=500)


class PrepareIn(BaseModel):
    vendor: str = Field(min_length=1, max_length=200)
    currency: str = Field(min_length=3, max_length=3)
    amount: float = Field(gt=0, lt=1e10)
    note: str | None = Field(default=None, max_length=500)


class RecordOrderIn(BaseModel):
    """An order the invoice quotes that was placed outside the app (by phone or e-mail)."""
    invoice_id: int
    item: str = Field(min_length=1, max_length=300)
    vendor: str = Field(min_length=1, max_length=200)
    currency: str = Field(min_length=3, max_length=3)
    amount: float = Field(gt=0, lt=1e10)
    reason: str = Field(min_length=1, max_length=500)


class RequestDecisionIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=500)


class PoIn(BaseModel):
    po_number: str = Field(min_length=1, max_length=40)
    vendor: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="INR", max_length=5)
    amount: float = Field(gt=0)


class VendorIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tax_id: str | None = Field(default=None, max_length=40)
    bank_account: str | None = Field(default=None, max_length=60)
    invoice_id: int | None = None                  # proposed from this invoice's review page


class VerifyIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=500)


class FieldsIn(BaseModel):
    changes: dict[str, str | float | int | None]
    note: str | None = Field(default=None, max_length=500)


class ReviewVendorIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tax_id: str | None = Field(default=None, max_length=40)
    bank_account: str | None = Field(default=None, max_length=60)


class ReviewIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=500)
    vendor: ReviewVendorIn | None = None            # a supplier that is not on the list yet, added with the review


class RemoveIn(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class ReceiptIn(BaseModel):
    ok: bool
    note: str | None = Field(default=None, max_length=500)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class SwitchIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)


class PasswordIn(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=1, max_length=200)


class UserIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=5, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: str = Field(pattern=r"^(ap|procurement|approver)$")
    approval_limit: float | None = Field(default=None, ge=0)
    password: str = Field(min_length=1, max_length=200)


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern=r"^(ap|procurement|approver)$")
    approval_limit: float | None = Field(default=None, ge=0)
    clear_limit: bool = False
    active: bool | None = None
    password: str | None = Field(default=None, max_length=200)


def _compact(inv: dict[str, Any]) -> dict[str, Any]:
    f = inv["fields"] or {}
    d = inv["decision"] or {}
    src = inv.get("source") or {}
    return {
        "stage": workflow.stage(inv), "uploaded_by": src.get("by"), "edited": bool(inv.get("edits")),
        "waived": (inv.get("waiver") or {}).get("fields") or [],
        "id": inv["id"], "filename": inv["filename"], "uploaded_at": inv["uploaded_at"],
        "vendor": f.get("vendor"), "invoice_number": f.get("invoice_number"), "invoice_date": f.get("invoice_date"),
        "total": f.get("total"), "currency": f.get("currency"),
        "ai_outcome": inv["ai_outcome"], "status": inv["status"], "decided_by": inv["decided_by"],
        "summary": d.get("summary"), "extractor": inv["extractor"],
        "flags": sum(1 for c in inv["checks"] or [] if c["status"] in ("warn", "fail")),
    }


def create_app(db_path: str | None = None, upload_dir: str | None = None, extractor: str | None = None,
               llm_client: Any = None, auth_enabled: bool | None = None) -> FastAPI:
    db_path = db_path or os.getenv("INPRO_DB", str(ROOT / "inpro.db"))
    upload_dir = upload_dir or os.getenv("INPRO_UPLOADS", str(ROOT / "uploads"))
    extractor = extractor or os.getenv("INPRO_EXTRACTOR", "auto")
    auth_on = (os.getenv("INPRO_AUTH", "1") == "1") if auth_enabled is None else auth_enabled
    public = os.getenv("INPRO_PUBLIC") == "1"          # the hosted demo on the internet (Hugging Face)
    headers = security_headers(public)
    store = Store(db_path)
    pipe = Pipeline(store, upload_dir, extractor=extractor,
                    policy=Policy(auto_approve_limit=float(os.getenv("INPRO_AUTO_LIMIT", "50000"))),
                    require_po=os.getenv("INPRO_REQUIRE_PO", "0") == "1", llm_client=llm_client,
                    require_receipt=os.getenv("INPRO_REQUIRE_RECEIPT", "0") == "1")
    if auth_on:
        auth.ensure_first_users(store)
    if auth.demo_mode():
        try:
            from .demo import refresh_demo_vendors
            refresh_demo_vendors(store, ROOT)          # demo data from an older version: complete the vendor list
        except Exception:
            pass

    app = FastAPI(title="InPro Copilot", version="0.2.0",
                  description="AI pre-review for invoice approvals: read, check, explain, recommend.")
    app.state.store, app.state.pipeline, app.state.auth_on = store, pipe, auth_on
    app.state.public = public
    demo_logins = {e for _, e, *_ in auth.DEMO_USERS}

    # public demo: a fresh server starts with the demo story already loaded (the disk there is not permanent)
    if (public and os.getenv("INPRO_AUTOSEED") != "0") or os.getenv("INPRO_AUTOSEED") == "1":
        import threading

        def _autoseed():
            try:
                if not store.list_invoices():
                    from .demo import seed_demo
                    seed_demo(pipe, ROOT)
            except Exception:
                pass
        threading.Thread(target=_autoseed, name="inpro-autoseed", daemon=True).start()

    # simple per-user rate limits for the public demo (uploads and forgeries cost CPU and sometimes AI)
    recent: dict[tuple[str, str], deque] = defaultdict(deque)

    def limit(user: dict[str, Any], kind: str, n: int, seconds: int) -> None:
        if not public:
            return
        q, t = recent[(kind, str(user.get("id")))], time.monotonic()
        while q and t - q[0] > seconds:
            q.popleft()
        if len(q) >= n:
            raise HTTPException(429, f"That's a lot in a short time. Wait {max(1, seconds // 60)} minutes and try again.")
        q.append(t)

    # sign-in switched off (tests, local experiments): everyone acts as a local administrator
    LOCAL = {"id": 0, "name": "Local user", "email": "local", "role": "local", "approval_limit": None, "active": 1}   # sign-in off: one person does everything

    # ---- notifications (notices.py): whoever's turn it is hears about it; whoever a decision affects hears the outcome
    from . import notices
    _label = notices.label

    def _stages() -> dict[int, str]:
        return notices.stages(store)

    def _announce(before: dict[int, str], actor: str | None) -> None:
        notices.announce(store, before, actor)

    def _tell(people, text: str, actor: str | None, invoice_id: int | None = None, link: str | None = None) -> None:
        notices.tell(store, people, text, actor, invoice_id, link)
    login_attempts: dict[str, deque] = defaultdict(deque)        # per-IP throttle for the sign-in form

    # ------------------------------------------------------------------ who is asking, and security headers
    @app.middleware("http")
    async def identify(request: Request, call_next):
        user = None
        bearer = request.headers.get("authorization", "")
        via_bearer = bearer.lower().startswith("bearer ")
        if not auth_on:
            user = LOCAL
        elif via_bearer:
            user = auth.service_user(bearer[7:].strip())
        else:
            user = auth.user_for_token(store, request.cookies.get(auth.COOKIE))
        request.state.user = user
        # CSRF: a change request sent with the session cookie must carry our custom header,
        # which a form or script on another website cannot add.
        if auth_on and request.method in UNSAFE and request.url.path.startswith("/api/") and not via_bearer:
            if request.headers.get(auth.CSRF_HEADER) != "1":
                return _secure(JSONResponse({"detail": "Request blocked: missing security header."}, status_code=403))
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
        return _secure(response, request.url.path)

    def _secure(response, path: str = ""):
        if path in ("/docs", "/redoc"):
            response.headers["Content-Security-Policy"] = DOCS_CSP
        for k, v in headers.items():
            response.headers.setdefault(k, v)
        return response

    def need(permission: str):
        def dep(request: Request) -> dict[str, Any]:
            u = request.state.user
            if u is None:
                raise HTTPException(401, "Please sign in.")
            if not auth.can(u, permission):
                raise HTTPException(403, f"Your role ({u['role']}) does not allow this.")
            return u
        return Depends(dep)

    # ------------------------------------------------------------------ sign-in
    @app.get("/api/auth/config")
    def auth_config():
        """What the sign-in screen needs to know. Demo accounts are listed only in demo mode."""
        demo = auth_on and auth.demo_mode()
        return {"auth": auth_on, "demo": demo, "sso": False, "public": public,
                "demo_accounts": [{"name": n, "email": e, "role": r, "note": note} for n, e, r, _, note in auth.DEMO_USERS] if demo else [],
                "demo_password": auth.demo_password() if demo else None}

    @app.post("/api/auth/login")
    def do_login(body: LoginIn, request: Request, response: Response):
        if not auth_on:
            return {"user": auth.public_user(LOCAL)}
        ip = request.client.host if request.client else ""
        q, now_s = login_attempts[ip], time.monotonic()
        while q and now_s - q[0] > 900:
            q.popleft()
        if len(q) >= 30:
            raise HTTPException(429, "Too many sign-in attempts from this computer. Wait 15 minutes.")
        q.append(now_s)
        try:
            user, token = auth.login(store, body.email, body.password, ip)
        except auth.LoginError as e:
            raise HTTPException(401, str(e))
        set_session_cookie(request, response, token)
        return {"user": auth.public_user(user)}

    def set_session_cookie(request: Request, response: Response, token: str) -> None:
        secure = os.getenv("INPRO_COOKIE_SECURE", "auto")
        response.set_cookie(auth.COOKIE, token, httponly=True, samesite="strict", path="/",
                            secure=(request.url.scheme == "https") if secure == "auto" else secure == "1",
                            max_age=int(auth.session_hours() * 3600))

    @app.post("/api/auth/switch")
    def switch_role(body: SwitchIn, request: Request, response: Response, user=need("read")):
        """Demo only: see the app as another demo person in one click, without signing out and in again.
        Works only between the built-in demo accounts, so it can never reach a real account."""
        if not auth_on or not auth.demo_mode() or not auth.is_demo_account(user.get("email")):
            raise HTTPException(404, "Switching roles is only available in the demo.")
        if not auth.is_demo_account(body.email):
            raise HTTPException(400, "That is not one of the demo accounts.")
        limit(user, "switch", 60, 600)
        target = store.user_by_email(body.email)
        if not target or not target["active"]:
            raise HTTPException(400, "That demo account is not available.")
        old = request.cookies.get(auth.COOKIE)
        if old:
            store.delete_session(auth.token_hash(old))
        set_session_cookie(request, response, auth.start_session(store, target))
        store.audit(None, user["name"], "switched_role", f"now viewing as {target['name']}")
        return {"user": auth.public_user(store.user_by_id(target["id"]))}

    @app.post("/api/auth/logout")
    def do_logout(request: Request, response: Response):
        token = request.cookies.get(auth.COOKIE)
        if token:
            u = request.state.user
            store.delete_session(auth.token_hash(token))
            if u:
                store.audit(None, u["name"], "signed_out", u["email"])
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(request: Request):
        """Who is signed in. {"user": null} when nobody is (not an error: the screen then shows sign-in)."""
        user = request.state.user
        if user is None:
            return {"user": None, "permissions": [], "auth": auth_on}
        perms = sorted(auth.PERMISSIONS.get(user["role"], set()))
        return {"user": auth.public_user(user), "permissions": perms, "auth": auth_on}

    @app.post("/api/auth/password")
    def change_password(body: PasswordIn, request: Request, user=need("read")):
        if not auth_on or not user.get("id"):
            raise HTTPException(400, "Password changes are not available for this account.")
        if public and user["email"] in demo_logins:
            raise HTTPException(400, "Demo accounts keep their password on the public demo, so every visitor can sign in.")
        full = store.user_by_id(user["id"])
        if not auth.verify_password(body.current, full["password_hash"]):
            raise HTTPException(400, "Your current password is not correct.")
        problem = auth.password_problem(body.new)
        if problem:
            raise HTTPException(400, problem)
        store.update_user(user["id"], password_hash=auth.hash_password(body.new))
        keep = request.cookies.get(auth.COOKIE)
        for_user = [s for s in [keep] if s]
        store.delete_sessions_for_user(user["id"])                       # sign out everywhere else ...
        if for_user:                                                     # ... but keep this browser signed in
            from datetime import timedelta
            now = auth.utcnow()
            store.create_session(auth.token_hash(for_user[0]), user["id"], auth.iso(now), auth.iso(now + timedelta(hours=auth.session_hours())))
        store.audit(None, user["name"], "password_changed", user["email"])
        return {"ok": True}

    # ------------------------------------------------------------------ users (admin)
    @app.get("/api/users")
    def list_users(user=need("manage")):
        return [auth.public_user(u) | {"locked": bool(u["locked_until"] and u["locked_until"] > auth.iso(auth.utcnow()))}
                for u in store.list_users()]

    @app.post("/api/users")
    def create_user(body: UserIn, user=need("manage")):
        if store.user_by_email(body.email):
            raise HTTPException(409, "There is already an account with this email.")
        if public and store.user_count() >= 25:
            raise HTTPException(400, "The public demo is limited to 25 accounts.")
        problem = auth.password_problem(body.password)
        if problem:
            raise HTTPException(400, problem)
        limit = body.approval_limit if body.role == "approver" else None
        uid = store.add_user(body.name, body.email, body.role, auth.hash_password(body.password), limit)
        store.audit(None, user["name"], "user_created", f"{body.email} as {body.role}")
        return auth.public_user(store.user_by_id(uid))

    @app.patch("/api/users/{uid}")
    def update_user(uid: int, body: UserPatch, user=need("manage")):
        target = store.user_by_id(uid)
        if not target:
            raise HTTPException(404, "User not found")
        if uid == user.get("id") and (body.active is False or (body.role and not auth.can({"role": body.role}, "manage"))):
            raise HTTPException(400, "You cannot disable or demote your own account. Ask another manager.")
        if public and target["email"] in demo_logins and (body.password or body.active is False or body.role
                                                              or body.clear_limit or body.approval_limit is not None):
            raise HTTPException(400, "The demo accounts stay as they are on the public demo, so every visitor gets the same tour. "
                                     "Create a new account to try these settings.")
        changes: dict[str, Any] = {}
        if body.role:
            changes["role"] = body.role
        if body.clear_limit:
            changes["approval_limit"] = None
        elif body.approval_limit is not None:
            changes["approval_limit"] = body.approval_limit
        if body.active is not None:
            changes["active"] = int(body.active)
        if body.password:
            problem = auth.password_problem(body.password)
            if problem:
                raise HTTPException(400, problem)
            changes.update(password_hash=auth.hash_password(body.password), failed_logins=0, locked_until=None)
        store.update_user(uid, **changes)
        if body.active is False or body.password:
            store.delete_sessions_for_user(uid)                          # takes effect immediately
        what = ", ".join(("new password" if k == "password_hash" else f"{k}={v}") for k, v in changes.items()
                         if k not in ("failed_logins", "locked_until"))
        store.audit(None, user["name"], "user_updated", f"{target['email']}: {what}")
        return auth.public_user(store.user_by_id(uid))

    # ------------------------------------------------------------------ system / AI
    @app.api_route("/api/health", methods=["GET", "HEAD"])
    def health(request: Request):
        u = request.state.user
        if u is None:
            return {"status": "ok"}
        from .llm import configured_tiers
        from .reader import ocr_available
        tiers = configured_tiers()
        mode = pipe.mode()
        return {"status": "ok", "mode": mode, "extractor": "rules" if mode == "rules" else "llm",
                "ai_models": [{"provider": t.provider, "model": t.model, "free": t.free} for t in tiers],
                "ocr": ocr_available(), "counts": store.counts(), "auth": auth_on}

    @app.get("/api/ai/usage")
    def ai_usage(user=need("read")):
        """How many AI requests were made, how many tokens, the estimated cost, and the limits."""
        from .config import env_float
        from .llm import configured_tiers
        out = store.usage_summary()
        out["limits"] = {"daily_requests": int(env_float("INPRO_LLM_DAILY_LIMIT", 300)),
                         "monthly_budget_usd": env_float("INPRO_LLM_MONTHLY_BUDGET_USD", 1.0)}
        out["mode"] = pipe.mode()
        out["models"] = [{"provider": t.provider, "model": t.model, "free": t.free} for t in configured_tiers()]
        return out

    @app.post("/api/ai/test")
    def ai_test(user=need("manage")):
        """Send a tiny request to every configured model: are the keys working?"""
        from .llm import configured_tiers, ping
        out = []
        for t in configured_tiers():
            ok, detail = ping(t)
            out.append({"provider": t.provider, "model": t.model, "free": t.free, "ok": ok, "detail": detail})
        return out

    @app.get("/api/activity")
    def activity(limit: int = 200, user=need("read")):
        return store.activity(max(1, min(limit, 1000)))

    # ------------------------------------------------------------------ invoices
    @app.post("/api/invoices")
    async def upload(file: UploadFile = File(...), user=need("upload")):
        limit(user, "upload", 40, 600)
        name = Path(file.filename or "upload").name
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED:
            raise HTTPException(415, f"Unsupported file type '{suffix}'. Use PDF, PNG, JPG or TIFF.")
        data = await file.read()
        if not data:
            raise HTTPException(400, "The uploaded file is empty.")
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "File is larger than 15 MB.")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / name
            p.write_bytes(data)
            try:
                before = _stages()
                rec = pipe.process(p, filename=name, uploaded_by=user["name"] if auth_on else None)
                _announce(before, user["name"])
            except OcrUnavailable as e:
                raise HTTPException(422, str(e))
            except Exception as e:  # corrupt PDF, unreadable image ...
                raise HTTPException(422, f"Could not read this document: {type(e).__name__}: {e}")
        rec["audit"] = store.audit_log(rec["id"])
        return rec

    @app.get("/api/invoices")
    def list_invoices(status: str | None = None, q: str | None = None, mine: bool = False, user=need("read")):
        if status and status not in {"pending", "approved", "rejected"}:
            raise HTTPException(400, "status must be pending, approved or rejected")
        rows = [_compact(i) for i in store.list_invoices(status) if not mine or workflow.is_task_for(user, i)]
        if q:
            ql = q.lower()
            rows = [r for r in rows if any(ql in str(r.get(k) or "").lower() for k in ("vendor", "invoice_number", "invoice_date", "filename"))]
        return rows

    @app.get("/api/invoices/{inv_id}")
    def get_invoice(inv_id: int, user=need("read")):
        inv = store.get_invoice(inv_id)
        if not inv:
            raise HTTPException(404, "Invoice not found")
        inv["audit"] = store.audit_log(inv_id)
        inv["pages"] = 1
        if str(inv["stored_path"]).lower().endswith(".pdf") and Path(inv["stored_path"]).exists():
            import pymupdf
            inv["pages"] = len(pymupdf.open(inv["stored_path"]))
        inv["you"] = workflow.describe(user, inv)
        from .checks import same_vendor
        rec = next((v for v in store.vendors() if same_vendor((inv["fields"] or {}).get("vendor"), v["name"], 85)), None)
        inv["vendor_record"] = rec
        return inv

    @app.get("/api/invoices/{inv_id}/vendor-suggestion")
    def vendor_suggestion(inv_id: int, user=need("read")):
        """The new-vendor card, pre-filled from what was read on the invoice, with each value checked."""
        from . import bank
        from .taxid import validate_tax_id
        inv = store.get_invoice(inv_id)
        if not inv:
            raise HTTPException(404, "Invoice not found")
        f = inv["fields"] or {}
        tax = None
        if f.get("tax_id"):
            r = validate_tax_id(f["tax_id"])
            tax = {"valid": r.valid, "kind": r.kind, "reason": r.reason}
        acct = None
        if f.get("bank_account"):
            number, _ = bank.parts(f["bank_account"])
            if bank.looks_like_iban(number):
                ok = bank.iban_valid(number)
                acct = {"valid": ok, "message": "IBAN checksum is valid" if ok else "IBAN fails its checksum: check for a typo"}
            else:
                acct = {"valid": None, "message": "Account number (no checksum to test): confirm it with the supplier"}
        return {"name": f.get("vendor"), "tax_id": f.get("tax_id"), "tax_check": tax, "bank_account": f.get("bank_account"),
                "bank_check": acct}

    @app.get("/api/invoices/{inv_id}/evidence")
    def invoice_evidence(inv_id: int, user=need("read")):
        """Proof on the document: where each problem is printed, and what it is compared with."""
        from .evidence import evidence
        inv = store.get_invoice(inv_id)
        if not inv:
            raise HTTPException(404, "Invoice not found")
        return {"items": evidence(store, inv)}

    @app.patch("/api/invoices/{inv_id}/fields")
    def correct_fields(inv_id: int, body: FieldsIn, user=need("edit")):
        inv = store.get_invoice(inv_id)
        if not inv:
            raise HTTPException(404, "Invoice not found")
        if not workflow.can_edit(user, inv):
            raise HTTPException(409, "Fields are corrected during the accounts payable review; after it, the reading is final.")
        try:
            before = _stages()
            rec = pipe.edit_fields(inv_id, body.changes, user["name"] if auth_on else "Local user", body.note)
            _announce(before, user["name"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        rec["audit"] = store.audit_log(inv_id)
        return rec

    @app.post("/api/invoices/{inv_id}/review")
    def review_invoice(inv_id: int, body: ReviewIn, user=need("review")):
        """Accounts payable approves (the invoice goes on, a new supplier is proposed with it) or rejects (closed with the
        reason, never paid; the record stays). The supplier, the amount and the currency are needed to approve."""
        inv = store.get_invoice(inv_id)
        if not inv:
            raise HTTPException(404, "Invoice not found")
        ok, why = workflow.can_review(user, inv)
        if not ok:
            raise HTTPException(409, why)
        name, note = user["name"] if auth_on else "Local user", (body.note or "").strip() or None
        if not body.approve and not note:
            raise HTTPException(400, "Say why the invoice is rejected, so the supplier can be told.")
        if body.approve:
            missing = workflow.missing_to_pay(inv)
            if missing:
                raise HTTPException(409, "Fill in the " + " and ".join(m.replace("_", " ").replace("total", "amount") for m in missing)
                                    + " first: they are needed to pay.")
            if workflow._kind(workflow._check(inv, "vendor")) == "new" and not (body.vendor and body.vendor.name.strip()):
                raise HTTPException(409, "This supplier is not on the vendor list: add it with the review.")
        before = _stages()
        try:
            rec = pipe.review(inv_id, name, body.approve, note, body.vendor.model_dump() if body.vendor and body.approve else None)
        except ValueError as e:
            raise HTTPException(400, str(e))
        _announce(before, name)
        rec = store.get_invoice(inv_id)
        rec["audit"] = store.audit_log(inv_id)
        rec["you"] = workflow.describe(user, rec)
        return rec

    @app.post("/api/invoices/{inv_id}/remove")
    def remove_invoice(inv_id: int, body: RemoveIn, user=need("read")):
        """Take an invoice that was uploaded by mistake out of the queue (accounts payable or a manager, with a reason,
        until a person decides it). It is never paid and counts nowhere, but its record and history stay."""
        inv = store.get_invoice(inv_id)
        if not inv:
            raise HTTPException(404, "Invoice not found")
        ok, why = workflow.can_remove(user, inv)
        if not ok:
            raise HTTPException(403 if "can remove" in why else 409, why)
        if not body.reason.strip():
            raise HTTPException(400, "Say why the invoice is removed.")
        name = user["name"] if auth_on else "Local user"
        store.remove_invoice(inv_id, name, body.reason.strip())
        store.audit(inv_id, name, "removed", body.reason.strip())
        order = inv.get("order_request") or {}
        if order.get("status") in ("requested", "prepared"):        # an order recorded only for this invoice
            store.update_request(order["id"], status="rejected", decided_by=name, decided_at=auth.iso(auth.utcnow()),
                                 note="Its invoice was removed")
            store.audit(inv_id, name, "po_rejected", f"request #{order['id']} closed: its invoice was removed")
        out = store.get_invoice(inv_id)
        out["you"] = workflow.describe(user, out)
        return out

    @app.post("/api/invoices/{inv_id}/receipt")
    def confirm_receipt(inv_id: int, body: ReceiptIn, user=need("receipt")):
        inv = store.get_invoice(inv_id)
        if not inv:
            raise HTTPException(404, "Invoice not found")
        if workflow.stage_key(inv) != "receipt":
            raise HTTPException(409, "This invoice is not waiting for a delivery confirmation.")
        if not body.ok and not (body.note or "").strip():
            raise HTTPException(400, "Say what the problem is, so the manager and the supplier know.")
        before = _stages()
        rec = pipe.confirm_receipt(inv_id, body.ok, user["name"] if auth_on else "Local user", (body.note or "").strip() or None)
        _announce(before, user["name"] if auth_on else None)
        return rec

    @app.get("/api/invoices/{inv_id}/file")
    def get_file(inv_id: int, user=need("read")):
        inv = store.get_invoice(inv_id)
        if not inv or not inv["stored_path"] or not Path(inv["stored_path"]).exists():
            raise HTTPException(404, "File not found")
        return FileResponse(inv["stored_path"], headers={"Content-Disposition": "inline", "Cache-Control": "private, no-store"})

    @app.get("/api/invoices/{inv_id}/page/{n}.png")
    def get_page(inv_id: int, n: int, user=need("read")):
        """PDF page as a picture, so the preview works in any browser (phones included)."""
        import pymupdf
        inv = store.get_invoice(inv_id)
        if not inv or not inv["stored_path"] or not Path(inv["stored_path"]).exists():
            raise HTTPException(404, "File not found")
        doc = pymupdf.open(inv["stored_path"])
        if not 0 <= n < len(doc):
            raise HTTPException(404, "No such page")
        png = doc[n].get_pixmap(dpi=130).tobytes("png")
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/invoices/{inv_id}/decision")
    def decide(inv_id: int, body: DecisionIn, user=need("decide")):
        inv = store.get_invoice(inv_id)
        if not inv:
            raise HTTPException(404, "Invoice not found")
        if inv.get("removed_at"):
            raise HTTPException(409, "This invoice was removed, so it is not paid.")
        if body.approve:
            ok, why = workflow.can_approve(user, inv)
            if not ok:
                raise HTTPException(403, why or "This invoice cannot be approved now.")
        actor = user["name"] if auth_on else body.user
        rec = pipe.human_decision(inv_id, body.approve, actor, body.note, workflow.open_items(inv) if body.approve else None)
        people = {(inv.get("source") or {}).get("by"), (inv.get("ap_review") or {}).get("by")}
        what = _label(inv)
        _tell(people, f"{actor} approved {what} for payment." if body.approve
              else f"{actor} rejected {what}" + (f": {body.note.strip()}" if (body.note or "").strip() else "."), actor, inv_id, f"#/invoice/{inv_id}")
        rec["audit"] = store.audit_log(inv_id)
        return rec

    @app.get("/api/stats")
    def stats(user=need("read")):
        rows = store.list_invoices()
        by_ai = {"auto_approve": 0, "needs_review": 0, "reject": 0}
        overrides = 0
        for r in rows:
            by_ai[r["ai_outcome"]] = by_ai.get(r["ai_outcome"], 0) + 1
            if r["decided_by"] and not str(r["decided_by"]).startswith("AI"):
                human_yes = r["status"] == "approved"
                if (r["ai_outcome"] == "auto_approve" and not human_yes) or (r["ai_outcome"] == "reject" and human_yes):
                    overrides += 1
        who = {"approved": {"checks": 0, "person": 0}, "rejected": {"checks": 0, "person": 0}}
        for r in rows:
            if r["status"] in who:
                who[r["status"]]["checks" if str(r["decided_by"] or "").startswith("AI") else "person"] += 1
        return {"total": len(rows), "by_status": store.counts(), "by_ai_outcome": by_ai, "human_overrides": overrides,
                "my_tasks": sum(1 for r in rows if workflow.is_task_for(user, r)),
                "my_requests": sum(1 for r in store.requests() if workflow.request_task_for(user, r)),
                "by_stage": {k: sum(1 for r in rows if workflow.stage_key(r) == k) for k in workflow.STAGES if k != "done"},
                "approved_by": who["approved"], "rejected_by": who["rejected"]}

    # ------------------------------------------------------------------ overview
    @app.get("/api/overview")
    def get_overview(user=need("read")):
        from .insights import overview
        return overview(store)

    # ------------------------------------------------------------------ master data
    @app.post("/api/purchase-orders")
    def add_po(body: PoIn, user=need("po_import")):
        """Load a purchase order that was already approved in the company's ERP (integrations only, with the service
        token). People never open an order directly: it starts as a purchase request and a manager approves it."""
        store.upsert_po(body.po_number.strip(), body.vendor.strip(), body.currency.upper(), body.amount)
        store.audit(None, user["name"], "po_saved", f"{body.po_number} {body.vendor} {body.amount:,.2f} {body.currency.upper()}")
        pipe.recheck_open(f"purchase order {body.po_number.strip()} saved")
        return {"ok": True}

    @app.get("/api/purchase-orders")
    def list_pos(user=need("read")):
        origin = {r["po_number"]: r for r in store.requests() if r.get("po_number")}
        return [{**p, "request_id": (origin.get(p["po_number"]) or {}).get("id")} for p in store.purchase_orders().values()]

    # ------------------------------------------------------------------ purchase requests
    @app.get("/api/purchase-requests")
    def list_requests(user=need("read")):
        return [workflow.describe_request(user, r) for r in store.requests()]

    @app.post("/api/purchase-requests")
    def add_request(body: RequestIn, user=need("request")):
        """Accounts payable asks for a purchase. It goes to procurement to prepare, then to a manager to approve."""
        import re as _re
        if body.needed_by and not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", body.needed_by):
            raise HTTPException(400, "Give the date as YYYY-MM-DD.")
        name = user["name"] if auth_on else "Local user"
        rid = store.add_request(requested_by=name, item=body.item.strip(), vendor=body.vendor.strip(), currency=body.currency.upper(),
                                amount=round(body.amount, 2), needed_by=body.needed_by, reason=(body.reason or "").strip() or None)
        store.audit(None, name, "po_requested", f"request #{rid}: {body.item.strip()} from {body.vendor.strip()}, "
                                               f"{body.amount:,.2f} {body.currency.upper()}")
        store.notify(f"New purchase request #{rid} to prepare: {body.item.strip()} from {body.vendor.strip()}, requested by {name}.",
                     role="procurement", actor=name, link="#/pos")
        return workflow.describe_request(user, store.request(rid))

    @app.post("/api/purchase-requests/after-the-fact")
    def record_order(body: RecordOrderIn, user=need("po")):
        """The invoice quotes an order that is not on file because it was placed outside the app. Procurement records
        it, with the reason; like every order it then needs a manager's approval (an after-the-fact purchase order)."""
        inv = store.get_invoice(body.invoice_id)
        if not inv:
            raise HTTPException(404, "Invoice not found")
        if workflow.stage_key(inv) != "po_missing":
            raise HTTPException(409, "This invoice is not waiting for its order to be recorded.")
        po = str((inv.get("fields") or {}).get("po_number") or "").strip()
        if not po:
            raise HTTPException(400, "The invoice does not quote a purchase order number.")
        if po in store.purchase_orders():
            raise HTTPException(409, f"Purchase order {po} is already on file.")
        if not body.reason.strip():
            raise HTTPException(400, "Say why it was ordered outside the app.")
        name = user["name"] if auth_on else "Local user"
        rid = store.add_request(requested_by=name, item=body.item.strip(), vendor=body.vendor.strip(), currency=body.currency.upper(),
                                amount=round(body.amount, 2), reason=body.reason.strip(), kind="after_the_fact",
                                invoice_id=inv["id"], po_number=po)
        before = _stages()
        store.update_request(rid, status="prepared", prepared_by=name, prepared_at=auth.iso(auth.utcnow()))
        _announce(before, name)
        store.audit(inv["id"], name, "po_recorded", f"request #{rid}: {po} from {body.vendor.strip()}, {body.amount:,.2f} "
                                                   f"{body.currency.upper()}, ordered outside the app: {body.reason.strip()}")
        return workflow.describe_request(user, store.request(rid))

    def _request(rid: int) -> dict[str, Any]:
        r = store.request(rid)
        if not r:
            raise HTTPException(404, "Purchase request not found")
        return r

    @app.post("/api/purchase-requests/{rid}/prepare")
    def prepare_request(rid: int, body: PrepareIn, user=need("po")):
        """Procurement confirms the supplier and the price, then sends the order to a manager."""
        r = _request(rid)
        if r["status"] != "requested":
            raise HTTPException(409, "This request is not waiting for procurement.")
        name = user["name"] if auth_on else "Local user"
        store.update_request(rid, vendor=body.vendor.strip(), currency=body.currency.upper(), amount=round(body.amount, 2),
                             status="prepared", prepared_by=name, prepared_at=auth.iso(auth.utcnow()),
                             note=(body.note or "").strip() or None)
        store.audit(None, name, "po_prepared", f"request #{rid}: {body.vendor.strip()}, {body.amount:,.2f} {body.currency.upper()}"
                                              + (f"; {body.note.strip()}" if (body.note or "").strip() else ""))
        store.notify(f"Order to approve: request #{rid}, {r['item']} from {body.vendor.strip()}, {body.amount:,.2f} {body.currency.upper()}.",
                     role="approver", actor=name, link="#/pos")
        return workflow.describe_request(user, store.request(rid))

    @app.post("/api/purchase-requests/{rid}/decision")
    def decide_request(rid: int, body: RequestDecisionIn, user=need("read")):
        """A manager approves (which creates the purchase order) or rejects. Procurement may reject before preparing."""
        r = _request(rid)
        name = user["name"] if auth_on else "Local user"
        info = workflow.describe_request(user, r)
        if body.approve:
            ok, why = workflow.can_approve_request(user, r)
            if not ok:
                raise HTTPException(403, why)
            po = r.get("po_number") or store.next_po_number()       # an order placed outside the app keeps its number
            if po in store.purchase_orders():
                raise HTTPException(409, f"Purchase order {po} is already on file.")
            store.upsert_po(po, r["vendor"], r["currency"], r["amount"])
            store.update_request(rid, status="approved", decided_by=name, decided_at=auth.iso(auth.utcnow()), po_number=po,
                                 note=(body.note or "").strip() or r.get("note"))
            store.audit(r.get("invoice_id"), name, "po_approved",
                        f"request #{rid} approved: {po} for {r['vendor']}, {r['amount']:,.2f} {r['currency']}")
            before = _stages()
            pipe.recheck_open(f"purchase order {po} created")
            _announce(before, name)
            _tell([r.get("requested_by"), r.get("prepared_by")], f"{name} approved request #{rid}: purchase order {po} is open.", name, link="#/pos")
        else:
            if not info["can_reject"]:
                raise HTTPException(403, "Procurement can reject a request before preparing it; a manager after.")
            if not (body.note or "").strip():
                raise HTTPException(400, "Give a reason for rejecting the request.")
            store.update_request(rid, status="rejected", decided_by=name, decided_at=auth.iso(auth.utcnow()), note=body.note.strip())
            store.audit(r.get("invoice_id"), name, "po_rejected", f"request #{rid} rejected: {body.note.strip()}")
            _tell([r.get("requested_by"), r.get("prepared_by")], f"{name} rejected request #{rid}: {body.note.strip()}", name, link="#/pos")
            inv = store.get_invoice(r["invoice_id"]) if r.get("invoice_id") else None
            if inv and inv["status"] == "pending":       # an order nobody approved is not paid: its invoice is rejected too
                pipe.human_decision(inv["id"], False, name, f"Order {r.get('po_number')} was not approved: {body.note.strip()}")
        return workflow.describe_request(user, store.request(rid))

    @app.post("/api/vendors")
    def add_vendor(body: VendorIn, user=need("vendor_propose")):
        """Add a supplier. Nobody creates and activates a supplier alone: accounts payable PROPOSES it (procurement then
        verifies it), procurement adds it as VERIFIED, and in both cases a manager approves it before it is active.
        A local single-user setup adds it directly."""
        from .checks import same_vendor
        name_by = user["name"] if auth_on else "Local user"
        verifier = auth.can(user, "vendor_verify")
        existing = next((v for v in store.vendors() if same_vendor(body.name.strip(), v["name"], 92)), None)
        if existing and not verifier:
            raise HTTPException(409, f"{existing['name']} is already on the vendor list. Changing an existing supplier's details, "
                                     "such as its bank account, needs procurement.")
        status = "approved" if user["role"] == "local" else "verified" if verifier else "pending"
        before = _stages()
        try:
            v = pipe.save_vendor(existing["name"] if existing else body.name.strip(), body.tax_id, body.bank_account,
                                 by=name_by, status=status, invoice_id=body.invoice_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        pipe.recheck_open(f"supplier {v['name']} added")
        if status == "verified":
            store.notify(f"New supplier to approve: {v['name']}, added by {name_by}.", role="approver", actor=name_by, link="#/vendors")
        elif status == "pending":
            store.notify(f"New supplier to verify: {v['name']}, proposed by {name_by}.", role="procurement", actor=name_by, link="#/vendors")
        _announce(before, name_by)
        return {"ok": True, "status": status, "vendor": v}

    @app.post("/api/vendors/{vid}/verify")
    def verify_vendor(vid: int, body: VerifyIn, user=need("vendor_verify")):
        """Procurement checks a proposed supplier is real (by phone, on a known number) and sends it to a manager."""
        v = store.vendor(vid)
        if not v:
            raise HTTPException(404, "Vendor not found")
        if v["status"] != "pending":
            raise HTTPException(409, f"{v['name']} is not waiting for verification.")
        name = user["name"] if auth_on else "Local user"
        if auth_on and v.get("proposed_by") == name:
            raise HTTPException(403, "You proposed this supplier, so someone else must verify it (segregation of duties).")
        if not body.approve and not (body.note or "").strip():
            raise HTTPException(400, "Give a reason for rejecting the supplier.")
        status = ("approved" if user["role"] == "local" else "verified") if body.approve else "rejected"
        before = _stages()
        store.set_vendor_status(vid, status, name, (body.note or "").strip() or None)
        store.audit(None, name, "vendor_verified" if body.approve else "vendor_rejected",
                    v["name"] + (f": {body.note.strip()}" if (body.note or "").strip() else ""))
        n = pipe.recheck_open(f"supplier {v['name']} {status}")
        _tell([v.get("proposed_by")], f"{name} verified the supplier {v['name']}. It is now with the manager for approval."
              if body.approve else f"{name} rejected the supplier {v['name']}: {(body.note or '').strip()}", name, link="#/vendors")
        _announce(before, name)
        return {"ok": True, "status": status, "rechecked": n}

    @app.post("/api/vendors/{vid}/approve")
    def approve_vendor(vid: int, body: VerifyIn, user=need("decide")):
        """The manager's approval of a supplier procurement verified. Only an approved supplier is active and payable."""
        v = store.vendor(vid)
        if not v:
            raise HTTPException(404, "Vendor not found")
        if v["status"] != "verified":
            raise HTTPException(409, f"{v['name']} is not waiting for a manager's approval.")
        name = user["name"] if auth_on else "Local user"
        if auth_on and name in {v.get("proposed_by"), v.get("verified_by")}:
            raise HTTPException(403, "You proposed or verified this supplier, so another person must approve it (segregation of duties).")
        if not body.approve and not (body.note or "").strip():
            raise HTTPException(400, "Give a reason for rejecting the supplier.")
        before = _stages()
        store.set_vendor_approval(vid, body.approve, name, (body.note or "").strip() or None)
        store.audit(None, name, "vendor_approved" if body.approve else "vendor_rejected",
                    v["name"] + (f": {body.note.strip()}" if (body.note or "").strip() else ""))
        n = pipe.recheck_open(f"supplier {v['name']} {'approved' if body.approve else 'rejected'}")
        _tell([v.get("proposed_by"), v.get("verified_by")],
              f"{name} approved the supplier {v['name']}. It is on the vendor list now." if body.approve
              else f"{name} rejected the supplier {v['name']}: {(body.note or '').strip()}", name, link="#/vendors")
        _announce(before, name)
        return {"ok": True, "status": "approved" if body.approve else "rejected", "rechecked": n}

    # ---- notifications
    @app.get("/api/notifications")
    def notifications(user=need("read")):
        key = user.get("email") or user["name"]
        items = store.notifications_for(user["role"], user["name"])
        seen = store.last_seen(key)
        return {"items": [{**n, "unread": n["id"] > seen} for n in items], "unread": sum(1 for n in items if n["id"] > seen)}

    @app.post("/api/notifications/seen")
    def notifications_seen(user=need("read")):
        items = store.notifications_for(user["role"], user["name"], limit=1)
        store.mark_seen(user.get("email") or user["name"], items[0]["id"] if items else 0)
        return {"ok": True}

    @app.get("/api/vendors")
    def list_vendors(user=need("read")):
        return store.vendors()

    # ------------------------------------------------------------------ demo data (admin)
    @app.post("/api/demo/seed")
    def seed(user=need("manage")):
        from .demo import seed_demo
        return seed_demo(pipe, ROOT)

    @app.post("/api/demo/reset")
    def reset(user=need("manage")):
        """Start over: removes all invoices and their stored files (the AI cache and sign-in history are kept).
        On the public demo it then loads the starting story again, so the next visitor gets the full tour."""
        limit(user, "reset", 6, 600)
        store.reset()
        for f in Path(pipe.upload_dir).glob("*"):
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass
        store.audit(None, user["name"], "demo_reset", "all invoices removed")
        if public:
            from .demo import seed_demo
            seed_demo(pipe, ROOT)
            return {"ok": True, "reseeded": True}
        return {"ok": True}

    @app.exception_handler(Exception)
    async def unhandled(_, exc: Exception):  # never leak a stack trace to the browser
        return JSONResponse({"detail": f"Internal error: {type(exc).__name__}"}, status_code=500)

    if UI_DIR.exists():
        @app.api_route("/", methods=["GET", "HEAD"])
        def index():
            return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app() if os.getenv("INPRO_NO_AUTOAPP") != "1" else None
