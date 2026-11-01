import os, json, hmac, hashlib, time, base64, datetime as dt
from contextlib import contextmanager
from urllib.parse import parse_qsl
import httpx, psycopg2, psycopg2.extras
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

BOT = os.environ["BOT_TOKEN"]
DB = os.environ["DATABASE_URL"]
GKEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ADMINS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
BASE = os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
WH = hashlib.sha256(BOT.encode()).hexdigest()[:32]
FREE = {"chat": 5, "tutor": 1}
COL = {"chat": ("cu", "rbc"), "tutor": ("tu", "rbt")}
SYS = {
    "chat": "Sen Student AI yordamchisisan. Talabalarga o'zbek tilida (foydalanuvchi boshqa tilda yozsa, shu tilda) aniq va qisqa yordam ber.",
    "tutor": "Sen shaxsiy ingliz tili ustozisan. Foydalanuvchi darajasiga mos, sodda tushuntir, misollar ber, xatolarini yumshoq tuzat.",
}
TG = f"https://api.telegram.org/bot{BOT}"
app = FastAPI()

SCHEMA = """
create table if not exists users(id bigint primary key, name text, balance bigint default 0,
 xp bigint default 0, cp int default 0, rbc int default 0, rbt int default 0, qd date,
 cu int default 0, tu int default 0, sub_until timestamptz, created timestamptz default now());
create table if not exists receipts(id serial primary key, user_id bigint, amount bigint,
 sender_name text, sender_card text, card_type text, status int default 0, created timestamptz default now());
"""

@contextmanager
def cur():
    c = psycopg2.connect(DB, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with c, c.cursor() as k:
            yield k
    finally:
        c.close()

def tg(method, **kw):
    return httpx.post(f"{TG}/{method}", json=kw, timeout=20)

@app.on_event("startup")
def start():
    with cur() as k:
        k.execute(SCHEMA)
    if BASE:
        tg("setWebhook", url=f"{BASE}/webhook/{WH}")
        tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Student AI", "web_app": {"url": BASE}})

def auth(init: str):
    d = dict(parse_qsl(init, keep_blank_values=True))
    h = d.pop("hash", "")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    sec = hmac.new(b"WebAppData", BOT.encode(), hashlib.sha256).digest()
    ok = hmac.compare_digest(hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest(), h)
    if not ok or time.time() - int(d.get("auth_date", 0)) > 86400:
        raise HTTPException(401, "auth")
    u = json.loads(d["user"])
    with cur() as k:
        k.execute("insert into users(id,name) values(%s,%s) on conflict(id) do update set name=excluded.name returning *",
                  (u["id"], u.get("first_name", "")))
        return k.fetchone()

def spend(uid, kind):
    col, bon = COL[kind]
    with cur() as k:
        k.execute("select * from users where id=%s for update", (uid,))
        u = k.fetchone()
        today = dt.date.today()
        if u["qd"] != today:
            k.execute("update users set qd=%s,cu=0,tu=0 where id=%s", (today, uid))
            u[col] = 0
        if u[col] < FREE[kind]:
            k.execute(f"update users set {col}={col}+1 where id=%s", (uid,)); return "free"
        if u[bon] > 0:
            k.execute(f"update users set {bon}={bon}-1 where id=%s", (uid,)); return "bonus"
        if u["cp"] > 0:
            k.execute("update users set cp=cp-1 where id=%s", (uid,)); return "paid"
    return None

def refund(uid, kind, src):
    col, bon = COL[kind]
    q = {"free": f"update users set {col}={col}-1 where id=%s",
         "bonus": f"update users set {bon}={bon}+1 where id=%s",
         "paid": "update users set cp=cp+1 where id=%s"}[src]
    with cur() as k:
        k.execute(q, (uid,))

@app.get("/api/me")
def me(x_init: str = Header("")):
    return auth(x_init)

@app.post("/api/chat")
def chat(body: dict, x_init: str = Header("")):
    u = auth(x_init)
    kind = "tutor" if body.get("kind") == "tutor" else "chat"
    msgs = [{"role": m["role"], "content": str(m["content"])[:4000]}
            for m in body.get("messages", [])[-12:] if m.get("role") in ("user", "assistant")]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "bad")
    src = spend(u["id"], kind)
    if not src:
        raise HTTPException(402, "limit")
    try:
        contents = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in msgs]
        r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                       headers={"x-goog-api-key": GKEY},
                       json={"systemInstruction": {"parts": [{"text": SYS[kind]}]}, "contents": contents,
                             "generationConfig": {"maxOutputTokens": 1000}}, timeout=60)
        r.raise_for_status()
        text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()
        if not text:
            raise ValueError("empty")
        return {"text": text, "src": src}
    except Exception:
        refund(u["id"], kind, src)
        raise HTTPException(502, "ai")

