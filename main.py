import datetime, json, os, random, re, string
from typing import Optional
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

try:
    from anthropic import AsyncAnthropic as _Anthropic
    _ai_key = os.getenv("ANTHROPIC_API_KEY", "")
    ai = _Anthropic(api_key=_ai_key) if _ai_key else None
except ImportError:
    ai = None

from fastapi import (FastAPI, Request, Form, Depends,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

from sqlalchemy import func
import models, database
from database import engine, get_db

# ── Config ────────────────────────────────────────────────────────────────────
SECRET_KEY       = os.getenv("SECRET_KEY", "duo-queue-secret-change-in-prod")
CAMPUS_DOMAIN    = "purdue.edu"
POST_TTL_MINUTES = 30
OTP_TTL_MINUTES  = 15

CAMPUS_BOUNDS = dict(min_lat=40.419, max_lat=40.440,
                     min_lon=-86.940, max_lon=-86.900)
REQUIRE_GEOLOCATION = False

CATEGORY_META = {
    "gym":   {"label": "Gym",   "icon": "🏋️", "color": "#f59e0b"},
    "study": {"label": "Study", "icon": "📚", "color": "#3b82f6"},
    "food":  {"label": "Food",  "icon": "🍕", "color": "#ef4444"},
    "walk":  {"label": "Walk",  "icon": "🚶", "color": "#10b981"},
    "other": {"label": "Other", "icon": "✨", "color": "#8b5cf6"},
}

# ── App ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    models.Base.metadata.create_all(bind=engine)
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

def minutes_left(expires_at: datetime.datetime) -> int:
    return max(0, int((expires_at - datetime.datetime.utcnow()).total_seconds() / 60))

# Pass these in every template context via the helper below
def ctx(request, **kwargs):
    return {"request": request, "CATEGORY_META": CATEGORY_META,
            "minutes_left": minutes_left, **kwargs}

# ── Email ─────────────────────────────────────────────────────────────────────
def send_otp_email(to_email: str, code: str):
    resend_key  = os.getenv("RESEND_API_KEY", "")
    resend_from = os.getenv("RESEND_FROM", "Duo Queue <onboarding@resend.dev>")

    html = f"""
    <div style="font-family:Inter,sans-serif;max-width:420px;margin:0 auto;
                background:#111;color:#f0f0f0;border-radius:12px;padding:2rem;
                border:1px solid #2e2e2e;">
      <div style="text-align:center;margin-bottom:1.5rem;">
        <span style="font-size:1.8rem;">⚡</span>
        <h2 style="margin:.4rem 0;color:#fff;font-size:1.3rem;letter-spacing:-.02em;">
          Duo Queue
        </h2>
      </div>
      <p style="color:#777;margin-bottom:1rem;font-size:.9rem;">
        Your verification code:
      </p>
      <div style="background:#1a1a1a;border:1px solid #2e2e2e;border-radius:8px;
                  padding:1.5rem;text-align:center;letter-spacing:.35em;
                  font-size:2rem;font-weight:700;color:#fff;">
        {code}
      </div>
      <p style="color:#555;font-size:.8rem;margin-top:1rem;text-align:center;">
        Expires in {OTP_TTL_MINUTES} minutes. Do not share this code.
      </p>
    </div>"""

    if not resend_key:
        print(f"\n{'='*40}\nDEV MODE — OTP for {to_email}: {code}\n{'='*40}\n")
        raise RuntimeError("DEV_MODE")

    import resend as _resend
    _resend.api_key = resend_key
    dev_redirect = os.getenv("RESEND_DEV_REDIRECT", "")
    send_to = dev_redirect if dev_redirect else to_email
    _resend.Emails.send({
        "from":    resend_from,
        "to":      [send_to],
        "subject": f"{code} is your Duo Queue code" + (f" (for {to_email})" if dev_redirect else ""),
        "html":    html,
    })

# ── WebSocket manager ─────────────────────────────────────────────────────────
class WSManager:
    def __init__(self):
        self.feed_clients: list[WebSocket] = []
        self.chat_rooms: dict[int, list[WebSocket]] = {}

    async def connect_feed(self, ws: WebSocket):
        await ws.accept(); self.feed_clients.append(ws)

    def disconnect_feed(self, ws: WebSocket):
        self.feed_clients = [c for c in self.feed_clients if c is not ws]

    async def broadcast_post(self, data: dict):
        dead = []
        for ws in self.feed_clients:
            try: await ws.send_json(data)
            except: dead.append(ws)
        for ws in dead: self.disconnect_feed(ws)

    async def connect_chat(self, room: int, ws: WebSocket):
        await ws.accept()
        self.chat_rooms.setdefault(room, []).append(ws)

    def disconnect_chat(self, room: int, ws: WebSocket):
        self.chat_rooms[room] = [c for c in self.chat_rooms.get(room, []) if c is not ws]

    async def broadcast_msg(self, room: int, msg: dict):
        dead = []
        for ws in self.chat_rooms.get(room, []):
            try: await ws.send_json(msg)
            except: dead.append(ws)
        for ws in dead: self.disconnect_chat(room, ws)

ws_mgr = WSManager()

# ── Auth helpers ──────────────────────────────────────────────────────────────
def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[models.User]:
    uid = request.session.get("user_id")
    if not uid: return None
    return db.query(models.User).filter(models.User.id == uid).first()

def generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))

