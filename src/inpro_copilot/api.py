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

from . import auth  # noqa: E402
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
    """Strict browser rules. On the public demo (Hugging Face) the app may be shown inside the
    huggingface.co Space page, so framing is allowed for that one site and nobody else."""
    ancestors = "https://huggingface.co" if public else "'none'"
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
    if not public:
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


class PoIn(BaseModel):
    po_number: str = Field(min_length=1, max_length=40)
    vendor: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="INR", max_length=5)
    amount: float = Field(gt=0)


class VendorIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tax_id: str | None = Field(default=None, max_length=40)
    bank_account: str | None = Field(default=None, max_length=60)
    email_domains: str | None = Field(default=None, max_length=300)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class PasswordIn(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=1, max_length=200)


class UserIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=5, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: str = Field(pattern=r"^(viewer|approver|admin)$")
    approval_limit: float | None = Field(default=None, ge=0)
    password: str = Field(min_length=1, max_length=200)


class LabIn(BaseModel):
    base: str = Field(min_length=1, max_length=40)
    trick: str = Field(min_length=1, max_length=40)
    value: str | None = Field(default=None, max_length=80)


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern=r"^(viewer|approver|admin)$")
    approval_limit: float | None = Field(default=None, ge=0)
    clear_limit: bool = False
    active: bool | None = None
    password: str | None = Field(default=None, max_length=200)


