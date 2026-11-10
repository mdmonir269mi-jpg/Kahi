import asyncio, json, logging, os, random, re, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import Counter
import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv("env.txt"); load_dotenv(".env")  # env.txt আগে, না থাকলে .env
BOT_TOKEN = os.environ["BOT_TOKEN"]
HEADERS = {"mapikey": os.environ["ZENEX_KEY"], "Content-Type": "application/json"}
GETNUM_URL = os.environ["GETNUM_URL"]
OTP_URL = os.environ["OTP_URL"]
ROUTES_URL = os.environ.get("ROUTES_URL", "")   # active ranges (key লাগে)
FEED_URL = os.environ.get("FEED_URL", "")       # Global Live Console (public)

# বট সার্ভিস -> (বাটনের নাম, API-র service নামে যে শব্দ থাকবে, tag (না লাগলে None))
SERVICES = {
    "ig": ("📸 INSTAGRAM", ["instagram", "insta"], None),
    "wa": ("💬 WHATSAPP", ["whatsapp"], None),
    "fbn": ("🆕 FB NEW", ["facebook", "fb"], "new fb"),
    "fbc": ("💻 FB PC CLONE", ["facebook", "fb"], "pc clone"),
    "tg": ("✈️ TELEGRAM", ["telegram"], None),
    "lb": ("🎰 LINEBET", ["linebet"], None),   # না লাগলে এই লাইন মুছে দিন
}
SUPPORT = "@Monir12255"
TOP_N = 3                                   # কয়টা দেশ দেখাবে
BLOCKED_PREFIXES = ["44"]                   # United Kingdom বাদ
BLOCKED_COUNTRIES = ["united kingdom", "uk", "gb"]
POOL_KEEP = 6 * 3600
FAIL_LIMIT, FAIL_PAUSE = 3, 1800
POOL_FILE = "pool.json"
ROUTES_EVERY, FEED_EVERY = 180, 30
TRAFFIC_WINDOW = 30 * 60
TRY_RANGES = 4
POLL_EVERY, POLL_MAX = 6, 300

# প্যানেলের API না থাকলে এগুলো দিয়ে চলবে
MANUAL_RANGES = {
    "fbn": ["508555XXX", "992778XXX", "224657XXX", "374447XXX", "266616XXX", "251962XXX"],
    "fbc": ["229014XXX", "237626XXX", "992778XXX", "224657XXX", "374447XXX", "266616XXX", "251962XXX"],
    "lb": ["228709XXX", "228707XXX", "228705XXX", "228708XXX"],
    "ig": ["266612XXX"],
    "tg": ["232799XXX"],
    "wa": ["628123XXX"],
}
MANUAL_COUNTRY = {
    "229014XXX": "Benin", "508555XXX": "Saint Pierre & Miquelon", "237626XXX": "Cameroon",
    "266612XXX": "Lesotho", "232799XXX": "Sierra Leone", "224654XXX": "Guinea", "628123XXX": "Indonesia",
    "992778XXX": "Tajikistan", "224657XXX": "Guinea", "374447XXX": "Armenia", "266616XXX": "Lesotho",
    "251962XXX": "Ethiopia", "228709XXX": "Togo", "228707XXX": "Togo", "228705XXX": "Togo", "228708XXX": "Togo",
}
ISO = {  # দেশের নাম -> ISO কোড (ফ্ল্যাগের জন্য)
    "benin": "BJ", "cameroon": "CM", "lesotho": "LS", "guinea": "GN", "guinea republic": "GN", "sierra leone": "SL",
    "mali": "ML", "indonesia": "ID", "saint pierre & miquelon": "PM", "st pierre & miquelon": "PM",
    "tajikistan": "TJ", "armenia": "AM", "ghana": "GH", "nigeria": "NG", "senegal": "SN", "ivory coast": "CI", "cote d'ivoire": "CI", "togo": "TG",
    "burkina faso": "BF", "niger": "NE", "chad": "TD", "gabon": "GA", "congo": "CG", "dr congo": "CD",
    "madagascar": "MG", "mozambique": "MZ", "zambia": "ZM", "zimbabwe": "ZW", "kenya": "KE", "uganda": "UG",
    "tanzania": "TZ", "rwanda": "RW", "burundi": "BI", "liberia": "LR", "gambia": "GM", "mauritania": "MR",
    "ethiopia": "ET", "somalia": "SO", "sudan": "SD", "malawi": "MW", "angola": "AO", "botswana": "BW",
    "namibia": "NA", "eswatini": "SZ", "philippines": "PH", "vietnam": "VN", "thailand": "TH", "india": "IN",
    "pakistan": "PK", "bangladesh": "BD", "brazil": "BR", "mexico": "MX", "colombia": "CO", "peru": "PE",
}

