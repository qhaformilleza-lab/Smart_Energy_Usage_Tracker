"""
Smart Energy Tracker — Flask API
Run: python app.py
Endpoints:
  GET    /api/entries              list all entries (optional ?type=electricity|water&days=N)
  POST   /api/entries              add entry          { date, type, amount, note }
  DELETE /api/entries/<id>         delete entry
  GET    /api/dashboard            summary stats + last-7-day chart data
  GET    /api/trends?view=weekly|monthly   grouped trend data
  GET    /api/bill?period=7|30|all&elec_rate=&water_rate=&fixed=   bill estimate
"""

from flask import session, redirect
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from database import init_db, get_db, close_db
from datetime import date, timedelta
import traceback

app = Flask(__name__)
app.secret_key = "secret123"
CORS(app)          # allows the HTML file to call the API when opened directly in a browser


# ── AUTH ─────────────────────────────────────────────

@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "Username and password are required."}), 400

    db = get_db()
    try:
        db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
        db.commit()
        session["user"] = username
        return jsonify({"username": username, "message": "Account created and logged in."})
    except Exception:
        return jsonify({"error": "Username already exists."}), 400


@app.route("/api/users", methods=["GET"])
def list_users():
    db = get_db()
    rows = db.execute("SELECT username FROM users ORDER BY username").fetchall()
    return jsonify([r["username"] for r in rows])


@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "Username and password are required."}), 400

    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE username=?",
        (username,)
    ).fetchone()

    if not user:
        return jsonify({"error": "Account not found. Please sign up first."}), 404
    if user["password"] != password:
        return jsonify({"error": "Invalid password."}), 401

    session["user"] = username
    return jsonify({"username": username})


def get_current_user_id():
    username = session.get("user")
    if not username:
        return None
    db = get_db()
    row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    return row["id"] if row else None


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ── Init ──────────────────────────────────────────────────────────────────────

@app.before_request
def _ensure_db():
    pass   # DB is initialised at startup; nothing extra needed per-request

@app.teardown_appcontext
def teardown_db(exception=None):
    close_db()

# ── Entries ───────────────────────────────────────────────────────────────────

@app.route("/api/entries", methods=["GET"])
def list_entries():
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "Not authenticated"}), 401

    type_filter = request.args.get("type")        # electricity | water | None
    days_filter = request.args.get("days", type=int)   # integer number of days

    db = get_db()
    query = "SELECT * FROM entries WHERE user_id = ?"
    params = [user_id]
    conditions = []

    if type_filter:
        conditions.append("type = ?")
        params.append(type_filter)
    if days_filter:
        cutoff = (date.today() - timedelta(days=days_filter)).isoformat()
        conditions.append("date >= ?")
        params.append(cutoff)

    if conditions:
        query += " AND " + " AND ".join(conditions)
    query += " ORDER BY date DESC, id DESC"

    rows = db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/entries", methods=["POST"])
def add_entry():
    body = request.get_json(force=True)
    entry_date = body.get("date")
    entry_type = body.get("type")
    amount     = body.get("amount")
    note       = body.get("note", "")

    # Basic validation
    if not entry_date or entry_type not in ("electricity", "water"):
        return jsonify({"error": "Invalid date or type"}), 400
    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Amount must be a positive number"}), 400

    db = get_db()
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "Not authenticated"}), 401

    cur = db.execute(
        "INSERT INTO entries (user_id, date, type, amount, note) VALUES (?, ?, ?, ?, ?)",
        (user_id, entry_date, entry_type, amount, note)
    )
    db.commit()

    new_row = db.execute("SELECT * FROM entries WHERE id = ? AND user_id = ?", (cur.lastrowid, user_id)).fetchone()
    return jsonify(dict(new_row)), 201


