import os, time, datetime as dt
import psycopg2, psycopg2.extras
from flask import Flask, request, jsonify, render_template, abort
from prometheus_flask_exporter import PrometheusMetrics

app = Flask(__name__)
PrometheusMetrics(app)
DB = "issues.db"

CATS = {
    "Pothole": ["pothole", "road", "crack", "pavement", "asphalt"],
    "Garbage Overflow": ["garbage", "trash", "waste", "dump", "litter", "bin"],
    "Water Leakage": ["water", "leak", "pipe", "drain", "flood", "sewage"],
    "Broken Streetlight": ["streetlight", "light", "lamp", "dark", "pole", "wire"],
    "Traffic Issue": ["traffic", "signal", "jam", "parking"],
    "Encroachment": ["encroach", "illegal", "footpath", "vendor", "construction"],
}
DEPT = {
    "Pothole": "Roads & Buildings", "Garbage Overflow": "Sanitation",
    "Water Leakage": "Water Board", "Broken Streetlight": "Electrical",
    "Traffic Issue": "Traffic Police", "Encroachment": "Town Planning",
}
URGENT = ["danger", "accident", "urgent", "fire", "injury", "exposed", "child", "manhole"]
PROG = {"open": 0, "in_progress": 60, "resolved": 100}
LOCS = ["MG Road, Sector 22", "Banjara Hills", "Madhapur", "Gachibowli", "Kukatpally",
        "Secunderabad", "Charminar", "Ameerpet", "Dilsukhnagar", "Begumpet", "Jubilee Hills", "Uppal"]

def analyse(text, chosen):
    t = text.lower()
    cat = chosen if chosen in CATS else next(
        (c for c, k in CATS.items() if any(w in t for w in k)), "Other")
    if any(w in t for w in URGENT): pri = "high"
    elif cat in ("Pothole", "Water Leakage", "Traffic Issue"): pri = "medium"
    else: pri = "low"
    return cat, pri, DEPT.get(cat, "General Administration")

class Con:
    def __init__(self):
        for _ in range(30):
            try:
                self.c = psycopg2.connect(host=os.getenv("DB_HOST", "postgres"), dbname=os.getenv("DB_NAME", "civicsense"),
                                          user=os.getenv("DB_USER", "civic"), password=os.getenv("DB_PASSWORD", ""))
                return
            except psycopg2.OperationalError:
                time.sleep(2)
        raise RuntimeError("database not reachable")
    def execute(self, sql, args=()):
        cur = self.c.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql.replace("?", "%s"), args)
        return cur
    def commit(self):
        self.c.commit()
    def __del__(self):
        try: self.c.close()
        except Exception: pass

def db():
    return Con()

def init_db():
    con = Con()
    con.execute("SELECT pg_advisory_xact_lock(42)")
    con.execute("""CREATE TABLE IF NOT EXISTS issues(
        id SERIAL PRIMARY KEY, title TEXT, description TEXT, location TEXT,
        category TEXT, priority TEXT, department TEXT, status TEXT DEFAULT 'open',
        progress INTEGER DEFAULT 0, created DOUBLE PRECISION)""")
    if con.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0:
        cats = ["Pothole"]*4 + ["Garbage Overflow"]*3 + ["Water Leakage"]*2 + ["Broken Streetlight"]*2 + ["Traffic Issue"]
        sts = ["in_progress"]*3 + ["open"] + ["resolved"]*8
        for i, (c, st) in enumerate(zip(cats, sts)):
            con.execute("INSERT INTO issues(title,description,location,category,priority,department,status,progress,created) VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"{c} - {LOCS[i]}", "Reported by citizen", LOCS[i] + ", Hyderabad", c, "medium",
                         DEPT[c], st, PROG[st], time.time() - (i % 6)*86400 - i*3600))
    con.commit()

init_db()

