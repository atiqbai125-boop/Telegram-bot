from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from config import get_settings
from services.user_account import UserAccountController

settings = get_settings()
logger = logging.getLogger("song_bot.dashboard")
account = UserAccountController(settings)
failed_logins: dict[str, deque[float]] = defaultdict(deque)


def _cookie_secret() -> str:
    path = settings.data_dir / "dashboard_cookie.key"
    try:
        key = path.read_text(encoding="ascii").strip()
        if len(key) >= 48:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fp:
        fp.write(key)
    os.chmod(path, 0o600)
    return key


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await account.startup()
    yield
    if account.client:
        try:
            await account.client.disconnect()
        except Exception:
            pass


app = FastAPI(title="Song Dashboard", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=_cookie_secret(),
    session_cookie="song_dashboard_session",
    max_age=60 * 60 * 8,
    same_site="strict",
    https_only=False,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


CSS = """
:root{color-scheme:dark;--bg:#090e18;--panel:#101a2a;--line:#26354a;--muted:#9aabc2;--text:#f3f7ff;--cyan:#63e6d3;--blue:#76a9ff;--pink:#f19bb5;--green:#7fe0a5}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 12% 0%,#16283f 0%,transparent 46%),radial-gradient(ellipse at 100% 100%,#1b2542 0%,transparent 42%),var(--bg);color:var(--text);font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;min-height:100vh;padding:26px 16px 48px}.wrap{max-width:880px;margin:0 auto}.brand{display:flex;align-items:center;gap:13px;margin:8px 0 30px}.logo{width:46px;height:46px;border-radius:15px;background:linear-gradient(135deg,#62e6d3,#6094ff);display:grid;place-items:center;font-size:23px;box-shadow:0 10px 30px #4ea9dc35}.brand h1{font-size:18px;margin:0;letter-spacing:.02em}.brand p{color:var(--muted);font-size:12px;margin:1px 0}.hero{margin:10px 0 22px}.eyebrow{color:var(--cyan);font-size:11px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}.hero h2{font-size:clamp(27px,7vw,40px);letter-spacing:-.045em;line-height:1.12;margin:8px 0 9px}.hero p{margin:0;color:var(--muted);max-width:650px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{border:1px solid var(--line);background:linear-gradient(145deg,#121e30ed,#0f1725f0);border-radius:20px;padding:21px;box-shadow:0 18px 46px #0003}.card.wide{grid-column:1/-1}.card h3{font-size:17px;margin:0 0 5px}.sub{color:var(--muted);font-size:13px;margin:0 0 17px}.status{display:flex;align-items:center;gap:11px;padding:13px 14px;border:1px solid var(--line);border-radius:14px;background:#0a111d;margin:15px 0 18px}.dot{width:10px;height:10px;border-radius:50%;background:#74849a;box-shadow:0 0 0 5px #74849a20}.dot.online{background:var(--green);box-shadow:0 0 0 5px #7fe0a520}.status strong{font-size:14px}.status small{display:block;color:var(--muted);font-size:12px}.field{display:block;margin:12px 0}.field span{display:block;color:#c8d4e6;font-size:12px;font-weight:650;margin:0 0 6px}.input{width:100%;background:#09111d;border:1px solid #31425a;border-radius:12px;color:var(--text);padding:13px 14px;font:inherit;font-size:15px;outline:none;transition:.2s}.input:focus{border-color:var(--cyan);box-shadow:0 0 0 3px #63e6d319}.btn{border:0;border-radius:12px;padding:12px 16px;color:#07121c;background:linear-gradient(100deg,var(--cyan),#8bc6ff);font-weight:800;font-size:14px;cursor:pointer;width:100%;margin-top:9px}.btn:hover{filter:brightness(1.06);transform:translateY(-1px)}.btn.secondary{background:#19283a;color:#d6e4f6;border:1px solid #34465d}.btn.danger{background:#351b27;color:#ffb8c5;border:1px solid #6b3345}.notice{padding:13px 15px;border:1px solid #326557;background:#10251f;border-radius:13px;color:#b6f3d1;margin:0 0 18px}.notice.error{border-color:#704053;background:#28151f;color:#ffc3d0}.tip{color:var(--muted);font-size:12px;margin:12px 0 0}.steps{padding-left:20px;color:#cbd8e9;font-size:13px}.steps li{margin:8px 0}.pill{display:inline-flex;border:1px solid #34465c;border-radius:999px;padding:3px 9px;color:#bfcee0;font-size:11px;margin:2px}.mono{font-family:ui-monospace,SFMono-Regular,monospace}.secure-note{border-left:3px solid #efc374;padding:10px 12px;background:#251e13;color:#ddcda9;border-radius:0 10px 10px 0;font-size:12px;margin-top:15px}.login{max-width:420px;margin:9vh auto 0}.footer{margin-top:22px;text-align:center;color:#72839a;font-size:11px}.row{display:flex;gap:10px}.row>*{flex:1}@media(max-width:680px){body{padding:17px 13px 35px}.grid{grid-template-columns:1fr}.card.wide{grid-column:auto}.card{padding:18px;border-radius:17px}.brand{margin-bottom:25px}.row{display:block}}
"""


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#090e18"><title>{html.escape(title)} · Song Dashboard</title><style>{CSS}</style></head><body><main class="wrap">{body}</main></body></html>"""


def _csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def _check_csrf(request: Request, submitted: str) -> bool:
    current = request.session.get("csrf", "")
    return bool(current and submitted and hmac.compare_digest(str(current), str(submitted)))


def _authed(request: Request) -> bool:
    return request.session.get("authenticated") is True


def _login_limited(ip: str) -> bool:
    now = time.monotonic()
    bucket = failed_logins[ip]
    while bucket and now - bucket[0] > 600:
        bucket.popleft()
    return len(bucket) >= 6


def _header() -> str:
    return '<header class="brand"><div class="logo">♫</div><div><h1>Song Control</h1><p>Private account dashboard</p></div></header>'


def _notice(request: Request) -> str:
    message = request.query_params.get("notice", "")[:220]
    if not message:
        return ""
    is_error = request.query_params.get("kind") == "error"
    return f'<div class="notice{" error" if is_error else ""}">{html.escape(message)}</div>'


def _redirect_notice(message: str, error: bool = False) -> RedirectResponse:
    kind = "error" if error else "ok"
    return RedirectResponse(f"/?kind={kind}&notice={quote(message[:220])}", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    csrf = _csrf(request)
    if not _authed(request):
        body = _header() + f"""<section class="login"><div class="hero"><span class="eyebrow">Protected workspace</span><h2>Welcome back.</h2><p>Sign in to securely link your Telegram account and manage media downloads.</p></div>{_notice(request)}<section class="card"><h3>Dashboard sign-in</h3><p class="sub">Use the dashboard password configured for this installation.</p><form method="post" action="/login"><input type="hidden" name="csrf" value="{csrf}"><label class="field"><span>Password</span><input class="input" type="password" name="password" autocomplete="current-password" required autofocus></label><button class="btn" type="submit">Unlock dashboard <span aria-hidden="true">→</span></button></form><div class="secure-note">Never share your login password, Telegram code, or two-step verification password. Telegram codes are entered only here.</div></section><div class="footer">Private dashboard · Session expires after 8 hours</div></section>"""
        return HTMLResponse(_layout("Sign in", body))

    state = account.status()
    stage = str(state["stage"])
    connected = bool(state["connected"])
    label = "Connected" if connected else ({"awaiting_code": "Code required", "awaiting_password": "2-step password required", "connecting": "Connecting"}.get(stage, "Not connected"))
    account_name = str(state["account"] or "No Telegram account linked")
    csrf_input = f'<input type="hidden" name="csrf" value="{csrf}">'
    if connected:
        link_panel = f"""<div class="status"><span class="dot online"></span><div><strong>{html.escape(label)}</strong><small>{html.escape(account_name)}</small></div></div><p class="sub">Your linked account monitors incoming <code>/song</code>, <code>/video</code> and <code>/search</code> commands. <code>/start</code> stays silent.</p><form method="post" action="/link/disconnect">{csrf_input}<button class="btn danger" type="submit">Disconnect this dashboard session</button></form>"""
    elif stage == "awaiting_code":
        link_panel = f"""<div class="status"><span class="dot"></span><div><strong>Login code sent</strong><small>Enter the code Telegram sent to {html.escape(account.phone)}</small></div></div><form method="post" action="/link/code">{csrf_input}<label class="field"><span>Telegram login code</span><input class="input mono" name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="12345" maxlength="12" required></label><button class="btn" type="submit">Verify code →</button></form><p class="tip">If Telegram asks for a two-step password, the next screen will request it.</p>"""
    elif stage == "awaiting_password":
        link_panel = f"""<div class="status"><span class="dot"></span><div><strong>Two-step verification</strong><small>Telegram requires the account's cloud password</small></div></div><form method="post" action="/link/password">{csrf_input}<label class="field"><span>Telegram two-step password</span><input class="input" type="password" name="password" autocomplete="current-password" required></label><button class="btn" type="submit">Finish account linking →</button></form><p class="tip">The password is used only to authenticate this login and is not saved.</p>"""
    else:
        link_panel = f"""<div class="status"><span class="dot"></span><div><strong>Not connected</strong><small>Link a Telegram account to enable the user-account listener</small></div></div><form method="post" action="/link/start">{csrf_input}<div class="row"><label class="field"><span>Telegram API ID</span><input class="input" name="api_id" inputmode="numeric" autocomplete="off" placeholder="12345678" required></label><label class="field"><span>API hash</span><input class="input" name="api_hash" type="password" autocomplete="off" placeholder="From my.telegram.org" required></label></div><label class="field"><span>Phone number with country code</span><input class="input" name="phone" type="tel" autocomplete="tel" placeholder="+8801XXXXXXXXX" required></label><button class="btn" type="submit">Send Telegram login code →</button></form><p class="tip">Create your API ID and hash through Telegram's official developer portal. The API hash is not stored by this dashboard.</p>"""
    body = _header() + f"""<section class="hero"><span class="eyebrow">Account link · Download control</span><h2>Your music desk,<br>in one place.</h2><p>Connect your own Telegram account, then let it handle song, video, and search commands in chats where you choose to use them.</p></section>{_notice(request)}<div class="grid"><section class="card"><h3>Telegram connection</h3><p class="sub">Sign in using Telegram's official code verification flow.</p>{link_panel}</section><section class="card"><h3>How it works</h3><p class="sub">Ordinary text is ignored. Others must reply to the linked account with a complete command; the owner can run commands directly.</p><ol class="steps"><li><span class="pill mono">/song &lt;name&gt;</span> or <span class="pill mono">!song &lt;name&gt;</span> for MP3.</li><li><span class="pill mono">/video &lt;name&gt;</span> or <span class="pill mono">!video &lt;name&gt;</span> for HD video.</li><li><span class="pill mono">/search &lt;name&gt;</span> to choose a result and format.</li><li><span class="pill mono">/start</span> receives no reply.</li></ol><div class="secure-note">Telegram keeps the login session on this machine. Keep the dashboard private and use HTTPS or localhost; anyone with this session file can act as your account.</div></section><section class="card wide"><h3>Security & privacy</h3><p class="sub">The one-time code and Telegram two-step password are held in memory only. API hash is used for sign-in and is not written to disk. The authorized session file is stored owner-only in <code>data/</code> so it can reconnect after restart. Disconnect from here to revoke this dashboard session.</p></section></div><form method="post" action="/logout" style="text-align:right;margin-top:16px">{csrf_input}<button class="btn secondary" style="width:auto;padding-inline:19px" type="submit">Lock dashboard</button></form><div class="footer">Song Control · Private by design</div><script>async function refresh(){{try{{const r=await fetch('/api/status',{{cache:'no-store'}});if(!r.ok)return;const s=await r.json();if(s.stage!=='{stage}')location.reload()}}catch(e){{}}}}setInterval(refresh,5000);</script>"""
    return HTMLResponse(_layout("Dashboard", body))


@app.post("/login")
async def login(request: Request, password: str = Form(...), csrf: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    if not _check_csrf(request, csrf):
        return _redirect_notice("Your sign-in page expired. Reload and try again.", True)
    if _login_limited(ip):
        return _redirect_notice("Too many sign-in attempts. Wait 10 minutes before trying again.", True)
    if not hmac.compare_digest(password.encode("utf-8"), settings.dashboard_secret.encode("utf-8")):
        failed_logins[ip].append(time.monotonic())
        return _redirect_notice("Password not accepted.", True)
    failed_logins.pop(ip, None)
    request.session.clear()
    request.session["authenticated"] = True
    request.session["csrf"] = secrets.token_urlsafe(24)
    return RedirectResponse("/", status_code=303)


async def _auth_or_redirect(request: Request):
    if not _authed(request):
        return _redirect_notice("Please sign in to continue.", True)
    return None


@app.post("/logout")
async def logout(request: Request, csrf: str = Form(...)):
    if not _authed(request) or not _check_csrf(request, csrf):
        return _redirect_notice("Session expired. Sign in again.", True)
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.post("/link/start")
async def link_start(request: Request, csrf: str = Form(...), api_id: str = Form(...), api_hash: str = Form(...), phone: str = Form(...)):
    if not _authed(request):
        return _redirect_notice("Please sign in to continue.", True)
    if not _check_csrf(request, csrf):
        return _redirect_notice("Your form expired. Reload the dashboard and try again.", True)
    try:
        api_id_int = int(api_id.strip())
        if not (1 <= api_id_int <= 2_147_483_647):
            raise ValueError
        clean_hash = api_hash.strip()
        if len(clean_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in clean_hash):
            return _redirect_notice("Enter the 32-character API hash from Telegram.", True)
        clean_phone = phone.strip()
        if len(clean_phone) < 7 or len(clean_phone) > 24:
            return _redirect_notice("Enter a valid phone number with country code.", True)
        result = await account.begin_login(api_id_int, clean_hash, clean_phone)
        return _redirect_notice(result)
    except ValueError as exc:
        return _redirect_notice(str(exc) or "Enter a valid API ID.", True)
    except Exception:
        logger.exception("Dashboard account-link request failed")
        return _redirect_notice("Could not start Telegram sign-in. Check the API details and try again.", True)


@app.post("/link/code")
async def link_code(request: Request, csrf: str = Form(...), code: str = Form(...)):
    if not _authed(request):
        return _redirect_notice("Please sign in to continue.", True)
    if not _check_csrf(request, csrf):
        return _redirect_notice("Your form expired. Reload the dashboard and try again.", True)
    clean = "".join(code.split())
    if not clean or len(clean) > 12 or not clean.isdigit():
        return _redirect_notice("Enter the numeric code sent by Telegram.", True)
    try:
        result = await account.submit_code(clean)
        return _redirect_notice(result)
    except ValueError as exc:
        return _redirect_notice(str(exc), True)
    except Exception:
        logger.exception("Dashboard login-code verification failed")
        return _redirect_notice("Telegram could not verify that code. Request a new code and retry.", True)


@app.post("/link/password")
async def link_password(request: Request, csrf: str = Form(...), password: str = Form(...)):
    if not _authed(request):
        return _redirect_notice("Please sign in to continue.", True)
    if not _check_csrf(request, csrf):
        return _redirect_notice("Your form expired. Reload the dashboard and try again.", True)
    if not password or len(password) > 256:
        return _redirect_notice("Enter your Telegram two-step password.", True)
    try:
        result = await account.submit_password(password)
        return _redirect_notice(result)
    except ValueError as exc:
        return _redirect_notice(str(exc), True)
    except Exception:
        logger.exception("Dashboard two-step verification failed")
        return _redirect_notice("Telegram did not accept the two-step password. Check it and retry.", True)


@app.post("/link/disconnect")
async def disconnect(request: Request, csrf: str = Form(...)):
    if not _authed(request):
        return _redirect_notice("Please sign in to continue.", True)
    if not _check_csrf(request, csrf):
        return _redirect_notice("Your form expired. Reload the dashboard and try again.", True)
    result = await account.disconnect()
    return _redirect_notice(result)


@app.get("/api/status")
async def api_status(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "authentication required"}, status_code=401)
    return JSONResponse(account.status())


def main() -> None:
    import uvicorn
    uvicorn.run(
        "dashboard:app", host=settings.dashboard_host, port=settings.dashboard_port,
        reload=False, log_level="info", proxy_headers=True,
    )


if __name__ == "__main__":
    main()