def expire_old_posts(db: Session):
    db.query(models.Post).filter(
        models.Post.is_active == True,
        models.Post.expires_at <= datetime.datetime.utcnow()
    ).update({"is_active": False})
    db.commit()

# ── Step 1: Enter Purdue email ────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", ctx(request))

@app.post("/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db)
):
    email = email.strip().lower()

    if not email.endswith(f"@{CAMPUS_DOMAIN}"):
        return templates.TemplateResponse("login.html",
            ctx(request, error=f"Must use a @{CAMPUS_DOMAIN} email address."),
            status_code=400)

    # Rate-limit: one OTP per 60 seconds
    cooldown_cutoff = datetime.datetime.utcnow() - datetime.timedelta(seconds=60)
    recent = db.query(models.OTPCode).filter(
        models.OTPCode.email == email,
        models.OTPCode.used == False,
        models.OTPCode.expires_at > datetime.datetime.utcnow(),
        models.OTPCode.created_at > cooldown_cutoff
    ).first()
    if recent:
        return templates.TemplateResponse("login.html",
            ctx(request, error="A code was just sent. Please wait 60 seconds before requesting another."),
            status_code=429)

    # Invalidate old unused codes for this email
    db.query(models.OTPCode).filter(
        models.OTPCode.email == email,
        models.OTPCode.used == False
    ).update({"used": True})
    db.commit()

    code = generate_otp()
    otp  = models.OTPCode(
        email=email, code=code,
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=OTP_TTL_MINUTES)
    )
    db.add(otp); db.commit()

    dev_code = None
    try:
        send_otp_email(email, code)
    except RuntimeError as e:
        if str(e) == "DEV_MODE":
            dev_code = code   # show on page when no SMTP configured
        else:
            raise
    except Exception as e:
        print(f"Email error: {e}")
        return templates.TemplateResponse("login.html",
            ctx(request, error="Failed to send email. Check SMTP settings in .env"),
            status_code=500)

    request.session["pending_email"] = email
    if dev_code:
        return RedirectResponse(f"/verify?dev={dev_code}", status_code=303)
    return RedirectResponse("/verify", status_code=303)