logging.basicConfig(level=logging.INFO)
http = httpx.AsyncClient(timeout=20)
tasks: dict[int, asyncio.Task] = {}
routes: list[dict] = []
feed: list[dict] = []
pool: dict[str, dict] = {}
banned: set[str] = set()

# ---------- helpers ----------
def _match(text, keywords):
    t = str(text).lower()
    return any(k in t for k in keywords)

def to_range(number):
    d = re.sub(r"[^0-9]", "", str(number).split("X")[0].split("x")[0])
    return d[:6] + "XXX" if len(d) >= 6 else None

def iso_of(name):
    n = str(name).strip().lower()
    return ISO.get(n) or (n[:2].upper() if n else "??")

def flag(code):
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code) if len(code) == 2 and code.isalpha() else "🌍"

def is_blocked(rng, country=""):
    if rng in banned or any(rng.startswith(p) for p in BLOCKED_PREFIXES):
        return True
    return str(country).strip().lower() in BLOCKED_COUNTRIES

def load_pool():
    global pool, banned
    try:
        d = json.load(open(POOL_FILE)); pool = d.get("pool", {}); banned = set(d.get("banned", []))
    except Exception:
        pool, banned = {}, set()

def save_pool():
    try:
        json.dump({"pool": pool, "banned": sorted(banned)}, open(POOL_FILE, "w"))
    except Exception as e:
        logging.warning("save pool: %s", e)

def update_pool():
    """নতুন range পুলে যোগ হয়, পুরনোগুলো ৬ ঘণ্টা থেকে যায়।"""
    now = time.time()
    for key, (_, kw, tag) in SERVICES.items():
        p = pool.setdefault(key, {})
        for r in routes:
            rng = r.get("range")
            if not rng or not _match(r.get("service", ""), kw): continue
            if tag and tag.lower() not in str(r.get("tag", "")).lower(): continue
            if is_blocked(rng, r.get("country", "")): continue
            e = p.setdefault(rng, {"fails": 0, "until": 0})
            e["seen"] = now
            if r.get("country"): e["country"] = r["country"]
        for rng in [x for x, e in p.items() if now - e.get("seen", 0) > POOL_KEEP or is_blocked(x, e.get("country", ""))]:
            del p[rng]
    save_pool()

def recent_feed(kw):
    now = time.time() * 1000
    return [f for f in feed if _match(f.get("service", ""), kw) and now - float(f.get("time", 0) or 0) <= TRAFFIC_WINDOW * 1000]

def candidates(key):
    """range -> দেশের নাম (পুল + ম্যানুয়াল + লাইভ ফিড)"""
    _, kw, tag = SERVICES[key]
    feed_country = {}
    for f in feed:
        r = to_range(f.get("number", ""))
        if r and f.get("country"): feed_country[r] = f["country"]
    out = {}
    for r, e in pool.get(key, {}).items():
        out[r] = e.get("country") or MANUAL_COUNTRY.get(r) or feed_country.get(r, "")
    for r in MANUAL_RANGES.get(key, []):
        out.setdefault(r, MANUAL_COUNTRY.get(r) or feed_country.get(r, ""))
    if tag is None:
        for f in recent_feed(kw):
            r = to_range(f.get("number", ""))
            if r: out.setdefault(r, f.get("country", ""))
    return {r: c for r, c in out.items() if not is_blocked(r, c)}

def country_groups(key):
    """দেশ অনুযায়ী ভাগ, hits বেশি আগে"""
    _, kw, _ = SERVICES[key]
    hits = Counter(to_range(f.get("number", "")) for f in recent_feed(kw))
    groups = {}
    for r, c in candidates(key).items():
        code = iso_of(c) if c else "??"
        g = groups.setdefault(code, {"name": c or "Unknown", "ranges": [], "hits": 0})
        g["ranges"].append(r); g["hits"] += hits.get(r, 0)
    return sorted(groups.items(), key=lambda kv: (-kv[1]["hits"], random.random()))

