"""Smart Farmer API (production-ready).  Run:  npm start   (or: python app.py)
Settings come from the .env file. Real-time alerts use Server-Sent Events; read-aloud uses neural voices."""
import asyncio
import base64
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal

import env_loader  # noqa: F401  (loads .env)
import mysql.connector
from flask import Flask, Response, jsonify, request, send_from_directory, session
from mysql.connector import pooling
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smartfarmer")
BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="")
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
if not os.getenv("SECRET_KEY") or os.getenv("SECRET_KEY") == "change-this-secret":
    log.warning("SECRET_KEY is not set to a private value. Put a long random string in .env")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE") == "1", PERMANENT_SESSION_LIFETIME=8 * 3600)

# ---------------------------------------------------------------- database
_pool = None


def db():
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(pool_name="sf", pool_size=20, autocommit=True,
                                            host=os.getenv("DB_HOST", "localhost"), user=os.getenv("DB_USER", "root"),
                                            password=os.getenv("DB_PASS", ""), database=os.getenv("DB_NAME", "smart_farmer"))
    return _pool.get_connection()


def cv(v):
    if isinstance(v, Decimal): return float(v)
    return v.isoformat() if hasattr(v, "isoformat") else v


def run(sql, args=(), one=False, write=False):
    c = db(); cur = c.cursor(dictionary=True)
    try:
        cur.execute(sql, args)
        if write:
            return cur.lastrowid
        rows = [{k: cv(v) for k, v in r.items()} for r in cur.fetchall()]
        return (rows[0] if rows else None) if one else rows
    finally:
        cur.close(); c.close()


# ---------------------------------------------------------------- real-time notifications (SSE) + optional SMS
subs, subs_lock, overdue_lock = [], threading.Lock(), threading.Lock()


def send_sms(fid, text):                       # optional: set TWILIO_SID / TWILIO_TOKEN / TWILIO_FROM in .env
    sid, tok, frm = os.getenv("TWILIO_SID"), os.getenv("TWILIO_TOKEN"), os.getenv("TWILIO_FROM")
    if not (sid and tok and frm): return
    try:
        f = run("SELECT phone FROM farmer WHERE farmer_id=%s", (fid,), one=True)
        body = urllib.parse.urlencode({"To": "+91" + f["phone"], "From": frm, "Body": text}).encode()
        req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=body)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode())
        urllib.request.urlopen(req, timeout=10)
        log.info("SMS sent to farmer %s", fid)
    except Exception as e:
        log.warning("SMS failed: %s", e)