# ── Resend OTP ────────────────────────────────────────────────────────────────
@app.post("/resend-otp")
async def resend_otp(request: Request, db: Session = Depends(get_db)):
    email = request.session.get("pending_email")
    if not email:
        return RedirectResponse("/login", status_code=303)

    # Invalidate old codes
    db.query(models.OTPCode).filter(
        models.OTPCode.email == email,
        models.OTPCode.used == False
    ).update({"used": True})
    db.commit()

    code = generate_otp()
    otp  = models.OTPCode(
        email=email, code=code,
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=OTP_TTL_MINUTES)
    )
    db.add(otp); db.commit()

    dev_code = None
    try:
        send_otp_email(email, code)
    except RuntimeError as e:
        if str(e) == "DEV_MODE":
            dev_code = code
        else:
            raise

    if dev_code:
        return RedirectResponse(f"/verify?dev={dev_code}", status_code=303)
    return RedirectResponse("/verify?resent=1", status_code=303)

# ── Step 2: Enter OTP ─────────────────────────────────────────────────────────
@app.get("/verify", response_class=HTMLResponse)
async def verify_get(request: Request, dev: str = ""):
    email = request.session.get("pending_email")
    if not email:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("verify.html",
        ctx(request, email=email, dev_code=dev if dev else None))

@app.post("/verify")
async def verify_post(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db)
):
    email = request.session.get("pending_email")
    if not email:
        return RedirectResponse("/login", status_code=303)

    otp = db.query(models.OTPCode).filter(
        models.OTPCode.email == email,
        models.OTPCode.code  == code.strip(),
        models.OTPCode.used  == False,
        models.OTPCode.expires_at > datetime.datetime.utcnow()
    ).first()

    if not otp:
        return templates.TemplateResponse("verify.html",
            ctx(request, email=email, error="Invalid or expired code. Try again."),
            status_code=400)

    otp.used = True
    db.commit()

    user = db.query(models.User).filter(models.User.email == email).first()
    if user:
        # Returning user — log straight in
        request.session.pop("pending_email", None)
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)
    else:
        # New user — need a username
        request.session["verified_email"] = email
        request.session.pop("pending_email", None)
        return RedirectResponse("/setup", status_code=303)

# ── Step 3: Pick username (new users only) ────────────────────────────────────
@app.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request):
    if not request.session.get("verified_email"):
        return RedirectResponse("/login", status_code=303)
    email = request.session["verified_email"]
    # Pre-fill username from email prefix
    suggested = email.split("@")[0]
    return templates.TemplateResponse("setup.html",
        ctx(request, email=email, suggested=suggested))

@app.post("/setup")
async def setup_post(
    request: Request,
    username: str = Form(...),
    db: Session = Depends(get_db)
):
    email = request.session.get("verified_email")
    if not email:
        return RedirectResponse("/login", status_code=303)

    username = username.strip()
    if len(username) < 3 or len(username) > 30:
        return templates.TemplateResponse("setup.html",
            ctx(request, email=email, suggested=username,
                error="Username must be 3–30 characters."), status_code=400)

    if not re.fullmatch(r"[a-zA-Z0-9_]+", username):
        return templates.TemplateResponse("setup.html",
            ctx(request, email=email, suggested=username,
                error="Username can only contain letters, numbers, and underscores."), status_code=400)

    if db.query(models.User).filter(models.User.username == username).first():
        return templates.TemplateResponse("setup.html",
            ctx(request, email=email, suggested=username,
                error="That username is taken. Try another."), status_code=400)

    user = models.User(email=email, username=username)
    db.add(user); db.commit(); db.refresh(user)
    request.session.pop("verified_email", None)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