def fmt(r):
    d = dict(r)
    c = dt.datetime.fromtimestamp(d["created"])
    e = c + dt.timedelta(days=5)
    d["ticket"] = f"CSAI-{c:%Y-%m%d}-{d['id']:06d}"
    d["date"] = c.strftime("%b %d, %Y at %I:%M %p")
    d["eta"] = e.strftime("%b %d, %Y")
    d["days_left"] = max((e - dt.datetime.now()).days, 0)
    return d

@app.route("/")
def dashboard():
    con = db()
    n = lambda q, *a: con.execute(q, a).fetchone()[0]
    week = time.time() - 7*86400
    s = dict(total=n("SELECT COUNT(*) FROM issues"),
             resolved=n("SELECT COUNT(*) FROM issues WHERE status='resolved'"),
             progress=n("SELECT COUNT(*) FROM issues WHERE status='in_progress'"),
             new_week=n("SELECT COUNT(*) FROM issues WHERE created>=?", week))
    s["score"] = 510 + s["resolved"]*30
    return render_template("dashboard.html", s=s)

@app.route("/report")
def report():
    return render_template("report.html", cats=list(CATS) + ["Other"])

@app.route("/track")
@app.route("/track/<int:iid>")
def track(iid=None):
    con = db()
    if iid: r = con.execute("SELECT * FROM issues WHERE id=?", (iid,)).fetchone()
    else: r = con.execute("SELECT * FROM issues ORDER BY (status='in_progress') DESC, id ASC").fetchone()
    if not r: abort(404)
    similar = con.execute("SELECT COUNT(*) FROM issues WHERE category=? AND id!=?", (r["category"], r["id"])).fetchone()[0]
    return render_template("track.html", i=fmt(r), similar=similar)

@app.route("/complaints")
def complaints():
    rows = [fmt(r) for r in db().execute("SELECT * FROM issues ORDER BY id DESC")]
    return render_template("complaints.html", rows=rows, heading="My Complaints")

@app.route("/nearby")
def nearby():
    rows = [fmt(r) for r in db().execute("SELECT * FROM issues WHERE status!='resolved' ORDER BY id DESC")]
    return render_template("complaints.html", rows=rows, heading="Nearby Issues")

@app.route("/api/issues", methods=["GET"])
def list_issues():
    return jsonify([fmt(r) for r in db().execute("SELECT * FROM issues ORDER BY id DESC")])

@app.route("/api/issues", methods=["POST"])
def create_issue():
    d = request.get_json(force=True)
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify(error="Enter a title for the issue."), 400
    cat, pri, dept = analyse(title + " " + (d.get("description") or ""), d.get("category"))
    con = db()
    cur = con.execute("INSERT INTO issues(title,description,location,category,priority,department,created) VALUES(?,?,?,?,?,?,?) RETURNING id",
                      (title, d.get("description"), d.get("location") or "Hyderabad", cat, pri, dept, time.time()))
    con.commit()
    r = fmt(con.execute("SELECT * FROM issues WHERE id=?", (cur.fetchone()[0],)).fetchone())
    return jsonify(id=r["id"], ticket=r["ticket"], category=cat, priority=pri, department=dept), 201

@app.route("/api/issues/<int:iid>/status", methods=["POST"])
def set_status(iid):
    s = request.get_json(force=True).get("status")
    if s not in PROG: return jsonify(error="bad status"), 400
    con = db()
    con.execute("UPDATE issues SET status=?, progress=? WHERE id=?", (s, PROG[s], iid))
    con.commit()
    return jsonify(ok=True)

@app.route("/api/stats")
def stats():
    since = time.time() - int(request.args.get("days", 7))*86400
    con = db()
    g = lambda col: {r[0]: r[1] for r in con.execute(f"SELECT {col},COUNT(*) FROM issues WHERE created>=? GROUP BY {col}", (since,))}
    return jsonify(categories=g("category"), status=g("status"))

@app.route("/health")
def health():
    return jsonify(status="ok")

@app.route("/load")
def load():
    x = 0
    for i in range(3_000_000): x += i*i   # CPU burner for the HPA demo
    return jsonify(done=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