def _compact(inv: dict[str, Any]) -> dict[str, Any]:
    f = inv["fields"] or {}
    d = inv["decision"] or {}
    return {
        "id": inv["id"], "filename": inv["filename"], "uploaded_at": inv["uploaded_at"],
        "vendor": f.get("vendor"), "invoice_number": f.get("invoice_number"), "invoice_date": f.get("invoice_date"),
        "total": f.get("total"), "currency": f.get("currency"),
        "ai_outcome": inv["ai_outcome"], "status": inv["status"], "decided_by": inv["decided_by"],
        "summary": d.get("summary"), "extractor": inv["extractor"],
        "flags": sum(1 for c in inv["checks"] or [] if c["status"] in ("warn", "fail")),
        "lab": bool((inv.get("source") or {}).get("lab")),
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
                    require_po=os.getenv("INPRO_REQUIRE_PO", "0") == "1", llm_client=llm_client)
    if auth_on:
        auth.ensure_first_users(store)
    from .intake import Intake
    intake = Intake(pipe, store)
    if os.getenv("INPRO_INTAKE", "1") == "1":
        intake.start()                       # watches the inbox folder (and the mailbox, if configured)

    app = FastAPI(title="InPro Copilot", version="0.2.0",
                  description="AI pre-review for invoice approvals: read, check, explain, recommend.")
    app.state.store, app.state.pipeline, app.state.auth_on, app.state.intake = store, pipe, auth_on, intake
    app.state.public = public
    demo_emails_set = {e for _, e, *_ in auth.DEMO_USERS}

    # public demo: a fresh server starts with the demo story already loaded (the disk there is not permanent)
    if (public and os.getenv("INPRO_AUTOSEED") != "0") or os.getenv("INPRO_AUTOSEED") == "1":
        import threading

        def _autoseed():
            try:
                if not store.list_invoices():
                    from .demo import seed_demo, write_demo_emails
                    seed_demo(pipe, ROOT)
                    write_demo_emails(intake, ROOT)
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
    LOCAL = {"id": 0, "name": "Local user", "email": "local", "role": "admin", "approval_limit": None, "active": 1}
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
        secure = os.getenv("INPRO_COOKIE_SECURE", "auto")
        response.set_cookie(auth.COOKIE, token, httponly=True, samesite="strict", path="/",
                            secure=(request.url.scheme == "https") if secure == "auto" else secure == "1",
                            max_age=int(auth.session_hours() * 3600))
        return {"user": auth.public_user(user)}

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
        if public and user["email"] in demo_emails_set:
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
        if uid == user.get("id") and (body.active is False or (body.role and body.role != "admin")):
            raise HTTPException(400, "You cannot disable or demote your own account. Ask another admin.")
        if public and target["email"] in demo_emails_set and (body.password or body.active is False or body.role
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
                rec = pipe.process(p, filename=name, uploaded_by=user["name"] if auth_on else None)
            except OcrUnavailable as e:
                raise HTTPException(422, str(e))
            except Exception as e:  # corrupt PDF, unreadable image ...
                raise HTTPException(422, f"Could not read this document: {type(e).__name__}: {e}")
        rec["audit"] = store.audit_log(rec["id"])
        return rec

    @app.get("/api/invoices")
    def list_invoices(status: str | None = None, q: str | None = None, user=need("read")):
        if status and status not in {"pending", "approved", "rejected"}:
            raise HTTPException(400, "status must be pending, approved or rejected")
        rows = [_compact(i) for i in store.list_invoices(status)]
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
        total = (inv["fields"] or {}).get("total")
        inv["you"] = {"can_decide": auth.can(user, "decide"), "within_limit": auth.within_limit(user, total),
                      "approval_limit": user.get("approval_limit")}
        return inv

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
        total = (inv["fields"] or {}).get("total")
        if body.approve and not auth.within_limit(user, total):
            raise HTTPException(403, f"This invoice ({total:,.2f}) is above your approval limit ({user['approval_limit']:,.2f}). "
                                     "An approver with a higher limit, such as a finance manager, must approve it.")
        actor = user["name"] if auth_on else body.user
        rec = pipe.human_decision(inv_id, body.approve, actor, body.note)
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
                "approved_by": who["approved"], "rejected_by": who["rejected"]}

    # ------------------------------------------------------------------ intake: e-mail and watched folder
    def _intake_rows(limit: int = 100) -> list[dict[str, Any]]:
        rows = store.intake_log(limit)
        for r in rows:
            r["sender_check"] = r["bank_check"] = None
            if r.get("invoice_id"):
                inv = store.get_invoice(r["invoice_id"])
                for c in (inv or {}).get("checks") or []:
                    if c["name"] in ("sender", "bank"):
                        r[c["name"] + "_check"] = {"status": c["status"], "message": c["message"], "kind": (c.get("details") or {}).get("kind")}
        return rows

    @app.get("/api/intake")
    def intake_status(user=need("read")):
        imap = intake.imap_settings()
        return {"public": public, "folder": None if public else str(intake.folder.resolve()),
                "interval_seconds": intake.interval, "running": intake.running,
                "last_run": intake.last_run, "last_error": intake.last_error,
                "imap": {"configured": bool(imap), "host": imap["host"] if imap else None, "user": imap["user"] if imap else None,
                         "folder": imap["folder"] if imap else None},
                "log": _intake_rows()}

    @app.post("/api/intake/scan")
    def intake_scan(user=need("upload")):
        return {"results": intake.run_once()}

    @app.post("/api/intake/email")
    async def intake_email(file: UploadFile = File(...), user=need("upload")):
        limit(user, "upload", 40, 600)
        name = Path(file.filename or "message.eml").name
        if Path(name).suffix.lower() != ".eml":
            raise HTTPException(415, "Upload a saved e-mail (.eml). In Outlook or Gmail: open the message, then Save as / Download message.")
        data = await file.read()
        if not data or len(data) > 4 * MAX_BYTES:
            raise HTTPException(400, "The e-mail file is empty or too large.")
        return {"results": intake.process_email_bytes(data)}

    @app.post("/api/demo/emails")
    def demo_emails(user=need("manage")):
        from .demo import write_demo_emails
        return {"results": write_demo_emails(intake, ROOT)}

    # ------------------------------------------------------------------ fraud lab
    @app.get("/api/lab")
    def lab_info(user=need("read")):
        from . import lab
        return {"bases": lab.bases(), "tricks": lab.TRICKS, **lab.attempts(store)}

    @app.get("/api/lab/base/{key}.png")
    def lab_base_image(key: str, small: int = 0, page: int = 0, user=need("read")):
        from . import lab
        if key not in lab.BASES:
            raise HTTPException(404, "Unknown invoice")
        try:
            png = lab.render_base(key, small=bool(small), page=page)
        except IndexError:
            raise HTTPException(404, "No such page")
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/lab/forge")
    def lab_forge(body: LabIn, user=need("upload")):
        from . import lab
        limit(user, "lab", 40, 600)
        try:
            return lab.forge(pipe, body.base, body.trick, body.value, by=user["name"] if auth_on else "Local user")
        except lab.LabError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------------ overview
    @app.get("/api/overview")
    def get_overview(user=need("read")):
        from .insights import overview
        return overview(store)

    # ------------------------------------------------------------------ master data
    @app.post("/api/purchase-orders")
    def add_po(body: PoIn, user=need("manage")):
        store.upsert_po(body.po_number.strip(), body.vendor.strip(), body.currency.upper(), body.amount)
        store.audit(None, user["name"], "po_saved", f"{body.po_number} {body.vendor} {body.amount:,.2f} {body.currency.upper()}")
        return {"ok": True}

    @app.get("/api/purchase-orders")
    def list_pos(user=need("read")):
        return list(store.purchase_orders().values())

    @app.post("/api/vendors")
    def add_vendor(body: VendorIn, user=need("manage")):
        from . import bank
        from .sender import registrable
        acct = (body.bank_account or "").strip() or None
        if acct:
            number, ifsc = bank.parts(acct)
            if bank.looks_like_iban(number) and not bank.iban_valid(number):
                raise HTTPException(400, f"IBAN {bank.pretty(number)} fails the IBAN checksum. Check it for a typo.")
            acct = number + (f" / {ifsc}" if ifsc else "")
        doms = ",".join(sorted({registrable(d.strip().lstrip("@").lower()) for d in (body.email_domains or "").replace(";", ",").split(",") if d.strip()})) or None
        store.add_vendor(body.name.strip(), (body.tax_id or "").strip() or None, acct, doms)
        store.audit(None, user["name"], "vendor_saved", ", ".join(x for x in (body.name, body.tax_id, acct, doms) if x))
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
            from .demo import seed_demo, write_demo_emails
            seed_demo(pipe, ROOT)
            write_demo_emails(intake, ROOT)
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