# ── Feed ──────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def feed(request: Request, category: str = "", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    expire_old_posts(db)

    q = db.query(models.Post).filter(
        models.Post.is_active == True,
        models.Post.expires_at > datetime.datetime.utcnow()
    )
    if category and category in CATEGORY_META:
        q = q.filter(models.Post.category == category)
    posts = q.order_by(models.Post.created_at.desc()).all()

    posts_data = []
    for p in posts:
        conn = db.query(models.Connection).filter(
            models.Connection.post_id == p.id,
            models.Connection.joiner_id == user.id
        ).first()
        my_connections = []
        if p.user_id == user.id:
            my_connections = db.query(models.Connection).filter(
                models.Connection.post_id == p.id
            ).all()
        posts_data.append({
            "post": p,
            "join_count": len(p.connections),
            "already_joined": conn is not None,
            "is_mine": p.user_id == user.id,
            "connection_id": conn.id if conn else None,
            "my_connections": my_connections,
        })

    counts_raw = db.query(models.Post.category, func.count(models.Post.id)).filter(
        models.Post.is_active == True,
        models.Post.expires_at > datetime.datetime.utcnow()
    ).group_by(models.Post.category).all()
    category_counts = {cat: cnt for cat, cnt in counts_raw}

    has_active_post = db.query(models.Post).filter(
        models.Post.user_id == user.id,
        models.Post.is_active == True,
        models.Post.expires_at > datetime.datetime.utcnow()
    ).first() is not None

    return templates.TemplateResponse("index.html",
        ctx(request, user=user, posts_data=posts_data,
            active_category=category, categories=CATEGORY_META,
            category_counts=category_counts, has_active_post=has_active_post,
            require_geo=REQUIRE_GEOLOCATION, campus_bounds=CAMPUS_BOUNDS))

# ── Create Post ───────────────────────────────────────────────────────────────
@app.post("/post")
async def create_post(
    request: Request,
    category: str = Form(...),
    content: str = Form(...),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if category not in CATEGORY_META:
        return RedirectResponse("/", status_code=303)

    content = content.strip()
    if not content or len(content) > 120:
        return RedirectResponse("/", status_code=303)

    # One active post per user
    existing_post = db.query(models.Post).filter(
        models.Post.user_id == user.id,
        models.Post.is_active == True,
        models.Post.expires_at > datetime.datetime.utcnow()
    ).first()
    if existing_post:
        return RedirectResponse("/", status_code=303)

    if REQUIRE_GEOLOCATION:
        if latitude is None or longitude is None:
            return RedirectResponse("/", status_code=303)
        b = CAMPUS_BOUNDS
        if not (b["min_lat"] <= latitude <= b["max_lat"] and
                b["min_lon"] <= longitude <= b["max_lon"]):
            return RedirectResponse("/", status_code=303)

    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=POST_TTL_MINUTES)
    post = models.Post(
        user_id=user.id, category=category, content=content,
        latitude=latitude, longitude=longitude, expires_at=expires_at
    )
    db.add(post); db.commit(); db.refresh(post)

    meta = CATEGORY_META.get(category, CATEGORY_META["other"])
    await ws_mgr.broadcast_post({
        "type": "new_post", "id": post.id,
        "user_id": user.id,
        "category": category, "icon": meta["icon"],
        "color": meta["color"], "label": meta["label"],
        "content": content, "username": user.username,
        "minutes_left": POST_TTL_MINUTES,
    })
    return RedirectResponse("/", status_code=303)

