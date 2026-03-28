"""
ClosePro MVP — AI-Powered Sales Assistant for SMEs
Backend: Tornado (stdlib-compatible, no Flask needed)
DB: SQLite | Auth: PyJWT | AI: OpenAI / Anthropic / built-in fallback
"""

import os, sys, json, csv, io, hashlib, secrets, sqlite3, re
from datetime import datetime, timedelta
from functools import wraps

import jwt                          # PyJWT — already installed
import tornado.ioloop
import tornado.web
import tornado.log

# ─── Config ──────────────────────────────────────────────────────────────────
SECRET_KEY   = os.environ.get("SECRET_KEY", secrets.token_hex(32))
AI_PROVIDER  = os.environ.get("AI_PROVIDER", "openai")
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_KEY= os.environ.get("ANTHROPIC_API_KEY", "")
_HERE        = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.environ.get("DB_PATH", os.path.join(_HERE, "closepro.db"))
PORT         = int(os.environ.get("PORT", 5000))

# ─── Database ────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")   # best on local FS
    except Exception:
        pass                                       # skip on network mounts
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        business_name TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        tone TEXT DEFAULT 'friendly',
        plan TEXT DEFAULT 'starter',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        phone TEXT DEFAULT '',
        email TEXT DEFAULT '',
        product_interest TEXT DEFAULT '',
        status TEXT DEFAULT 'new',
        source TEXT DEFAULT 'whatsapp',
        notes TEXT DEFAULT '',
        deal_value REAL DEFAULT 0,
        last_contact TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER,
        user_id INTEGER NOT NULL,
        customer_message TEXT NOT NULL,
        ai_reply TEXT NOT NULL,
        was_sent INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS followups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        message TEXT NOT NULL,
        followup_type TEXT DEFAULT 'auto',
        scheduled_date TIMESTAMP NOT NULL,
        status TEXT DEFAULT 'pending',
        sent_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (lead_id) REFERENCES leads(id)
    );
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan TEXT DEFAULT 'starter',
        amount REAL DEFAULT 5000,
        currency TEXT DEFAULT 'NGN',
        status TEXT DEFAULT 'active',
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP
    );
    """)
    conn.commit(); conn.close()
    print("  ✓ Database initialised")

# ─── Auth helpers ────────────────────────────────────────────────────────────
def hash_pw(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000)
    return f"{salt}:{h.hex()}"

def verify_pw(stored, provided):
    try:
        salt, h = stored.split(":")
        return hashlib.pbkdf2_hmac("sha256", provided.encode(), salt.encode(), 100_000).hex() == h
    except Exception:
        return False

def make_token(uid, email):
    return jwt.encode(
        {"user_id": uid, "email": email,
         "exp": datetime.utcnow() + timedelta(days=30)},
        SECRET_KEY, algorithm="HS256"
    )

def decode_token(token):
    return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])

# ─── AI Engine ───────────────────────────────────────────────────────────────
def ai_reply(customer_msg, ctx=None):
    ctx = ctx or {}
    biz   = ctx.get("business_name", "our business")
    tone  = ctx.get("tone", "friendly")
    industry = ctx.get("industry", "")
    name  = ctx.get("lead_name", "")

    system = (f"You are ClosePro, an AI sales assistant for '{biz}' ({industry}). "
              f"Tone: {tone}. Lead name: {name or 'customer'}. "
              f"Reply to the customer's WhatsApp message: be helpful, persuasive, warm. "
              f"Include a clear call-to-action. Under 150 words. Sound human.")

    if ANTHROPIC_KEY and AI_PROVIDER == "anthropic":
        try:
            import anthropic
            c = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            r = c.messages.create(model="claude-sonnet-4-20250514", max_tokens=300,
                                  system=system,
                                  messages=[{"role":"user","content":customer_msg}])
            return r.content[0].text
        except Exception as e:
            print(f"Anthropic error: {e}")

    if OPENAI_KEY:
        try:
            import openai
            c = openai.OpenAI(api_key=OPENAI_KEY)
            r = c.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":system},
                          {"role":"user","content":customer_msg}],
                max_tokens=300, temperature=0.8)
            return r.choices[0].message.content
        except Exception as e:
            print(f"OpenAI error: {e}")

    return _fallback(customer_msg, name, biz)

def _fallback(msg, name, biz):
    hi = f"Hi {name}! " if name else "Hi there! "
    m  = msg.lower()
    if any(w in m for w in ["price","cost","how much","naira","amount"]):
        return (f"{hi}Thanks for asking! At {biz} we offer great value. "
                f"Could you tell me exactly what you need so I can give you "
                f"our best price? I'll reply within the hour. 🙏")
    if any(w in m for w in ["available","stock","have you","do you have"]):
        return (f"{hi}Yes, we have that available right now! 🎉 "
                f"Would you like photos, full specs, or pricing? "
                f"Just say the word — I'm here to help.")
    if any(w in m for w in ["deliver","shipping","location","lagos","abuja"]):
        return (f"{hi}Great news — we deliver! 🚚 "
                f"What's your location? I'll confirm the delivery fee "
                f"and timeline for you straight away.")
    if any(w in m for w in ["thank","okay","ok","fine","sure","noted"]):
        return (f"{hi}You're welcome! 😊 Feel free to reach out anytime. "
                f"We're always here to help at {biz}.")
    return (f"{hi}Thanks for reaching out to {biz}! "
            f"I'd love to help — could you share a bit more "
            f"about what you're looking for? "
            f"I'll get back to you with the best option. 🙌")

def gen_followup_sequence(lead_name, product, biz, tone="friendly"):
    msgs = [
        {"day":0,  "message": f"Hi {lead_name}! 👋 Thanks for your interest in {product}. We still have it available — would you like to proceed? Reply YES and I'll sort everything out for you. — {biz}"},
        {"day":3,  "message": f"Hey {lead_name}, just checking in! 😊 The {product} has been really popular — only a few left. Want me to reserve one for you? — {biz}"},
        {"day":7,  "message": f"{lead_name}, here's what our customers are saying about {product}: 'Best purchase I've made!' 🌟 Still interested? I can arrange a special deal for you. — {biz}"},
        {"day":10, "message": f"Special offer for you, {lead_name}! 🎁 Order {product} today and get 10% off + free delivery. Offer expires in 48 hours — reply ORDER to confirm! — {biz}"},
        {"day":14, "message": f"Hi {lead_name}, last message from us — don't want to bother you! 😊 If you're still interested in {product}, we're here anytime. Hope to serve you! — {biz}"},
    ]
    return msgs

# ─── Base Handler ────────────────────────────────────────────────────────────
class BaseHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin",  "*")
        self.set_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.set_header("Content-Type", "application/json")

    def options(self, *args):
        self.set_status(204); self.finish()

    def json(self, data, status=200):
        self.set_status(status)
        self.write(json.dumps(data, default=str))

    def body(self):
        try:
            return json.loads(self.request.body or b"{}")
        except Exception:
            return {}

    def get_user(self):
        auth = self.request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "").strip()
        if not token:
            self.json({"error":"Token required"}, 401); return None
        try:
            return decode_token(token)
        except jwt.ExpiredSignatureError:
            self.json({"error":"Token expired"}, 401); return None
        except Exception:
            self.json({"error":"Invalid token"}, 401); return None

# ─── Auth Handlers ───────────────────────────────────────────────────────────
class SignupHandler(BaseHandler):
    def post(self):
        d = self.body()
        email = d.get("email","").strip().lower()
        pw    = d.get("password","")
        bname = d.get("business_name","")
        ind   = d.get("industry","")
        if not email or not pw:
            return self.json({"error":"Email and password required"}, 400)
        if len(pw) < 6:
            return self.json({"error":"Password must be at least 6 characters"}, 400)
        conn = get_db()
        try:
            if conn.execute("SELECT id FROM users WHERE email=?",(email,)).fetchone():
                return self.json({"error":"Email already registered"}, 409)
            conn.execute("INSERT INTO users (email,password_hash,business_name,industry) VALUES (?,?,?,?)",
                         (email, hash_pw(pw), bname, ind))
            conn.commit()
            uid = conn.execute("SELECT id FROM users WHERE email=?",(email,)).fetchone()["id"]
            exp = (datetime.utcnow()+timedelta(days=30)).isoformat()
            conn.execute("INSERT INTO subscriptions (user_id,plan,amount,status,expires_at) VALUES (?,?,?,?,?)",
                         (uid,"starter",5000,"active",exp))
            conn.commit()
            self.json({"token":make_token(uid,email),"user_id":uid,"email":email}, 201)
        finally:
            conn.close()

class LoginHandler(BaseHandler):
    def post(self):
        d = self.body()
        email = d.get("email","").strip().lower()
        pw    = d.get("password","")
        conn  = get_db()
        try:
            u = conn.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone()
            if not u or not verify_pw(u["password_hash"], pw):
                return self.json({"error":"Invalid email or password"}, 401)
            self.json({"token":make_token(u["id"],email), "user_id":u["id"],
                       "email":email, "business_name":u["business_name"],
                       "industry":u["industry"], "plan":u["plan"]})
        finally:
            conn.close()

class MeHandler(BaseHandler):
    def get(self):
        u = self.get_user();
        if not u: return
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?",(u["user_id"],)).fetchone()
            if not row: return self.json({"error":"Not found"},404)
            self.json({"id":row["id"],"email":row["email"],"business_name":row["business_name"],
                       "industry":row["industry"],"tone":row["tone"],"plan":row["plan"]})
        finally:
            conn.close()

class SettingsHandler(BaseHandler):
    def put(self):
        u = self.get_user();
        if not u: return
        d = self.body()
        conn = get_db()
        try:
            conn.execute("UPDATE users SET business_name=?,industry=?,tone=? WHERE id=?",
                         (d.get("business_name",""), d.get("industry",""),
                          d.get("tone","friendly"), u["user_id"]))
            conn.commit(); self.json({"ok":True})
        finally:
            conn.close()

# ─── Dashboard ───────────────────────────────────────────────────────────────
class DashboardHandler(BaseHandler):
    def get(self):
        u = self.get_user();
        if not u: return
        uid = u["user_id"]
        conn = get_db()
        try:
            def cnt(q, *a): return conn.execute(q,a).fetchone()[0]
            total    = cnt("SELECT COUNT(*) FROM leads WHERE user_id=?", uid)
            active   = cnt("SELECT COUNT(*) FROM leads WHERE user_id=? AND status IN ('new','contacted','hot')", uid)
            convt    = cnt("SELECT COUNT(*) FROM leads WHERE user_id=? AND status='converted'", uid)
            convos   = cnt("SELECT COUNT(*) FROM conversations WHERE user_id=?", uid)
            pending  = cnt("SELECT COUNT(*) FROM followups WHERE user_id=? AND status='pending'", uid)
            revenue  = conn.execute("SELECT COALESCE(SUM(deal_value),0) FROM leads WHERE user_id=? AND status='converted'",(uid,)).fetchone()[0]
            rate     = round(convt/total*100,1) if total else 0
            recent   = [dict(r) for r in conn.execute("SELECT * FROM leads WHERE user_id=? ORDER BY created_at DESC LIMIT 5",(uid,)).fetchall()]
            upcoming = [dict(r) for r in conn.execute(
                "SELECT f.*,l.name as lead_name FROM followups f JOIN leads l ON f.lead_id=l.id WHERE f.user_id=? AND f.status='pending' ORDER BY f.scheduled_date ASC LIMIT 5",(uid,)).fetchall()]
            self.json({"total_leads":total,"active_leads":active,"converted":convt,"conversion_rate":rate,
                       "total_conversations":convos,"pending_followups":pending,"total_revenue":revenue,
                       "recent_leads":recent,"upcoming_followups":upcoming})
        finally:
            conn.close()

# ─── AI Handlers ─────────────────────────────────────────────────────────────
class AIReplyHandler(BaseHandler):
    def post(self):
        u = self.get_user();
        if not u: return
        d = self.body()
        msg = d.get("message","").strip()
        if not msg: return self.json({"error":"Message required"},400)
        lead_id = d.get("lead_id")
        conn = get_db()
        try:
            user = conn.execute("SELECT * FROM users WHERE id=?",(u["user_id"],)).fetchone()
            ctx  = {"business_name":user["business_name"],"tone":user["tone"],"industry":user["industry"]}
            if lead_id:
                lead = conn.execute("SELECT * FROM leads WHERE id=? AND user_id=?",(lead_id,u["user_id"])).fetchone()
                if lead: ctx["lead_name"] = lead["name"]
            reply = ai_reply(msg, ctx)
            conn.execute("INSERT INTO conversations (lead_id,user_id,customer_message,ai_reply) VALUES (?,?,?,?)",
                         (lead_id, u["user_id"], msg, reply))
            conn.commit()
            self.json({"reply":reply})
        finally:
            conn.close()

class AISequenceHandler(BaseHandler):
    def post(self):
        u = self.get_user();
        if not u: return
        d = self.body()
        lead_id = d.get("lead_id")
        if not lead_id: return self.json({"error":"lead_id required"},400)
        conn = get_db()
        try:
            lead = conn.execute("SELECT * FROM leads WHERE id=? AND user_id=?",(lead_id,u["user_id"])).fetchone()
            if not lead: return self.json({"error":"Lead not found"},404)
            user = conn.execute("SELECT * FROM users WHERE id=?",(u["user_id"],)).fetchone()
            seq  = gen_followup_sequence(lead["name"], lead["product_interest"] or "our products",
                                         user["business_name"] or "our business", user["tone"])
            now  = datetime.utcnow()
            for item in seq:
                sched = (now + timedelta(days=item["day"])).isoformat()
                conn.execute("INSERT INTO followups (lead_id,user_id,message,followup_type,scheduled_date) VALUES (?,?,?,?,?)",
                             (lead_id, u["user_id"], item["message"], "auto", sched))
            conn.execute("UPDATE leads SET status='contacted',last_contact=? WHERE id=?",(now.isoformat(),lead_id))
            conn.commit()
            self.json({"sequence":seq,"count":len(seq)})
        finally:
            conn.close()

# ─── Lead Handlers ───────────────────────────────────────────────────────────
class LeadsHandler(BaseHandler):
    def get(self):
        u = self.get_user();
        if not u: return
        status = self.get_argument("status","")
        conn = get_db()
        try:
            q = "SELECT * FROM leads WHERE user_id=?"
            a = [u["user_id"]]
            if status: q += " AND status=?"; a.append(status)
            rows = conn.execute(q+" ORDER BY created_at DESC", a).fetchall()
            self.json([dict(r) for r in rows])
        finally:
            conn.close()

    def post(self):
        u = self.get_user();
        if not u: return
        d = self.body()
        conn = get_db()
        try:
            conn.execute("INSERT INTO leads (user_id,name,phone,email,product_interest,status,source,notes,deal_value) VALUES (?,?,?,?,?,?,?,?,?)",
                         (u["user_id"],d.get("name",""),d.get("phone",""),d.get("email",""),
                          d.get("product_interest",""),d.get("status","new"),d.get("source","whatsapp"),
                          d.get("notes",""),d.get("deal_value",0)))
            conn.commit()
            lid  = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.json(dict(conn.execute("SELECT * FROM leads WHERE id=?",(lid,)).fetchone()),201)
        finally:
            conn.close()

class LeadHandler(BaseHandler):
    def put(self, lid):
        u = self.get_user();
        if not u: return
        d = self.body()
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM leads WHERE id=? AND user_id=?",(lid,u["user_id"])).fetchone()
            if not row: return self.json({"error":"Not found"},404)
            conn.execute("UPDATE leads SET name=?,phone=?,email=?,product_interest=?,status=?,source=?,notes=?,deal_value=?,last_contact=? WHERE id=?",
                         (d.get("name",row["name"]),d.get("phone",row["phone"]),d.get("email",row["email"]),
                          d.get("product_interest",row["product_interest"]),d.get("status",row["status"]),
                          d.get("source",row["source"]),d.get("notes",row["notes"]),
                          d.get("deal_value",row["deal_value"]),datetime.utcnow().isoformat(),lid))
            conn.commit()
            self.json(dict(conn.execute("SELECT * FROM leads WHERE id=?",(lid,)).fetchone()))
        finally:
            conn.close()

    def delete(self, lid):
        u = self.get_user();
        if not u: return
        conn = get_db()
        try:
            conn.execute("DELETE FROM followups WHERE lead_id=? AND user_id=?",(lid,u["user_id"]))
            conn.execute("DELETE FROM conversations WHERE lead_id=? AND user_id=?",(lid,u["user_id"]))
            conn.execute("DELETE FROM leads WHERE id=? AND user_id=?",(lid,u["user_id"]))
            conn.commit(); self.json({"ok":True})
        finally:
            conn.close()

# ─── Follow-up Handlers ──────────────────────────────────────────────────────
class FollowupsHandler(BaseHandler):
    def get(self):
        u = self.get_user();
        if not u: return
        status = self.get_argument("status","pending")
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT f.*,l.name as lead_name,l.phone as lead_phone FROM followups f JOIN leads l ON f.lead_id=l.id WHERE f.user_id=? AND f.status=? ORDER BY f.scheduled_date ASC",
                (u["user_id"],status)).fetchall()
            self.json([dict(r) for r in rows])
        finally:
            conn.close()

class FollowupSendHandler(BaseHandler):
    def post(self, fid):
        u = self.get_user();
        if not u: return
        conn = get_db()
        try:
            conn.execute("UPDATE followups SET status='sent',sent_at=? WHERE id=? AND user_id=?",
                         (datetime.utcnow().isoformat(),fid,u["user_id"]))
            conn.commit(); self.json({"ok":True})
        finally:
            conn.close()

class FollowupSkipHandler(BaseHandler):
    def post(self, fid):
        u = self.get_user();
        if not u: return
        conn = get_db()
        try:
            conn.execute("UPDATE followups SET status='skipped' WHERE id=? AND user_id=?",(fid,u["user_id"]))
            conn.commit(); self.json({"ok":True})
        finally:
            conn.close()

# ─── Conversations ───────────────────────────────────────────────────────────
class ConversationsHandler(BaseHandler):
    def get(self):
        u = self.get_user();
        if not u: return
        lid = self.get_argument("lead_id","")
        conn = get_db()
        try:
            if lid:
                rows = conn.execute("SELECT * FROM conversations WHERE user_id=? AND lead_id=? ORDER BY created_at DESC",(u["user_id"],lid)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM conversations WHERE user_id=? ORDER BY created_at DESC LIMIT 50",(u["user_id"],)).fetchall()
            self.json([dict(r) for r in rows])
        finally:
            conn.close()

# ─── Subscription ────────────────────────────────────────────────────────────
class SubscriptionHandler(BaseHandler):
    def get(self):
        u = self.get_user();
        if not u: return
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM subscriptions WHERE user_id=? ORDER BY started_at DESC LIMIT 1",(u["user_id"],)).fetchone()
            self.json(dict(row) if row else {"plan":"free","status":"none"})
        finally:
            conn.close()

    def post(self):
        u = self.get_user();
        if not u: return
        d = self.body()
        plan = d.get("plan","starter")
        amounts = {"starter":5000,"growth":12000,"pro":25000}
        amount  = amounts.get(plan,5000)
        exp     = (datetime.utcnow()+timedelta(days=30)).isoformat()
        conn = get_db()
        try:
            conn.execute("INSERT INTO subscriptions (user_id,plan,amount,status,expires_at) VALUES (?,?,?,?,?)",
                         (u["user_id"],plan,amount,"active",exp))
            conn.execute("UPDATE users SET plan=? WHERE id=?",(plan,u["user_id"]))
            conn.commit(); self.json({"plan":plan,"amount":amount,"expires_at":exp})
        finally:
            conn.close()

# ─── Export ──────────────────────────────────────────────────────────────────
class ExportLeadsHandler(BaseHandler):
    def get(self):
        u = self.get_user();
        if not u: return
        conn = get_db()
        try:
            rows = conn.execute("SELECT * FROM leads WHERE user_id=? ORDER BY created_at DESC",(u["user_id"],)).fetchall()
            buf  = io.StringIO()
            w    = csv.writer(buf)
            w.writerow(["Name","Phone","Email","Product","Status","Source","Deal Value","Last Contact","Created"])
            for r in rows:
                w.writerow([r["name"],r["phone"],r["email"],r["product_interest"],
                            r["status"],r["source"],r["deal_value"],r["last_contact"],r["created_at"]])
            self.set_header("Content-Type","text/csv")
            self.set_header("Content-Disposition",'attachment; filename="closepro_leads.csv"')
            self.write(buf.getvalue())
        finally:
            conn.close()

# ─── Static (serve React frontend) ──────────────────────────────────────────
class StaticHandler(tornado.web.StaticFileHandler):
    def validate_absolute_path(self, root, absolute_path):
        # Always serve index.html for unknown routes (SPA)
        if not os.path.exists(absolute_path) or os.path.isdir(absolute_path):
            return os.path.join(root, "index.html")
        return absolute_path

# ─── App Factory ─────────────────────────────────────────────────────────────
def make_app():
    static_path = os.path.join(os.path.dirname(__file__), "static")
    return tornado.web.Application([
        # Auth
        (r"/api/auth/signup",   SignupHandler),
        (r"/api/auth/login",    LoginHandler),
        (r"/api/auth/me",       MeHandler),
        (r"/api/auth/settings", SettingsHandler),
        # Dashboard
        (r"/api/dashboard",     DashboardHandler),
        # AI
        (r"/api/ai/reply",              AIReplyHandler),
        (r"/api/ai/followup-sequence",  AISequenceHandler),
        # Leads
        (r"/api/leads",         LeadsHandler),
        (r"/api/leads/(\d+)",   LeadHandler),
        # Follow-ups
        (r"/api/followups",             FollowupsHandler),
        (r"/api/followups/(\d+)/send",  FollowupSendHandler),
        (r"/api/followups/(\d+)/skip",  FollowupSkipHandler),
        # Conversations
        (r"/api/conversations", ConversationsHandler),
        # Subscription
        (r"/api/subscription",  SubscriptionHandler),
        # Export
        (r"/api/export/leads",  ExportLeadsHandler),
        # Static (catch-all — must be last)
        (r"/(.*)", StaticHandler, {"path": static_path, "default_filename": "index.html"}),
    ], debug=False)

# ─── Boot ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Load .env file if present
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        # Re-read env vars after loading .env
        OPENAI_KEY    = os.environ.get("OPENAI_API_KEY", "")
        ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    init_db()

    ai_status = ("OpenAI ✓" if OPENAI_KEY else "") or \
                ("Anthropic ✓" if ANTHROPIC_KEY else "Fallback mode (no API key)")

    print(f"""
╔══════════════════════════════════════════════════════╗
║              CLOSEPRO MVP — Running!                 ║
╠══════════════════════════════════════════════════════╣
║  Open your browser:  http://localhost:{PORT:<5}          ║
║  AI engine:          {ai_status:<32}║
║  Database:           {DB_PATH:<32}║
║  Press Ctrl+C to stop                                ║
╚══════════════════════════════════════════════════════╝
    """)

    app = make_app()
    app.listen(PORT)
    tornado.ioloop.IOLoop.current().start()