@app.route("/api/entries/<int:entry_id>", methods=["DELETE"])
def delete_entry(entry_id):
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "Not authenticated"}), 401

    db = get_db()
    row = db.execute("SELECT id FROM entries WHERE id = ? AND user_id = ?", (entry_id, user_id)).fetchone()
    if row is None:
        return jsonify({"error": "Entry not found"}), 404
    db.execute("DELETE FROM entries WHERE id = ? AND user_id = ?", (entry_id, user_id))
    db.commit()
    return jsonify({"deleted": entry_id})


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "Not authenticated"}), 401

    db   = get_db()
    today = date.today()

    def cutoff(days):
        return (today - timedelta(days=days)).isoformat()

    def sum_type(type_, days=None):
        if days:
            rows = db.execute(
                "SELECT COALESCE(SUM(amount),0) as s FROM entries WHERE user_id = ? AND type=? AND date>=?",
                (user_id, type_, cutoff(days))
            ).fetchone()
        else:
            rows = db.execute(
                "SELECT COALESCE(SUM(amount),0) as s FROM entries WHERE user_id = ? AND type=?",
                (user_id, type_)
            ).fetchone()
        return rows["s"]

    def count_days(type_, days):
        rows = db.execute(
            "SELECT COUNT(DISTINCT date) as c FROM entries WHERE user_id = ? AND type=? AND date>=?",
            (user_id, type_, cutoff(days))
        ).fetchone()
        return rows["c"] or 1  # avoid division by zero

    elec_7  = sum_type("electricity", 7)
    water_7 = sum_type("water", 7)
    elec_30 = sum_type("electricity", 30)
    water_30= sum_type("water", 30)
    total_entries = db.execute("SELECT COUNT(*) as c FROM entries WHERE user_id = ?", (user_id,)).fetchone()["c"]

    avg_elec  = round(elec_7  / count_days("electricity", 7),  1)
    avg_water = round(water_7 / count_days("water",        7),  0)
    est_bill  = round(elec_30 * 11.80 + water_30 * 0.035 + 150, 2)

    # Last-7-days per-day breakdown for bar charts
    daily_elec  = {}
    daily_water = {}
    for i in range(6, -1, -1):
        ds = (today - timedelta(days=i)).isoformat()
        daily_elec[ds]  = 0.0
        daily_water[ds] = 0.0

    rows = db.execute(
        "SELECT date, type, SUM(amount) as total FROM entries WHERE user_id = ? AND date >= ? GROUP BY date, type",
        (user_id, cutoff(7),)
    ).fetchall()
    for r in rows:
        if r["date"] in daily_elec:
            if r["type"] == "electricity":
                daily_elec[r["date"]]  = round(r["total"], 1)
            else:
                daily_water[r["date"]] = round(r["total"], 0)

    days_sorted = sorted(daily_elec.keys())
    return jsonify({
        "summary": {
            "avg_daily_electricity": avg_elec,
            "avg_daily_water":       int(avg_water),
            "est_monthly_bill":      est_bill,
            "total_entries":         total_entries,
        },
        "chart": {
            "labels":      [d for d in days_sorted],
            "electricity": [daily_elec[d]  for d in days_sorted],
            "water":       [daily_water[d] for d in days_sorted],
        }
    })


# ── Trends ────────────────────────────────────────────────────────────────────

@app.route("/api/trends", methods=["GET"])
def trends():
    view = request.args.get("view", "weekly")   # daily | weekly | monthly
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "Not authenticated"}), 401

    db   = get_db()

    if view == "monthly":
        # Group by YYYY-MM
        rows = db.execute(
            "SELECT strftime('%Y-%m', date) as key, type, SUM(amount) as total "
            "FROM entries WHERE user_id = ? GROUP BY key, type ORDER BY key",
            (user_id,)
        ).fetchall()
    elif view == "daily":
        rows = db.execute(
            "SELECT date as key, type, SUM(amount) as total "
            "FROM entries WHERE user_id = ? GROUP BY key, type ORDER BY key",
            (user_id,)
        ).fetchall()
    else:
        # Group by ISO week start (Monday)
        rows = db.execute(
            """
            SELECT
              date(date, 'weekday 1', '-7 days') as key,
              type,
              SUM(amount) as total
            FROM entries
            WHERE user_id = ?
            GROUP BY key, type
            ORDER BY key
            """,
            (user_id,),
        ).fetchall()

    # Pivot into {key: {electricity:x, water:y}}
    map_ = {}
    for r in rows:
        k = r["key"]
        if k not in map_:
            map_[k] = {"electricity": 0.0, "water": 0.0}
        map_[k][r["type"]] = round(r["total"], 1)

    keys = sorted(map_.keys())[-12:]   # last 12 periods
    return jsonify({
        "labels":      keys,
        "electricity": [map_[k]["electricity"] for k in keys],
        "water":       [map_[k]["water"]       for k in keys],
    })


# ── Bill estimator ────────────────────────────────────────────────────────────

@app.route("/api/bill", methods=["GET"])
def bill():
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "Not authenticated"}), 401

    elec_rate = float(request.args.get("elec_rate", 11.80))
    water_rate= float(request.args.get("water_rate", 0.035))
    fixed     = float(request.args.get("fixed", 150))
    period    = request.args.get("period", "30")  # "7", "30", or "all"

    db = get_db()

    if period == "all":
        where, params = "", []
    else:
        cutoff = (date.today() - timedelta(days=int(period))).isoformat()
        where  = "AND date >= ?"
        params = [cutoff]

    def total(type_):
        query = f"SELECT COALESCE(SUM(amount),0) as s FROM entries WHERE user_id = ? AND type=? {where}"
        row = db.execute(
            query,
            [user_id, type_] + params
        ).fetchone()
        return row["s"]

    total_elec  = total("electricity")
    total_water = total("water")
    cost_elec   = round(total_elec  * elec_rate,  2)
    cost_water  = round(total_water * water_rate, 2)
    total_bill  = round(cost_elec + cost_water + fixed, 2)

    return jsonify({
        "total":       total_bill,
        "electricity": {"kwh": round(total_elec, 1),  "cost": cost_elec},
        "water":       {"liters": round(total_water, 0), "cost": cost_water},
        "fixed":       fixed,
    })


# ── Error handler ─────────────────────────────────────────────────────────────

@app.errorhandler(Exception)
def handle_error(e):
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500

@app.route("/")
def home():
    if "user" not in session:
        return redirect("/login")
    return render_template("index.html")

@app.route("/favicon.ico")
def favicon():
    return "", 204
# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with app.app_context():
        init_db()
    print("DB initialised. Starting server on http://localhost:5000")
    app.run(debug=True, port=5000)