# ── Delete Post ───────────────────────────────────────────────────────────────
@app.post("/post/{post_id}/delete")
async def delete_post(post_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post and user and post.user_id == user.id:
        post.is_active = False; db.commit()
    return RedirectResponse("/", status_code=303)

# ── Join Post ─────────────────────────────────────────────────────────────────
@app.post("/post/{post_id}/join")
async def join_post(post_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    post = db.query(models.Post).filter(
        models.Post.id == post_id, models.Post.is_active == True
    ).first()
    if not post or post.user_id == user.id:
        return RedirectResponse("/", status_code=303)

    existing = db.query(models.Connection).filter(
        models.Connection.post_id == post_id,
        models.Connection.joiner_id == user.id
    ).first()
    if existing:
        return RedirectResponse(f"/chat/{existing.id}", status_code=303)

    conn = models.Connection(post_id=post_id, joiner_id=user.id)
    db.add(conn); db.commit(); db.refresh(conn)

    join_count = db.query(models.Connection).filter(
        models.Connection.post_id == post_id
    ).count()
    await ws_mgr.broadcast_post({
        "type": "join_update",
        "post_id": post_id,
        "join_count": join_count,
        "author_id": post.user_id,
        "joiner_username": user.username,
        "connection_id": conn.id,
    })

    return RedirectResponse(f"/chat/{conn.id}", status_code=303)

# ── Chat ──────────────────────────────────────────────────────────────────────
@app.get("/chat/{conn_id}", response_class=HTMLResponse)
async def chat_page(conn_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db.query(models.Connection).filter(models.Connection.id == conn_id).first()
    if not conn:
        return RedirectResponse("/", status_code=303)

    if user.id not in (conn.post.user_id, conn.joiner_id):
        return RedirectResponse("/", status_code=303)

    expired = (not conn.post.is_active or
               conn.post.expires_at <= datetime.datetime.utcnow())
    other = conn.joiner if user.id == conn.post.user_id else conn.post.author

    return templates.TemplateResponse("chat.html",
        ctx(request, user=user, conn=conn, messages=conn.messages,
            other_user=other, expired=expired, categories=CATEGORY_META,
            minutes_left=minutes_left(conn.post.expires_at) if not expired else 0))

# ── WebSocket: Feed ───────────────────────────────────────────────────────────
@app.websocket("/ws/feed")
async def ws_feed(websocket: WebSocket):
    await ws_mgr.connect_feed(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        ws_mgr.disconnect_feed(websocket)

# ── WebSocket: Chat ───────────────────────────────────────────────────────────
@app.websocket("/ws/chat/{conn_id}")
async def ws_chat(websocket: WebSocket, conn_id: int):
    db = database.SessionLocal()
    try:
        user_id = websocket.session.get("user_id")
        if not user_id:
            await websocket.close(code=1008)
            return
        user = db.query(models.User).filter(models.User.id == user_id).first()
        conn_obj = db.query(models.Connection).filter(models.Connection.id == conn_id).first()
        if not user or not conn_obj or user_id not in (conn_obj.post.user_id, conn_obj.joiner_id):
            await websocket.close(code=1008)
            return

        await ws_mgr.connect_chat(conn_id, websocket)
        try:
            while True:
                data    = await websocket.receive_json()
                content = data.get("content", "").strip()
                if not content: continue
                msg = models.Message(connection_id=conn_id, user_id=user_id, content=content)
                db.add(msg); db.commit(); db.refresh(msg)
                await ws_mgr.broadcast_msg(conn_id, {
                    "type": "message", "user_id": user_id,
                    "username": user.username, "content": content,
                    "time": msg.created_at.strftime("%H:%M"),
                })
        except WebSocketDisconnect:
            ws_mgr.disconnect_chat(conn_id, websocket)
    finally:
        db.close()

# ── AI: streaming post completion ─────────────────────────────────────────────
@app.post("/api/ai/complete")
async def api_ai_complete(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or not ai:
        return JSONResponse({"completion": ""})

    body     = await request.json()
    partial  = body.get("partial", "").strip()
    category = body.get("category", "other")
    if len(partial) < 8:
        return JSONResponse({"completion": ""})

    cat_label = CATEGORY_META.get(category, CATEGORY_META["other"])["label"]
    hour      = datetime.datetime.now().hour
    time_ctx  = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening" if hour < 21 else "night"

    async def stream():
        try:
            async with ai.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=55,
                system="Complete campus activity post text. Output ONLY the completion — the text that follows what was typed. No quotes, no emojis, no explanations. Keep it casual and brief.",
                messages=[{"role": "user", "content":
                    f"Category: {cat_label}. Time: {time_ctx}. Post so far: \"{partial}\"\n"
                    f"Complete it naturally in at most 60 more characters."}]
            ) as s:
                async for chunk in s.text_stream:
                    yield f"data: {json.dumps({'t': chunk})}\n\n"
        except Exception as e:
            print(f"AI complete error: {e}")
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── AI: suggest a full post ────────────────────────────────────────────────────
@app.post("/api/ai/suggest")
async def api_ai_suggest(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or not ai:
        return JSONResponse({"category": "other", "content": ""})

    now  = datetime.datetime.now()
    hour = now.hour
    day  = now.strftime("%A")
    time_ctx = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening" if hour < 21 else "late night"

    live_cats = db.query(models.Post.category).filter(
        models.Post.is_active == True,
        models.Post.expires_at > datetime.datetime.utcnow()
    ).limit(8).all()
    trending = ", ".join(r[0] for r in live_cats) or "none"

    try:
        msg = await ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system="Suggest a spontaneous campus activity for a Purdue student. Reply with JSON only.",
            messages=[{"role": "user", "content":
                f"{day} {time_ctx}. Trending on campus: {trending}.\n"
                f'Reply: {{"category":"gym|study|food|walk|other","content":"post under 90 chars, casual, no emojis"}}'}]
        )
        raw  = msg.content[0].text.strip()
        data = json.loads(raw[raw.index("{"):raw.rindex("}")+1])
        cat  = data.get("category", "other")
        if cat not in CATEGORY_META:
            cat = "other"
        return JSONResponse({"category": cat, "content": str(data.get("content", ""))[:120]})
    except Exception as e:
        print(f"AI suggest error: {e}")
        return JSONResponse({"category": "other", "content": ""})

# ── AI: chat icebreaker ────────────────────────────────────────────────────────
@app.post("/api/ai/icebreaker")
async def api_ai_icebreaker(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or not ai:
        return JSONResponse({"icebreaker": ""})

    body           = await request.json()
    post_content   = body.get("post_content", "")[:120]
    category       = body.get("category", "other")
    other_username = body.get("other_username", "them")
    cat_label      = CATEGORY_META.get(category, CATEGORY_META["other"])["label"]

    try:
        msg = await ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=70,
            system="Write a single natural first message from one college student to another. No cringe. Sound human. Under 100 chars. No emojis.",
            messages=[{"role": "user", "content":
                f"Activity: {cat_label} — \"{post_content}\"\n"
                f"Send a first message to @{other_username}. Be casual and specific to the activity."}]
        )
        return JSONResponse({"icebreaker": msg.content[0].text.strip().strip('"')})
    except Exception as e:
        print(f"AI icebreaker error: {e}")
        return JSONResponse({"icebreaker": ""})

# ── AI: match scores for feed posts ───────────────────────────────────────────
@app.post("/api/ai/scores")
async def api_ai_scores(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or not ai:
        return JSONResponse({"scores": {}})

    body  = await request.json()
    posts = body.get("posts", [])[:12]
    if not posts:
        return JSONResponse({"scores": {}})

    # User's join history (last 30)
    history = db.query(models.Post.category).join(
        models.Connection, models.Connection.post_id == models.Post.id
    ).filter(models.Connection.joiner_id == user.id).limit(30).all()
    user_history = [h[0] for h in history] or ["none"]

    hour     = datetime.datetime.now().hour
    time_ctx = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening" if hour < 21 else "late night"

    posts_text = "\n".join(
        f'id:{p["id"]} [{p["category"]}] "{p["content"][:60]}"'
        for p in posts
    )

    try:
        msg = await ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            system="Score campus activity posts for a specific user. Reply only with a JSON array.",
            messages=[{"role": "user", "content":
                f"Time: {time_ctx}. User's past joins: {user_history}.\n"
                f"Posts:\n{posts_text}\n\n"
                f'Reply: [{{"id":<id>,"score":<1-100>,"why":"<4 words max>"}}] — one entry per post.'}]
        )
        raw    = msg.content[0].text.strip()
        parsed = json.loads(raw[raw.index("["):raw.rindex("]")+1])
        scores = {str(s["id"]): {"score": int(s["score"]), "why": str(s.get("why", ""))} for s in parsed}
        return JSONResponse({"scores": scores})
    except Exception as e:
        print(f"AI scores error: {e}")
        return JSONResponse({"scores": {}})