def pick_ranges(key, n, only):
    now = time.time()
    _, kw, _ = SERVICES[key]
    p = pool.setdefault(key, {})
    for r in only:
        p.setdefault(r, {"fails": 0, "until": 0, "seen": now})
    cands = [r for r in only if p[r].get("until", 0) < now]
    hits = Counter(to_range(f.get("number", "")) for f in recent_feed(kw))
    out = []
    while cands and len(out) < n:
        c = random.choices(cands, weights=[1 + hits.get(x, 0) for x in cands])[0]
        out.append(c); cands.remove(c)
    return out

# ---------- background refresh ----------
async def refresh_routes():
    global routes
    while True:
        try:
            if ROUTES_URL:
                r = await http.get(ROUTES_URL, headers=HEADERS); r.raise_for_status()
                d = r.json().get("data", {})
                routes = d.get("active_ranges", []) if isinstance(d, dict) else d
                update_pool()
                logging.info("routes updated: %d", len(routes))
        except Exception as e:
            logging.warning("routes error: %s", e)
        await asyncio.sleep(ROUTES_EVERY)

async def refresh_feed():
    global feed
    while True:
        try:
            if FEED_URL:
                r = await http.get(FEED_URL); r.raise_for_status()
                feed = r.json().get("data", [])
        except Exception as e:
            logging.warning("feed error: %s", e)
        await asyncio.sleep(FEED_EVERY)

# ---------- API ----------
async def provision(range_):
    r = await http.post(GETNUM_URL, headers=HEADERS, json={"range": range_, "is_national": False, "remove_plus": False})
    r.raise_for_status()
    return r.json()["data"]

async def auto_provision(key, only):
    last = None
    for rng in pick_ranges(key, TRY_RANGES, only):
        e = pool[key][rng]
        try:
            d = await provision(rng)
            if is_blocked(rng, d.get("country", "")):
                banned.add(rng); pool[key].pop(rng, None); save_pool(); continue
            e["fails"] = 0; e["country"] = d.get("country", e.get("country", ""))
            return d, rng
        except Exception as ex:
            last = ex; e["fails"] = e.get("fails", 0) + 1
            if e["fails"] >= FAIL_LIMIT:
                e["until"] = time.time() + FAIL_PAUSE; e["fails"] = 0
            logging.warning("range %s failed: %s", rng, ex)
    raise last or RuntimeError("কোনো range পাওয়া যায়নি")

async def fetch_otp(number):
    r = await http.get(OTP_URL, headers=HEADERS); r.raise_for_status()
    for item in r.json().get("data", {}).get("otps", []):
        if item.get("number", "").lstrip("+") == number.lstrip("+"):
            return item.get("otp", "")
    return None

async def watch(app, chat_id, number, seen):
    waited = 0
    try:
        while waited < POLL_MAX:
            await asyncio.sleep(POLL_EVERY); waited += POLL_EVERY
            try:
                msg = await fetch_otp(number)
            except Exception as e:
                logging.warning("otp poll error: %s", e); continue
            if msg and msg not in seen:
                seen.add(msg)
                m = re.search(r"\b\d{4,8}\b", msg)
                code = m.group(0) if m else "?"
                await app.bot.send_message(chat_id, f"✅ OTP পাওয়া গেছে!\n📞 `+{number.lstrip('+')}`\n🔑 কোড: `{code}`\n\n💬 {msg}", parse_mode="Markdown")
                return
        await app.bot.send_message(chat_id, "⌛ সময় শেষ, OTP আসেনি। নতুন নাম্বার নিন।")
    except asyncio.CancelledError:
        pass

# ---------- UI ----------
MAIN_KB = ReplyKeyboardMarkup([["📱 GET NUMBER"], ["🔍 SEARCH NUMBER", "🌐 PROXY/VPN"], ["🎁 REFER", "🔐 2FA"], ["💸 WITHDRAWAL", "🆘 SUPPORT"]], resize_keyboard=True)

def service_kb():
    rows = [[B(v[0], callback_data=f"svc:{k}")] for k, v in SERVICES.items()]
    rows.append([B("❌ CLOSE", callback_data="close")])
    return M(rows)

def country_view(key):
    groups = country_groups(key)[:TOP_N]
    name = SERVICES[key][0]
    if not groups:
        return f"❌ {name}-এর জন্য এখন কোনো দেশ নেই।\nসাপোর্ট: {SUPPORT}", M([[B("⬅️ BACK", callback_data="back")]])
    lines = [f"🌍 Select a country for {name}:", "", f"Top {len(groups)} countries by hits"]
    btns = []
    for i, (code, g) in enumerate(groups, 1):
        fire = " 🔥" if i == 1 and g["hits"] > 0 else ""
        lines.append(f"{i}. {flag(code)} {g['name']} ({code}) • {g['hits']} hits{fire}")
        btns.append(B(f"{flag(code)} {code} · {g['hits']}", callback_data=f"cty:{key}:{code}"))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([B("⬅️ BACK", callback_data="back")])
    return "\n".join(lines), M(rows)