def notify(kind, data, fid=None, sms=None):   # best-effort: never let an alert failure undo or hide a saved loan/payment
    try:
        nid = run("INSERT INTO notification(kind,data,farmer_id) VALUES(%s,%s,%s)", (kind, json.dumps(data), fid), write=True)
        item = json.dumps({"notif_id": nid, "kind": kind, "data": data, "farmer_id": fid, "is_read": 0,
                           "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        with subs_lock:
            for q in list(subs): q.put(item)
        if sms and fid: threading.Thread(target=send_sms, args=(fid, sms), daemon=True).start()
    except Exception as e:
        log.warning("notify failed: %s", e)


def fname(fid):
    r = run("SELECT name FROM farmer WHERE farmer_id=%s", (fid,), one=True)
    return r["name"] if r else "?"


def risk_of(fid):
    return run("SELECT risk_score, risk_status FROM farmer WHERE farmer_id=%s", (fid,), one=True)


def check_risk(fid, before):                   # notify when a farmer's risk band changes
    try:
        after = risk_of(fid)
        if before and after and after["risk_status"] != before["risk_status"]:
            notify("risk", {"name": fname(fid), "status": after["risk_status"], "score": after["risk_score"]}, fid)
    except Exception as e:
        log.warning("risk check failed: %s", e)


def check_overdue():                           # flags late loans, then alerts (runs every minute and on dashboard load)
    with overdue_lock:
        rows = run("""SELECT l.loan_id, l.farmer_id, f.name, f.risk_status FROM loan l JOIN farmer f USING(farmer_id)
                      WHERE l.status='Active' AND (l.due_date<CURDATE() OR EXISTS(SELECT 1 FROM installment i
                              WHERE i.loan_id=l.loan_id AND i.status<>'Paid' AND i.due_date<CURDATE()))""")
        if not rows: return
        run("CALL sp_flag_overdue()", write=True)
        for r in rows:
            notify("overdue", {"name": r["name"], "loan_id": r["loan_id"]}, r["farmer_id"],
                   sms=f"Smart Farmer: your loan #{r['loan_id']} is overdue. Please repay as soon as possible.")
        seen = {}
        for r in rows: seen.setdefault(r["farmer_id"], {"risk_status": r["risk_status"]})
        for fid, before in seen.items(): check_risk(fid, before)


def overdue_watch():
    while True:
        time.sleep(60)
        try: check_overdue()
        except Exception as e: log.warning("overdue check failed: %s", e)


# ---------------------------------------------------------------- guards, errors, headers
FAILS = {}


@app.before_request
def guard():
    if request.path.startswith("/api/") and request.path not in ("/api/login", "/api/register", "/api/health") \
            and "officer" not in session:
        return jsonify(error="Login required"), 401


@app.after_request
def headers(r):
    r.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"})
    return r


@app.errorhandler(Exception)
def fail(e):
    if isinstance(e, HTTPException): return jsonify(error=e.description), e.code
    if isinstance(e, ValueError): return jsonify(error=str(e)), 400
    if isinstance(e, mysql.connector.IntegrityError) and e.errno == 1062:
        return jsonify(error="This phone number is already registered"), 409
    if isinstance(e, mysql.connector.Error) and getattr(e, "errno", None) in (1146, 1305, 1054, 1359):
        log.error("Database is older than the code (%s). Run:  npm run setup", e)
        return jsonify(error="Database is out of date. Stop the app and run: npm run setup"), 500
    log.exception("unhandled error")
    return jsonify(error="Something went wrong. Please try again."), 500


@app.get("/")
def home():
    for d in (os.path.join(BASE, "static"), BASE):
        if os.path.exists(os.path.join(d, "index.html")):
            return send_from_directory(d, "index.html")
    return "index.html not found. Put it inside a folder named static next to app.py.", 404


@app.get("/api/health")
def health():
    run("SELECT 1"); return jsonify(status="ok")


# ---------------------------------------------------------------- auth
@app.post("/api/login")
def login():
    d = request.json or {}
    u = d.get("username", "").strip().lower(); key = (request.remote_addr, u); now = time.time()
    FAILS[key] = [t for t in FAILS.get(key, []) if now - t < 600]
    if len(FAILS[key]) >= 5:
        return jsonify(error="Too many attempts. Try again in a few minutes."), 429
    o = run("SELECT * FROM officer WHERE username=%s", (u,), one=True)
    if not o or not check_password_hash(o["password_hash"], d.get("password", "")):
        FAILS[key].append(now); log.info("failed login for %s", u)
        return jsonify(error="Invalid username or password"), 401
    FAILS.pop(key, None); session.clear(); session["officer"] = o["username"]; session.permanent = True
    return jsonify(ok=True, name=o["full_name"])


@app.post("/api/register")
def register():
    d = request.json or {}
    u, name, pw = d.get("username", "").strip().lower(), d.get("full_name", "").strip(), d.get("password", "")
    if not name: raise ValueError("Please enter your full name")
    if not re.fullmatch(r"[a-z0-9_]{3,20}", u): raise ValueError("Username must be 3 to 20 letters, numbers or _")
    if len(pw) < 6: raise ValueError("Password must be at least 6 characters")
    if run("SELECT 1 FROM officer WHERE username=%s", (u,), one=True): raise ValueError("Username already taken")
    run("INSERT INTO officer(username,password_hash,full_name) VALUES(%s,%s,%s)", (u, generate_password_hash(pw), name[:80]), write=True)
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear(); return jsonify(ok=True)


@app.get("/api/me")
def me():
    return jsonify(user=session["officer"])


# ---------------------------------------------------------------- notifications
@app.get("/api/events")
def events():
    q = queue.Queue()
    with subs_lock: subs.append(q)

    def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                try: yield "data: " + q.get(timeout=20) + "\n\n"
                except queue.Empty: yield ": ping\n\n"
        finally:
            with subs_lock:
                if q in subs: subs.remove(q)
    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/notifications")
def notifications():
    items = run("SELECT notif_id,kind,data,farmer_id,is_read,created_at FROM notification ORDER BY notif_id DESC LIMIT 30")
    return jsonify(items=items, unread=run("SELECT COUNT(*) c FROM notification WHERE is_read=0", one=True)["c"])


@app.post("/api/notifications/read")
def notifications_read():
    run("UPDATE notification SET is_read=1 WHERE is_read=0", write=True); return jsonify(ok=True)


# ---------------------------------------------------------------- read-aloud: tries natural neural voices, then Google voices
VOICES = {"en": "en-IN-NeerjaNeural", "hi": "hi-IN-SwaraNeural", "ta": "ta-IN-PallaviNeural", "te": "te-IN-ShrutiNeural"}
_tts = {}


def _edge(text, lang):                         # engine 1: Microsoft neural voices (most natural)
    import edge_tts

    async def synth():
        buf = b""
        async for ch in edge_tts.Communicate(text, VOICES.get(lang, VOICES["en"]), rate="-8%").stream():
            if ch["type"] == "audio": buf += ch["data"]
        return buf
    return asyncio.run(asyncio.wait_for(synth(), 25))


def _chunks(t, n=180):
    out = []
    for s in re.split(r"(?<=[.!?\u0964])\s+", t):
        while len(s) > n:
            cut = s.rfind(" ", 0, n); cut = cut if cut > 0 else n
            out.append(s[:cut]); s = s[cut:].strip()
        if s: out.append(s)
    return out


def _google(text, lang):                       # engine 2: Google voices, needs no install (Hindi, Tamil, Telugu supported)
    out = b""
    for part in _chunks(text):
        url = "https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&ttsspeed=0.85&tl=%s&q=%s" % (lang, urllib.parse.quote(part))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://translate.google.com/"})
        out += urllib.request.urlopen(req, timeout=15).read()
    return out


def speak_text(text, lang):
    errors, audio, engine = [], b"", ""
    for name, fn in (("edge", _edge), ("google", _google)):
        try:
            audio = fn(text, lang)
        except ImportError:
            errors.append(f"{name}: package not installed (run: pip install -U -r requirements.txt)")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__} {e}")
        if audio:
            engine = name; break
    return audio, engine, errors


@app.post("/api/tts")
def tts():
    d = request.json or {}
    text, lang = (d.get("text") or "").strip()[:700], d.get("lang", "en")
    if lang not in VOICES: lang = "en"
    if not text: raise ValueError("Nothing to read")
    key = hashlib.md5((lang + text).encode()).hexdigest()
    if key not in _tts:
        audio, engine, errors = speak_text(text, lang)
        if not audio:
            log.warning("TTS failed: %s", errors)
            return jsonify(error="Voice service unreachable. Check your internet connection.", detail=errors), 502
        if len(_tts) > 150: _tts.clear()
        _tts[key] = (audio, engine)
    audio, engine = _tts[key]
    return Response(audio, mimetype="audio/mpeg", headers={"X-Voice-Engine": engine})


@app.get("/api/tts/test")                      # open http://localhost:5000/api/tts/test after login to see what works
def tts_test():
    res = {}
    for lang in VOICES:
        res[lang] = {}
        for name, fn in (("edge", _edge), ("google", _google)):
            try: res[lang][name] = "OK" if fn("test", lang) else "empty"
            except Exception as e: res[lang][name] = f"{type(e).__name__}: {e}"
    return jsonify(res)


# ---------------------------------------------------------------- business API
@app.get("/api/dashboard")
def dashboard():
    try: check_overdue()
    except Exception as e: log.warning("overdue check failed: %s", e)   # never block the dashboard on the sweep
    return jsonify(run("""SELECT (SELECT COUNT(*) FROM farmer) farmers,
        (SELECT COUNT(*) FROM loan WHERE status='Active') active, (SELECT COUNT(*) FROM loan WHERE status='Overdue') overdue,
        (SELECT available_balance FROM fund WHERE fund_id=1) fund, (SELECT COUNT(*) FROM farmer WHERE risk_status='Low') low,
        (SELECT COUNT(*) FROM farmer WHERE risk_status='Medium') medium, (SELECT COUNT(*) FROM farmer WHERE risk_status='High') high""", one=True))


@app.get("/api/farmers")
def farmers():
    return jsonify(run("SELECT * FROM farmer ORDER BY farmer_id"))


@app.post("/api/farmers")
def add_farmer():
    d = request.json or {}
    name, village, phone = d.get("name", "").strip()[:80], d.get("village", "").strip()[:60], d.get("phone", "").strip()
    if not name or not village: raise ValueError("Please enter the farmer's name and village")
    if not re.fullmatch(r"\d{10}", phone): raise ValueError("Enter a valid 10-digit phone number")
    fid = run("INSERT INTO farmer(name,phone,village) VALUES(%s,%s,%s)", (name, phone, village), write=True)
    notify("farmer", {"name": name}, fid)
    return jsonify(id=fid)


@app.get("/api/farmers/<int:fid>")
def profile(fid):
    return jsonify(
        farmer=run("SELECT * FROM farmer WHERE farmer_id=%s", (fid,), one=True),
        cycles=run("SELECT cc.*, c.crop_name FROM crop_cycle cc JOIN crop c USING(crop_id) WHERE farmer_id=%s ORDER BY sowing_date DESC", (fid,)),
        loans=run("""SELECT l.*, COALESCE((SELECT SUM(amount_paid) FROM repayment r WHERE r.loan_id=l.loan_id),0) paid
                     FROM loan l WHERE farmer_id=%s ORDER BY disbursement_date DESC""", (fid,)),
        log=run("SELECT score, status, logged_at FROM risk_log WHERE farmer_id=%s ORDER BY log_id", (fid,)))


@app.get("/api/crops")
def crops():
    return jsonify(run("SELECT * FROM crop ORDER BY crop_name"))


@app.get("/api/cycles")
def cycles():
    fid = request.args.get("farmer_id")
    sql = "SELECT cc.*, c.crop_name, f.name farmer FROM crop_cycle cc JOIN crop c USING(crop_id) JOIN farmer f USING(farmer_id)"
    return jsonify(run(sql + (" WHERE farmer_id=%s" if fid else "") + " ORDER BY sowing_date DESC", (fid,) if fid else ()))


@app.post("/api/cycles")
def add_cycle():
    d = request.json or {}
    if float(d["area"]) <= 0: raise ValueError("Area must be greater than zero")
    return jsonify(id=run("INSERT INTO crop_cycle(farmer_id,crop_id,area,sowing_date) VALUES(%s,%s,%s,%s)",
                          (d["farmer_id"], d["crop_id"], d["area"], d["sowing_date"]), write=True))


@app.post("/api/cycles/<int:cid>/harvest")
def harvest(cid):
    d = request.json or {}
    if d.get("status") not in ("Good", "Poor") or float(d["yield_qty"]) < 0: raise ValueError("Invalid harvest data")
    fid = run("SELECT farmer_id FROM crop_cycle WHERE cycle_id=%s", (cid,), one=True)["farmer_id"]
    before = risk_of(fid)
    run("UPDATE crop_cycle SET harvest_date=CURDATE(), yield_qty=%s, status=%s WHERE cycle_id=%s", (d["yield_qty"], d["status"], cid), write=True)
    run("CALL sp_calc_risk(%s)", (fid,), write=True)
    notify("harvest", {"name": fname(fid), "status": d["status"]}, fid)
    check_risk(fid, before)
    return jsonify(ok=True)


@app.get("/api/loans")
def loans():
    return jsonify(run("""SELECT l.*, f.name farmer, COALESCE((SELECT SUM(amount_paid) FROM repayment r WHERE r.loan_id=l.loan_id),0) paid,
        (SELECT due_date FROM installment i WHERE i.loan_id=l.loan_id AND i.status<>'Paid' ORDER BY seq LIMIT 1) next_due,
        (SELECT amount-paid FROM installment i WHERE i.loan_id=l.loan_id AND i.status<>'Paid' ORDER BY seq LIMIT 1) next_amt
        FROM loan l JOIN farmer f USING(farmer_id) ORDER BY l.loan_id DESC"""))


@app.get("/api/loans/<int:lid>/installments")
def installments(lid):
    loan = run("""SELECT l.*, f.name farmer, COALESCE((SELECT SUM(amount_paid) FROM repayment r WHERE r.loan_id=l.loan_id),0) paid
                  FROM loan l JOIN farmer f USING(farmer_id) WHERE l.loan_id=%s""", (lid,), one=True)
    if not loan: raise ValueError("Loan not found")
    return jsonify(loan=loan, items=run("SELECT seq,due_date,amount,paid,status FROM installment WHERE loan_id=%s ORDER BY seq", (lid,)))


@app.post("/api/loans")
def issue_loan():                              # runs the transactional stored procedure
    d = request.json or {}
    amount, days, fid, n = float(d["amount"]), int(d.get("days", 180)), int(d["farmer_id"]), int(d.get("installments", 1))
    if amount <= 0 or not 1 <= days <= 1095 or not 1 <= n <= 24 or n > days: raise ValueError("Invalid loan amount or period")
    before = risk_of(fid)
    c = db(); cur = c.cursor()
    try:
        cur.execute("SET @m=''")
        cur.execute("CALL sp_disburse_loan(%s,%s,%s,%s,%s,@m)", (fid, d.get("cycle_id") or None, amount, days, n))
        cur.execute("SELECT @m"); m = cur.fetchone()[0]
        m = m.decode() if isinstance(m, bytes) else m
    finally:
        cur.close(); c.close()
    if m == "OK":
        notify("loan", {"name": fname(fid), "amount": amount}, fid,
               sms=f"Smart Farmer: a loan of Rs {amount:,.0f} has been disbursed to you. Please repay within {days} days.")
        check_risk(fid, before)
    return jsonify(ok=m == "OK", message=m), (200 if m == "OK" else 400)


@app.post("/api/repayments")
def repay():
    d = request.json or {}
    amount = float(d["amount"])
    if amount <= 0: raise ValueError("Amount must be greater than zero")
    loan = run("""SELECT l.farmer_id, l.amount, l.status, f.name, COALESCE((SELECT SUM(amount_paid) FROM repayment r WHERE r.loan_id=l.loan_id),0) paid
                  FROM loan l JOIN farmer f USING(farmer_id) WHERE l.loan_id=%s""", (d["loan_id"],), one=True)
    if not loan: raise ValueError("Loan not found")
    if loan["status"] == "Repaid": raise ValueError("This loan is already fully repaid")
    if amount > round(loan["amount"] - loan["paid"], 2) + 0.001: raise ValueError("Amount exceeds the remaining balance")
    before = risk_of(loan["farmer_id"])
    run("INSERT INTO repayment(loan_id,amount_paid,payment_date) VALUES(%s,%s,CURDATE())", (d["loan_id"], amount), write=True)
    notify("repay", {"name": loan["name"], "amount": amount}, loan["farmer_id"],
           sms=f"Smart Farmer: we received your repayment of Rs {amount:,.0f}. Thank you.")
    check_risk(loan["farmer_id"], before)
    return jsonify(ok=True)


@app.get("/api/eligible")
def eligible():
    return jsonify(run("SELECT * FROM v_eligible_farmers ORDER BY risk_score DESC"))


# ---------------------------------------------------------------- startup
def init_db():                                 # idempotent: safe to run on every start
    run("""CREATE TABLE IF NOT EXISTS officer(officer_id INT AUTO_INCREMENT PRIMARY KEY, username VARCHAR(40) UNIQUE NOT NULL,
           password_hash VARCHAR(255) NOT NULL, full_name VARCHAR(80) NOT NULL)""", write=True)
    run("""CREATE TABLE IF NOT EXISTS notification(notif_id INT AUTO_INCREMENT PRIMARY KEY, kind VARCHAR(20) NOT NULL,
           data VARCHAR(500) NOT NULL, farmer_id INT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
           is_read TINYINT(1) NOT NULL DEFAULT 0)""", write=True)
    if not run("SELECT 1 FROM officer", one=True):
        run("INSERT INTO officer(username,password_hash,full_name) VALUES(%s,%s,%s)",
            ("officer", generate_password_hash("farmer123"), "Cooperative Officer"), write=True)


def check_schema():
    need = {"installment": "table", "sp_make_schedule": "proc", "sp_apply_payment": "proc", "sp_flag_overdue": "proc", "sp_disburse_loan": "proc"}
    miss = []
    for n, k in need.items():
        if k == "table": ok = run("SHOW TABLES LIKE %s", (n,))
        else: ok = run("SELECT 1 FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME=%s", (n,))
        if not ok: miss.append(n)
    if miss: log.error("DATABASE IS OUT OF DATE, missing: %s  ->  stop the app and run: npm run setup", ", ".join(miss))


def main():
    init_db()
    try: check_schema()
    except Exception as e: log.warning("schema check skipped: %s", e)
    debug = os.getenv("FLASK_DEBUG") == "1"
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "5000"))
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Thread(target=overdue_watch, daemon=True).start()
    if debug:
        app.run(host=host, port=port, debug=True, threaded=True); return
    try:
        from waitress import serve
    except ImportError:
        log.warning("waitress not installed: using the development server (pip install waitress)")
        app.run(host=host, port=port, threaded=True); return
    log.info("Smart Farmer running on http://%s:%s", host, port)
    serve(app, host=host, port=port, threads=32)   # SSE keeps one thread per open tab, so allow plenty


if __name__ == "__main__":
    main()