@app.post("/api/receipt")
def receipt(body: dict, x_init: str = Header("")):
    u = auth(x_init)
    try:
        amount = int(body["amount"])
        photo = base64.b64decode(str(body["photo"]).split(",")[-1])
    except Exception:
        raise HTTPException(400, "bad")
    name = str(body.get("name", "")).strip()
    card = "".join(ch for ch in str(body.get("card", "")) if ch.isdigit())
    ctype = "Humo" if body.get("type") == "Humo" else "Uzcard"
    if not 5000 <= amount <= 5_000_000 or len(name) < 5 or len(card) != 4 or not 0 < len(photo) <= 4_000_000:
        raise HTTPException(400, "invalid")
    with cur() as k:
        k.execute("insert into receipts(user_id,amount,sender_name,sender_card,card_type) values(%s,%s,%s,%s,%s) returning id",
                  (u["id"], amount, name, card, ctype))
        rid = k.fetchone()["id"]
    cap = f"Chek #{rid}\nFoydalanuvchi: {u['name']} ({u['id']})\nSumma: {amount:,}\nKarta: {ctype}\nJo'natuvchi: {name} · ****{card}"
    kb = {"inline_keyboard": [[{"text": "✅ Tasdiqlash", "callback_data": f"ok:{rid}"},
                               {"text": "❌ Rad etish", "callback_data": f"no:{rid}"}]]}
    for a in ADMINS:
        httpx.post(f"{TG}/sendPhoto", data={"chat_id": a, "caption": cap, "reply_markup": json.dumps(kb)},
                   files={"photo": ("chek.jpg", photo)}, timeout=30)
    return {"id": rid}

@app.post("/webhook/{secret}")
async def webhook(secret: str, req: Request):
    if secret != WH:
        raise HTTPException(404)
    up = await req.json()
    cb = up.get("callback_query")
    if cb and cb["from"]["id"] in ADMINS:
        act, rid = cb["data"].split(":")
        done = None
        with cur() as k:
            k.execute("select * from receipts where id=%s for update", (int(rid),))
            r = k.fetchone()
            if r and r["status"] == 0:
                if act == "ok":
                    a = r["amount"]
                    b = a // 10 if a >= 50000 else a // 20 if a >= 25000 else 0
                    k.execute("update receipts set status=1 where id=%s", (r["id"],))
                    k.execute("update users set balance=balance+%s where id=%s", (a + b, r["user_id"]))
                else:
                    k.execute("update receipts set status=2 where id=%s", (r["id"],))
                done = (act == "ok", r["user_id"], r["amount"])
        if done:
            ok, uid, a = done
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="✅ Tasdiqlandi" if ok else "❌ Rad etildi")
            tg("editMessageReplyMarkup", chat_id=cb["message"]["chat"]["id"],
               message_id=cb["message"]["message_id"], reply_markup={"inline_keyboard": []})
            tg("sendMessage", chat_id=uid, text=f"✅ {a:,} so'm balansingizga qo'shildi." if ok else "❌ Chekingiz rad etildi. Ma'lumotlarni tekshirib, qayta yuboring.")
        else:
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Bu chek allaqachon ko'rib chiqilgan")
    m = up.get("message")
    if m and str(m.get("text", "")).startswith("/start"):
        tg("sendMessage", chat_id=m["chat"]["id"], text="Student AI ga xush kelibsiz!",
           reply_markup={"inline_keyboard": [[{"text": "Ochish", "web_app": {"url": BASE}}]]})
    return {"ok": True}

@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))