async def safe_edit(q, text, **kw):
    try:
        await q.edit_message_text(text, **kw)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            await q.message.reply_text(text, **kw)

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("স্বাগতম! নিচের মেনু থেকে বেছে নিন 👇", reply_markup=MAIN_KB)

async def get_number_menu(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("📍 Select a service:", reply_markup=service_kb())

async def support(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(f"🆘 সাপোর্ট: {SUPPORT}")

async def show_ranges(u: Update, c: ContextTypes.DEFAULT_TYPE):
    lines = [f"routes: {len(routes)} | feed: {len(feed)}"]
    for k, v in SERVICES.items():
        g = country_groups(k)
        lines.append(f"{v[0]}: " + ", ".join(f"{code}({x['hits']})" for code, x in g[:6]))
    await u.message.reply_text("\n".join(lines))

async def on_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    chat = q.message.chat_id
    if q.data == "close":
        await q.message.delete(); return
    if q.data == "back":
        if t := tasks.pop(chat, None): t.cancel()
        await safe_edit(q, "📍 Select a service:", reply_markup=service_kb()); return
    if q.data.startswith("svc:"):
        if t := tasks.pop(chat, None): t.cancel()
        text, kb = country_view(q.data[4:])
        await safe_edit(q, text, reply_markup=kb); return
    if q.data.startswith("cty:"):
        _, key, code = q.data.split(":")
        if t := tasks.pop(chat, None): t.cancel()
        g = dict(country_groups(key)).get(code)
        if not g:
            text, kb = country_view(key); await safe_edit(q, text, reply_markup=kb); return
        name = SERVICES[key][0]
        try:
            d, rng = await auto_provision(key, set(g["ranges"]))
        except Exception as e:
            text, kb = country_view(key)
            await safe_edit(q, f"❌ এখন নাম্বার পাওয়া যায়নি, আবার চেষ্টা করুন।\nসাপোর্ট: {SUPPORT}\n({e})\n\n" + text, reply_markup=kb); return
        num = d["full_number"]
        kb = M([[B("🔄 Change Number", callback_data=f"cty:{key}:{code}")],
                [B("⬅️ Back", callback_data=f"svc:{key}"), B("❌ Close", callback_data="close")]])
        await safe_edit(q, f"{name}\n📞 নাম্বার: `+{num}`\n🌍 {flag(code)} {d.get('country', g['name'])} | {d.get('operator', '')}\n\n⏳ OTP-র জন্য অপেক্ষা করছি...", parse_mode="Markdown", reply_markup=kb)
        tasks[chat] = asyncio.create_task(watch(c.application, chat, num, set()))

async def soon(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🚧 এই ফিচার শীঘ্রই আসছে।")

async def post_init(app):
    asyncio.create_task(refresh_routes())
    asyncio.create_task(refresh_feed())

def start_health_server():
    """Render-এর Web Service-এর জন্য ছোট্ট সার্ভার (PORT থাকলেই চালু হয়)।"""
    port = os.environ.get("PORT")
    if not port:
        return
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        do_HEAD = do_GET
        def log_message(self, *a): pass
    threading.Thread(target=HTTPServer(("0.0.0.0", int(port)), H).serve_forever, daemon=True).start()

def main():
    start_health_server()
    load_pool()
    for n, v in (("OTP_URL", OTP_URL), ("ROUTES_URL", ROUTES_URL), ("FEED_URL", FEED_URL)):
        if "XXXX" in v or not v:
            logging.error("⚠️ %s এখনও বসানো হয়নি (env.txt দেখুন)", n)
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ranges", show_ranges))
    app.add_handler(CommandHandler("support", support))
    app.add_handler(MessageHandler(filters.Regex("GET NUMBER$"), get_number_menu))
    app.add_handler(MessageHandler(filters.Regex("SUPPORT$"), support))
    app.add_handler(MessageHandler(filters.Regex("SEARCH NUMBER|PROXY/VPN|REFER|2FA|WITHDRAWAL"), soon))
    app.add_handler(CallbackQueryHandler(on_cb))
    asyncio.set_event_loop(asyncio.new_event_loop())  # Python 3.14 fix
    app.run_polling()

if __name__ == "__main__":
    main()
