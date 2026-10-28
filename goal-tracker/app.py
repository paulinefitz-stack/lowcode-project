"""Goal Tracker: proof of concept.

Flask plus the Python standard library only (sqlite3, hashlib, secrets, ...).
Run it with:  flask --app app run   (see README.md)
"""
import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
from datetime import date, timedelta
from email.utils import parseaddr
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for)

app = Flask(__name__)

_secret = os.environ.get("GOALTRACKER_SECRET_KEY")
if not _secret:
    raise RuntimeError(
        "GOALTRACKER_SECRET_KEY is not set. Create one with:\n"
        '  python -c "import secrets; print(secrets.token_hex(32))"\n'
        "then set it as an environment variable (see README.md)."
    )
os.makedirs(app.instance_path, exist_ok=True)
app.config.update(
    SECRET_KEY=_secret,
    DATABASE=os.environ.get("GOALTRACKER_DB")
    or os.path.join(app.instance_path, "goaltracker.db"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=64 * 1024,
)

METRIC_ROWS = 4
SHARE_LEVELS = ("private", "summary", "full")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
STATUS_LABELS = {"ahead": "Ahead", "on_track": "On track", "behind": "Behind",
                 "off_track": "Off track", "missed": "No entry",
                 "upcoming": "Not started", "in_progress": "In progress"}
OFF_TRACK_PCT = 0.25   # more than 25% short of plan is "off track" (red)


# ---------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(app.config["DATABASE"])
    with app.open_resource("schema.sql") as f:
        db.executescript(f.read().decode())
    db.close()


init_db()


# ---------------------------------------------- security: passwords + CSRF
def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    salt_hex, digest_hex = stored.split("$")
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(expected, sent):
            abort(400)


@app.before_request
def load_user():
    g.user = None
    uid = session.get("user_id")
    if uid:
        g.user = get_db().execute(
            "SELECT id, name, email FROM users WHERE id = ?", (uid,)).fetchone()


@app.context_processor
def template_globals():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf"], "current_user": g.get("user")}


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; form-action 'self'; "
        "frame-ancestors 'none'")
    return resp


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, title="Page not found",
                           message="That page doesn't exist, or you don't "
                                   "have access to it."), 404


@app.errorhandler(400)
def bad_request(_e):
    return render_template("error.html", code=400, title="Please try again",
                           message="Your session expired or the form was "
                                   "incomplete. Go back, reload the page and "
                                   "submit again."), 400


@app.errorhandler(413)
def too_large(_e):
    return render_template("error.html", code=413, title="Too much data",
                           message="That request was too large."), 413


# ------------------------------------------------------- template helpers
def fmt_num(value):
    if value is None:
        return "-"
    text = f"{round(value, 2):,.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


@app.template_filter("num")
def num_filter(value):
    return fmt_num(value)


@app.template_filter("signed")
def signed_filter(value):
    if value is None:
        return "-"
    if abs(value) < 0.005:
        return "0"
    return ("+" if value > 0 else "") + fmt_num(value)


@app.template_filter("fmtdate")
def fmtdate_filter(value):
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return f"{value.day} {value:%b %Y}"          # e.g. 28 Oct 2026


@app.template_filter("status_label")
def status_label(value):
    return STATUS_LABELS.get(value, value)


# -------------------------------------------------------------- validation
def word_count(text):
    return len(text.split())


def parse_date(raw):
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def parse_number(raw):
    try:
        value = float((raw or "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def validate_email(raw):
    email = (raw or "").strip().lower()
    if not email:
        return email, "Enter your email address."
    if (len(email) > 254 or not EMAIL_RE.match(email)
            or parseaddr(email)[1] != email):
        return email, "Enter a valid email address, like name@example.com."
    return email, None


def validate_goal(f):
    """Validate the new-goal form. Returns (clean_data, errors)."""
    e, today = {}, date.today()

    title = f.get("title", "").strip()
    if not title:
        e["title"] = "Enter a goal title."
    elif len(title) > 100 or word_count(title) > 50:
        e["title"] = "Keep the title to 100 characters or fewer."

    desc = f.get("description", "").strip()
    if not desc:
        e["description"] = "Describe what you want to achieve."
    elif word_count(desc) > 50:
        e["description"] = "Keep the description to 50 words or fewer."

    start = parse_date(f.get("start_date"))
    if start is None:
        e["start_date"] = "Enter a valid start date."
    elif start < today:
        e["start_date"] = "The start date can't be in the past."

    end = parse_date(f.get("end_date"))
    if end is None:
        e["end_date"] = "Enter a valid end date."
    elif start and end <= start:
        e["end_date"] = "The end date must be after the start date."
    elif start and end - start < timedelta(days=7):
        e["end_date"] = "The goal must run for at least one week."

    pct = parse_number(f.get("acceptance_pct") or "0")
    if pct is None or not 0 <= pct <= 50:
        e["acceptance_pct"] = "Enter a threshold from 0 to 50."

    share = f.get("share_level", "private")
    if share not in SHARE_LEVELS:
        e["share_level"] = "Choose who can see this goal."

    style = f.get("plan_style", "even")
    if style not in ("even", "ramp"):
        e["plan_style"] = "Choose how to spread the weekly targets."

    metrics = []
    for i in range(METRIC_ROWS):
        raw = {k: f.get(f"m{i}_{k}", "").strip()
               for k in ("name", "unit", "final", "start")}
        if not any(raw.values()):
            continue
        tag = f"Metric {i + 1}: "
        direction = f.get(f"m{i}_direction", "increase")
        agg = f.get(f"m{i}_aggregation", "sum")
        if not raw["name"]:
            e[f"m{i}_name"] = tag + "enter a name, like Distance."
        elif len(raw["name"]) > 60:
            e[f"m{i}_name"] = tag + "keep the name to 60 characters or fewer."
        if not raw["unit"]:
            e[f"m{i}_unit"] = tag + "enter a unit, like miles or kg."
        elif len(raw["unit"]) > 20:
            e[f"m{i}_unit"] = tag + "keep the unit to 20 characters or fewer."
        if direction not in ("increase", "decrease"):
            e[f"m{i}_direction"] = tag + "choose a direction."
        if agg not in ("sum", "latest"):
            e[f"m{i}_aggregation"] = tag + "choose how entries are combined."
        final = parse_number(raw["final"])
        if final is None or final <= 0:
            e[f"m{i}_final"] = tag + "enter a target greater than 0."
        start_value = 0.0
        if agg == "latest":
            start_value = parse_number(raw["start"])
            if start_value is None or start_value < 0:
                e[f"m{i}_start"] = tag + "enter your starting value."
            elif final and final > 0 and direction == "increase" \
                    and final <= start_value:
                e[f"m{i}_final"] = tag + "the target must be above your starting value."
            elif final and final > 0 and direction == "decrease" \
                    and final >= start_value:
                e[f"m{i}_final"] = tag + "the target must be below your starting value."
        elif direction == "decrease":
            e[f"m{i}_direction"] = tag + "a running total can only increase. " \
                "Use 'latest reading' for things like weight."
        metrics.append({"index": i, "name": raw["name"], "unit": raw["unit"],
                        "direction": direction, "aggregation": agg,
                        "start_value": start_value or 0.0,
                        "final_target": final})

    primary = f.get("primary", "0")
    if not metrics:
        e["metrics"] = "Add at least one metric."
    elif len(metrics) == 1:
        primary = str(metrics[0]["index"])
    elif primary not in {str(m["index"]) for m in metrics}:
        e["primary"] = "Choose one of your metrics as the primary metric."

    data = {"title": title, "description": desc, "start": start, "end": end,
            "pct": pct, "share": share, "metrics": metrics,
            "primary": primary, "style": style}
    return data, e


# --------------------------------------------------------------- planning
def week_description(metric, value):
    """Default description for a generated week (every week needs one)."""
    if metric["aggregation"] == "sum":
        return f"Log {fmt_num(value)} {metric['unit']} of {metric['name']}"
    return f"Reach {fmt_num(value)} {metric['unit']} for {metric['name']}"


def rebuild_rows(metric, targets, goal_start, new_end, new_final, today,
                 mode="even"):
    """Weekly rows for a new plan version.

    Weeks that have already finished keep their old targets. The remaining
    weeks are spread so the plan reaches `new_final` by `new_end`.
    Returns (rows, problem) where problem is None, "end" or "final".
    """
    locked = [t for t in targets
              if date.fromisoformat(t["week_start"]) + timedelta(days=6) < today]
    first = goal_start + timedelta(days=7 * len(locked))
    days = (new_end - first).days
    if days < 7:
        return None, "end"
    n = math.ceil(days / 7)
    keep = [(t["week_start"], t["target_value"], t["description"])
            for t in locked]
    if metric["aggregation"] == "sum":
        left = new_final - sum(t["target_value"] for t in locked)
        if left <= 0:
            return None, "final"
        weights = list(range(1, n + 1)) if mode == "ramp" else [1] * n
        new = [left * w / sum(weights) for w in weights]
    else:
        base = locked[-1]["target_value"] if locked else metric["start_value"]
        rising = metric["direction"] == "increase"
        if (rising and new_final <= base) or (not rising and new_final >= base):
            return None, "final"
        new = [base + (new_final - base) * (i + 1) / n for i in range(n)]
    rows = keep + [((first + timedelta(days=7 * i)).isoformat(), round(v, 4),
                    week_description(metric, round(v, 2)))
                   for i, v in enumerate(new)]
    return rows, None


def add_plan_version(db, metric_id, end_date, final, reason, rows):
    last = db.execute("SELECT COALESCE(MAX(version_no), 0) FROM plan_versions"
                      " WHERE metric_id = ?", (metric_id,)).fetchone()[0]
    cur = db.execute(
        "INSERT INTO plan_versions (metric_id, version_no, end_date,"
        " final_target, reason) VALUES (?, ?, ?, ?, ?)",
        (metric_id, last + 1, end_date.isoformat(), final, reason))
    db.executemany(
        "INSERT INTO weekly_targets (plan_version_id, week_start, target_value,"
        " description) VALUES (?, ?, ?, ?)",
        [(cur.lastrowid, ws, tv, d) for ws, tv, d in rows])
    db.execute("UPDATE metrics SET final_target = ? WHERE id = ?",
               (final, metric_id))


def create_goal(user_id, d):
    db = get_db()
    cur = db.execute(
        "INSERT INTO goals (user_id, title, description, start_date, end_date,"
        " acceptance_pct, share_level) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, d["title"], d["description"], d["start"].isoformat(),
         d["end"].isoformat(), d["pct"], d["share"]))
    goal_id = cur.lastrowid
    for m in d["metrics"]:
        mc = db.execute(
            "INSERT INTO metrics (goal_id, name, unit, direction, aggregation,"
            " start_value, final_target, is_primary) VALUES (?,?,?,?,?,?,?,?)",
            (goal_id, m["name"], m["unit"], m["direction"], m["aggregation"],
             m["start_value"], m["final_target"],
             int(str(m["index"]) == d["primary"])))
        rows, _ = rebuild_rows(m, [], d["start"], d["end"], m["final_target"],
                               date.today(), d["style"])
        add_plan_version(db, mc.lastrowid, d["end"], m["final_target"],
                         "Original plan", rows)
    db.commit()
    return goal_id


# ------------------------------------------------------------ progress maths
def judge(shortfall, tolerance, magnitude):
    """Rate progress against plan.

    shortfall > 0 means behind plan. tolerance is the user's own threshold.
    magnitude is the planned amount the shortfall is measured against.
    ahead / on_track are green, behind (up to 25% short) is amber and
    off_track (more than 25% short) is red.
    """
    shortfall, tolerance = round(shortfall, 6), round(tolerance, 6)
    if shortfall < -tolerance:
        return "ahead"
    if shortfall <= tolerance:
        return "on_track"
    if shortfall <= round(OFF_TRACK_PCT * magnitude, 6):
        return "behind"
    return "off_track"


def load_plan(db, metric_id, latest=True):
    order = "DESC" if latest else "ASC"
    plan = db.execute(
        "SELECT * FROM plan_versions WHERE metric_id = ?"
        f" ORDER BY version_no {order} LIMIT 1", (metric_id,)).fetchone()
    targets = db.execute(
        "SELECT id, week_start, target_value, description FROM weekly_targets"
        " WHERE plan_version_id = ? ORDER BY week_start",
        (plan["id"],)).fetchall()
    return plan, targets


def cumulative_targets(metric, targets):
    series = [metric["start_value"]]
    for t in targets:
        series.append(series[-1] + t["target_value"]
                      if metric["aggregation"] == "sum" else t["target_value"])
    return series


def delta_text(weeks_delta):
    if weeks_delta > 0.05:
        n = round(weeks_delta, 1)
        return f"{fmt_num(n)} {'week' if n == 1 else 'weeks'} ahead of plan"
    if weeks_delta < -0.05:
        n = round(-weeks_delta, 1)
        return f"{fmt_num(n)} {'week' if n == 1 else 'weeks'} behind plan"
    return "Right on plan"


def metric_report(goal, metric, today):
    db = get_db()
    start = date.fromisoformat(goal["start_date"])
    plan, targets = load_plan(db, metric["id"])
    entries = [(date.fromisoformat(r["entry_date"]), r["value"]) for r in
               db.execute("SELECT entry_date, value FROM entries"
                          " WHERE metric_id = ? ORDER BY entry_date",
                          (metric["id"],))]
    tol = goal["acceptance_pct"] / 100
    is_sum = metric["aggregation"] == "sum"
    sign = 1 if metric["direction"] == "increase" else -1
    base = metric["start_value"] if not is_sum else 0.0
    final = plan["final_target"]
    not_started = today < start

    weeks, cum_t, cum_a = [], base, base
    for i, t in enumerate(targets):
        ws = date.fromisoformat(t["week_start"])
        we = ws + timedelta(days=6)
        vals = [v for d, v in entries if ws <= d <= we]
        actual = None if not vals else (sum(vals) if is_sum else vals[-1])
        target = t["target_value"]
        if is_sum:
            cum_t += target
            cum_a += actual or 0
        variance = None if actual is None else round(sign * (actual - target), 4)
        if ws > today:
            cum_var = None
        elif is_sum:
            cum_var = round(sign * (cum_a - cum_t), 4)
        else:
            cum_var = variance
        if actual is not None:
            mag = target if is_sum else abs(target - base)
            status = judge(sign * (target - actual), tol * mag, mag)
            if status in ("behind", "off_track") and we >= today:
                status = "in_progress"       # week not over yet
        elif we < today:
            status = "missed"
        else:
            status = "upcoming"
        weeks.append({"n": i + 1, "start": ws, "end": we, "target": target,
                      "description": t["description"], "actual": actual,
                      "variance": variance, "cum_variance": cum_var,
                      "status": status})

    done = max(0, min(len(weeks), (today - start).days // 7))   # finished weeks
    if is_sum:
        actual_now = sum(v for _, v in entries)
        expected = sum(w["target"] for w in weeks[:done])
        mag = expected
        progress = actual_now / final if final else 0
    else:
        actual_now = entries[-1][1] if entries else base
        expected = weeks[done - 1]["target"] if done else base
        mag = abs(expected - base)
        progress = (actual_now - base) / (final - base) if final != base else 0
    status = "upcoming" if not_started else judge(
        sign * (expected - actual_now), tol * mag, mag)
    per_week = abs(final - base) / len(weeks) if weeks else 0
    weeks_delta = (sign * (actual_now - expected) / per_week) if per_week else 0

    streak = 0
    for w in reversed(weeks):
        if w["start"] > today:
            continue
        if w["status"] == "in_progress" or (
                w["actual"] is None and today <= w["end"]):
            continue                       # this week isn't over yet
        if w["status"] in ("ahead", "on_track"):
            streak += 1
        else:
            break

    next_week = next((w for w in weeks if w["end"] >= today), None)

    # Chart: cumulative target vs actual, plus the original plan if revised.
    tgt = cumulative_targets({"aggregation": metric["aggregation"],
                              "start_value": base}, targets)
    act, run, last = [base], base, base
    for w in weeks:
        if w["start"] > today:
            break
        if is_sum:
            run += w["actual"] or 0
            act.append(run)
        else:
            last = w["actual"] if w["actual"] is not None else last
            act.append(last)
    orig = None
    if plan["version_no"] > 1:
        orig = cumulative_targets({"aggregation": metric["aggregation"],
                                   "start_value": base},
                                  load_plan(db, metric["id"], False)[1])
    everything = tgt + act + (orig or [])
    lo, hi = min(everything), max(everything)
    if hi == lo:
        hi = lo + 1
    W, H, PL, PR, PT, PB = 600, 240, 52, 16, 16, 32
    n = max(len(tgt), len(orig or [])) - 1

    def xy(i, v):
        return (PL + i * (W - PL - PR) / n,
                H - PB - (v - lo) / (hi - lo) * (H - PT - PB))

    def pts(series):
        return [f"{x:.1f},{y:.1f}" for x, y in
                (xy(i, v) for i, v in enumerate(series))]

    lx, ly = xy(len(act) - 1, act[-1])
    chart = {"target": " ".join(pts(tgt)), "actual": " ".join(pts(act)),
             "original": " ".join(pts(orig)) if orig else "",
             "shade": " ".join(pts(act) + list(reversed(pts(tgt[:len(act)]))))
             if len(act) > 1 else "",
             "lo": lo, "hi": hi, "weeks": n, "left": PL, "right": W - PR,
             "top": PT, "bottom": H - PB, "last_x": round(lx, 1),
             "last_y": round(ly, 1), "has_actual": len(act) > 1}

    return {"metric": metric, "weeks": weeks, "actual": actual_now,
            "expected": expected, "final": final, "status": status,
            "streak": streak, "weeks_delta": weeks_delta,
            "delta_text": ("Starts " + fmtdate_filter(start)) if not_started
            else delta_text(weeks_delta),
            "version": plan["version_no"], "next_week": next_week,
            "progress": round(max(0, min(1, progress)) * 100),
            "chart": chart}


# ------------------------------------------------------------ data access
def own_goal(goal_id):
    db = get_db()
    goal = db.execute("SELECT * FROM goals WHERE id = ? AND user_id = ?",
                      (goal_id, g.user["id"])).fetchone()
    if goal is None:
        abort(404)
    metrics = db.execute(
        "SELECT * FROM metrics WHERE goal_id = ? ORDER BY is_primary DESC, id",
        (goal_id,)).fetchall()
    return goal, metrics


def are_friends(a, b):
    return get_db().execute(
        "SELECT 1 FROM friendships WHERE status = 'accepted' AND"
        " ((requester_id = ? AND addressee_id = ?) OR"
        "  (requester_id = ? AND addressee_id = ?))", (a, b, b, a)
    ).fetchone() is not None


def goal_page(goal, owner, form=None, errors=None):
    """Render a goal. The owner sees everything; friends see what is shared."""
    db, today = get_db(), date.today()
    metrics = db.execute(
        "SELECT * FROM metrics WHERE goal_id = ? ORDER BY is_primary DESC, id",
        (goal["id"],)).fetchall()
    level = "full" if owner else goal["share_level"]
    reports = [metric_report(goal, m, today) for m in metrics]
    if level == "summary":
        reports = reports[:1]                       # primary metric only
    comments = db.execute(
        "SELECT c.body, c.created_at, u.name FROM comments c"
        " JOIN users u ON u.id = c.author_id WHERE c.goal_id = ?"
        " ORDER BY c.id DESC", (goal["id"],)).fetchall()
    owner_name = db.execute("SELECT name FROM users WHERE id = ?",
                            (goal["user_id"],)).fetchone()["name"]
    html = render_template(
        "goal_detail.html", goal=goal, reports=reports, owner=owner,
        level=level, comments=comments, owner_name=owner_name,
        form=form or {}, errors=errors or {})
    return html, (400 if errors else 200)


def add_comment(goal, owner):
    body = request.form.get("body", "").strip()
    errors = {}
    if not body:
        errors["body"] = "Write a comment first."
    elif len(body) > 500:
        errors["body"] = "Keep comments to 500 characters or fewer."
    if errors:
        return goal_page(goal, owner, request.form, errors)
    db = get_db()
    db.execute("INSERT INTO comments (goal_id, author_id, body) VALUES (?,?,?)",
               (goal["id"], g.user["id"], body))
    db.commit()
    flash("Comment added.")
    return redirect(url_for("goal_detail", goal_id=goal["id"]) if owner
                    else url_for("friend_goal", goal_id=goal["id"]))


# ------------------------------------------------------------------- routes
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", form={}, errors={})
    f, errors = request.form, {}
    name = f.get("name", "").strip()
    email, email_error = validate_email(f.get("email", ""))
    password = f.get("password", "")
    if not name:
        errors["name"] = "Enter your name."
    elif len(name) > 100:
        errors["name"] = "Keep your name to 100 characters or fewer."
    if email_error:
        errors["email"] = email_error
    if not password:
        errors["password"] = "Enter a password."
    elif not 10 <= len(password) <= 200:
        errors["password"] = "Use a password of 10 to 200 characters."
    if not errors:
        try:
            db = get_db()
            cur = db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?,?,?)",
                (name, email, hash_password(password)))
            db.commit()
        except sqlite3.IntegrityError:
            errors["email"] = "An account with this email already exists."
        else:
            session.clear()
            session["user_id"] = cur.lastrowid
            return redirect(url_for("dashboard"))
    return render_template("register.html", form=f, errors=errors), 400


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", form={}, errors={})
    f = request.form
    email = f.get("email", "").strip().lower()
    user = get_db().execute(
        "SELECT id, password_hash FROM users WHERE email = ?",
        (email,)).fetchone()
    if user and verify_password(f.get("password", ""), user["password_hash"]):
        session.clear()
        session["user_id"] = user["id"]
        return redirect(url_for("dashboard"))
    errors = {"form": "That email and password don't match an account."}
    return render_template("login.html", form=f, errors=errors), 400


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    db, today, rows = get_db(), date.today(), []
    counts = {"ahead": 0, "on_track": 0, "behind": 0, "off_track": 0,
              "upcoming": 0}
    for goal in db.execute("SELECT * FROM goals WHERE user_id = ?"
                           " ORDER BY end_date", (g.user["id"],)):
        primary = db.execute(
            "SELECT * FROM metrics WHERE goal_id = ? AND is_primary = 1",
            (goal["id"],)).fetchone()
        report = metric_report(goal, primary, today)
        counts[report["status"]] = counts.get(report["status"], 0) + 1
        rows.append({"goal": goal, "report": report})
    return render_template("dashboard.html", rows=rows, counts=counts)


@app.route("/goals/new", methods=["GET", "POST"])
@login_required
def goal_new():
    rows, today = range(METRIC_ROWS), date.today().isoformat()
    if request.method == "GET":
        return render_template("goal_new.html", form={}, errors={}, rows=rows,
                               today=today)
    data, errors = validate_goal(request.form)
    if errors:
        return render_template("goal_new.html", form=request.form,
                               errors=errors, rows=rows, today=today), 400
    return redirect(url_for("goal_detail",
                            goal_id=create_goal(g.user["id"], data)))


@app.route("/goals/<int:goal_id>", methods=["GET", "POST"])
@login_required
def goal_detail(goal_id):
    goal, _ = own_goal(goal_id)
    if request.method == "POST":
        return add_comment(goal, owner=True)
    return goal_page(goal, owner=True)


@app.route("/friends/goals/<int:goal_id>", methods=["GET", "POST"])
@login_required
def friend_goal(goal_id):
    goal = get_db().execute("SELECT * FROM goals WHERE id = ?",
                            (goal_id,)).fetchone()
    if goal is None:
        abort(404)
    if goal["user_id"] == g.user["id"]:
        return redirect(url_for("goal_detail", goal_id=goal_id))
    if goal["share_level"] == "private" or not are_friends(
            g.user["id"], goal["user_id"]):
        abort(404)                       # don't reveal that it exists
    if request.method == "POST":
        return add_comment(goal, owner=False)
    return goal_page(goal, owner=False)


@app.route("/goals/<int:goal_id>/log", methods=["GET", "POST"])
@login_required
def goal_log(goal_id):
    goal, metrics = own_goal(goal_id)
    db, today = get_db(), date.today()

    def render(form, errors, status=200):
        recent = db.execute(
            "SELECT e.entry_date, e.value, e.note, m.name, m.unit FROM entries e"
            " JOIN metrics m ON m.id = e.metric_id WHERE m.goal_id = ?"
            " ORDER BY e.entry_date DESC, m.id LIMIT 15", (goal_id,)).fetchall()
        return render_template("goal_log.html", goal=goal, metrics=metrics,
                               form=form, errors=errors, recent=recent,
                               today=today.isoformat()), status

    if request.method == "GET":
        return render({"entry_date": min(today.isoformat(), goal["end_date"])},
                      {})
    f, errors, values = request.form, {}, {}
    entry_date = parse_date(f.get("entry_date"))
    if entry_date is None:
        errors["entry_date"] = "Enter a valid date."
    elif not (date.fromisoformat(goal["start_date"]) <= entry_date
              <= date.fromisoformat(goal["end_date"])):
        errors["entry_date"] = (
            f"Pick a date between {fmtdate_filter(goal['start_date'])} and "
            f"{fmtdate_filter(goal['end_date'])}.")
    elif entry_date > today:
        errors["entry_date"] = "You can't log progress for a future date."
    for m in metrics:
        raw = f.get(f"value_{m['id']}", "").strip()
        if not raw:
            continue
        value = parse_number(raw)
        if value is None or value < 0:
            errors[f"value_{m['id']}"] = f"{m['name']}: enter a number, 0 or more."
        else:
            values[m["id"]] = value
    note = f.get("note", "").strip()
    if len(note) > 200:
        errors["note"] = "Keep the note to 200 characters or fewer."
    if not values and not errors:
        errors["form"] = "Enter a value for at least one metric."
    if errors:
        return render(f, errors, 400)
    for metric_id, value in values.items():
        db.execute(
            "INSERT INTO entries (metric_id, entry_date, value, note)"
            " VALUES (?,?,?,?) ON CONFLICT (metric_id, entry_date)"
            " DO UPDATE SET value = excluded.value, note = excluded.note",
            (metric_id, entry_date.isoformat(), value, note))
    db.commit()
    flash("Progress saved.")
    return redirect(url_for("goal_detail", goal_id=goal_id))


@app.route("/goals/<int:goal_id>/plan", methods=["GET", "POST"])
@login_required
def goal_plan(goal_id):
    goal, metrics = own_goal(goal_id)
    db, today = get_db(), date.today()
    goal_start = date.fromisoformat(goal["start_date"])
    goal_end = date.fromisoformat(goal["end_date"])
    plans = {m["id"]: load_plan(db, m["id"]) for m in metrics}

    def render(form, errors, status=200):
        blocks = []
        for m in metrics:
            plan, targets = plans[m["id"]]
            weeks = []
            for i, t in enumerate(targets):
                ws = date.fromisoformat(t["week_start"])
                weeks.append({
                    "n": i + 1, "start": ws, "end": ws + timedelta(days=6),
                    "tname": f"t_{m['id']}_{i}", "dname": f"d_{m['id']}_{i}",
                    "target": f"{t['target_value']:.4f}".rstrip("0").rstrip("."),
                    "desc": t["description"],
                    "past": ws + timedelta(days=6) < today})
            blocks.append({"metric": m, "plan": plan, "weeks": weeks})
        return render_template("goal_plan.html", goal=goal, blocks=blocks,
                               form=form, errors=errors), status

    if request.method == "GET":
        return render({}, {})

    f, action = request.form, request.form.get("action", "save")
    db_changes = []
    if action in ("even", "ramp"):
        errors = {}
        for m in metrics:
            plan, targets = plans[m["id"]]
            rows, problem = rebuild_rows(m, targets, goal_start, goal_end,
                                         plan["final_target"], today, action)
            if problem:
                errors["form"] = ("Every week of this goal has already "
                                  "finished, so there is nothing left to "
                                  "re-plan. Revise the goal to extend it.")
                break
            label = "evenly" if action == "even" else "with a ramp"
            db_changes.append((m["id"], plan["final_target"], rows,
                               f"Remaining weeks spread {label}"))
        if errors:
            return render(f, errors, 400)
    elif action == "save":
        errors = {}
        reason = f.get("reason", "").strip()
        if len(reason) > 200:
            errors["reason"] = "Keep the reason to 200 characters or fewer."
        for m in metrics:
            plan, targets = plans[m["id"]]
            rows, before = [], len(errors)
            for i, t in enumerate(targets):
                tname, dname = f"t_{m['id']}_{i}", f"d_{m['id']}_{i}"
                value = parse_number(f.get(tname))
                desc = f.get(dname, "").strip()
                if value is None or value <= 0:
                    errors[tname] = (f"{m['name']}, week {i + 1}: enter a "
                                     "target greater than 0.")
                if not desc:
                    errors[dname] = (f"{m['name']}, week {i + 1}: describe "
                                     "this week's goal.")
                elif word_count(desc) > 50:
                    errors[dname] = (f"{m['name']}, week {i + 1}: keep the "
                                     "description to 50 words or fewer.")
                rows.append((t["week_start"], value, desc))
            if len(errors) == before:
                final = (round(sum(r[1] for r in rows), 4)
                         if m["aggregation"] == "sum" else rows[-1][1])
                changed = any(
                    abs(r[1] - t["target_value"]) > 1e-9 or r[2] != t["description"]
                    for r, t in zip(rows, targets))
                if changed:
                    db_changes.append((m["id"], final, rows,
                                       reason or "Weekly targets edited"))
        if errors:
            return render(f, errors, 400)
        if not db_changes:
            flash("No changes to save.")
            return redirect(url_for("goal_plan", goal_id=goal_id))
    else:
        abort(400)

    for metric_id, final, rows, why in db_changes:
        add_plan_version(db, metric_id, goal_end, final, why, rows)
    db.commit()
    flash("Plan saved. The previous version is kept in the history.")
    return redirect(url_for("goal_detail", goal_id=goal_id))


@app.route("/goals/<int:goal_id>/revise", methods=["GET", "POST"])
@login_required
def goal_revise(goal_id):
    goal, metrics = own_goal(goal_id)
    db, today = get_db(), date.today()
    goal_start = date.fromisoformat(goal["start_date"])
    goal_end = date.fromisoformat(goal["end_date"])

    def render(form, errors, status=200):
        return render_template("goal_revise.html", goal=goal, metrics=metrics,
                               form=form, errors=errors), status

    if request.method == "GET":
        form = {"end_date": goal["end_date"]}
        for m in metrics:
            form[f"final_{m['id']}"] = fmt_num(m["final_target"]).replace(",", "")
        return render(form, {})

    f, errors = request.form, {}
    new_end = parse_date(f.get("end_date"))
    if new_end is None:
        errors["end_date"] = "Enter a valid end date."
    reason = f.get("reason", "").strip()
    if not reason:
        errors["reason"] = "Say why you are changing the goal."
    elif len(reason) > 200:
        errors["reason"] = "Keep the reason to 200 characters or fewer."
    finals = {}
    for m in metrics:
        key = f"final_{m['id']}"
        value = parse_number(f.get(key))
        if value is None or value <= 0:
            errors[key] = f"{m['name']}: enter a target greater than 0."
        else:
            finals[m["id"]] = value
    if errors:
        return render(f, errors, 400)

    end_changed = new_end != goal_end
    plans = {m["id"]: load_plan(db, m["id"]) for m in metrics}
    final_changed = {m["id"]: abs(finals[m["id"]]
                                  - plans[m["id"]][0]["final_target"]) > 1e-9
                     for m in metrics}
    if not end_changed and not any(final_changed.values()):
        errors["form"] = "Change the end date or a target before saving."
        return render(f, errors, 400)

    changes = []
    for m in metrics:
        if not (end_changed or final_changed[m["id"]]):
            continue
        plan, targets = plans[m["id"]]
        rows, problem = rebuild_rows(m, targets, goal_start, new_end,
                                     finals[m["id"]], today)
        if problem == "end":
            errors["end_date"] = ("Leave at least one week after the weeks "
                                  "that have already finished.")
        elif problem == "final":
            errors[f"final_{m['id']}"] = (
                f"{m['name']}: the target must be beyond what the finished "
                "weeks already planned.")
        else:
            changes.append((m["id"], finals[m["id"]], rows))
    if errors:
        return render(f, errors, 400)
    for metric_id, final, rows in changes:
        add_plan_version(db, metric_id, new_end, final, reason, rows)
    db.execute("UPDATE goals SET end_date = ? WHERE id = ?",
               (new_end.isoformat(), goal_id))
    db.commit()
    flash("Goal revised. The original plan is kept in the history.")
    return redirect(url_for("goal_history", goal_id=goal_id))


@app.route("/goals/<int:goal_id>/history")
@login_required
def goal_history(goal_id):
    goal, metrics = own_goal(goal_id)
    db, blocks = get_db(), []
    for m in metrics:
        versions = []
        for v in db.execute("SELECT * FROM plan_versions WHERE metric_id = ?"
                            " ORDER BY version_no", (m["id"],)):
            weeks = db.execute(
                "SELECT week_start, target_value FROM weekly_targets"
                " WHERE plan_version_id = ? ORDER BY week_start",
                (v["id"],)).fetchall()
            versions.append({"v": v, "weeks": weeks})
        blocks.append({"metric": m, "versions": versions})
    return render_template("goal_history.html", goal=goal, blocks=blocks)


@app.route("/goals/<int:goal_id>/share", methods=["GET", "POST"])
@login_required
def goal_share(goal_id):
    goal, _ = own_goal(goal_id)
    if request.method == "GET":
        return render_template("goal_share.html", goal=goal,
                               form={"share_level": goal["share_level"]},
                               errors={})
    level = request.form.get("share_level", "")
    if level not in SHARE_LEVELS:
        return render_template(
            "goal_share.html", goal=goal, form=request.form,
            errors={"share_level": "Choose who can see this goal."}), 400
    db = get_db()
    db.execute("UPDATE goals SET share_level = ? WHERE id = ?",
               (level, goal_id))
    db.commit()
    flash("Sharing updated. Friends see the change straight away.")
    return redirect(url_for("goal_detail", goal_id=goal_id))


# ------------------------------------------------------------------ friends
def friends_page(form=None, errors=None):
    db, me = get_db(), g.user["id"]
    rows = db.execute(
        "SELECT f.id, f.status, f.requester_id, u.id AS other_id,"
        " u.name AS other_name, u.email AS other_email FROM friendships f"
        " JOIN users u ON u.id = CASE WHEN f.requester_id = ?"
        "   THEN f.addressee_id ELSE f.requester_id END"
        " WHERE (f.requester_id = ? OR f.addressee_id = ?)"
        "   AND f.status IN ('pending', 'accepted') ORDER BY u.name",
        (me, me, me)).fetchall()
    friends, incoming, outgoing = [], [], []
    for r in rows:
        if r["status"] == "accepted":
            goals = db.execute(
                "SELECT id, title, share_level FROM goals WHERE user_id = ?"
                " AND share_level != 'private' ORDER BY end_date",
                (r["other_id"],)).fetchall()
            friends.append({"row": r, "goals": goals})
        elif r["requester_id"] == me:
            outgoing.append(r)
        else:
            incoming.append(r)
    html = render_template("friends.html", friends=friends, incoming=incoming,
                           outgoing=outgoing, form=form or {},
                           errors=errors or {})
    return html, (400 if errors else 200)


@app.route("/friends")
@login_required
def friends():
    return friends_page()


@app.route("/friends/invite", methods=["GET", "POST"])
@login_required
def friends_invite():
    if request.method == "GET":
        return redirect(url_for("friends"))
    db, me = get_db(), g.user["id"]
    email, error = validate_email(request.form.get("email", ""))
    if error:
        return friends_page(request.form, {"email": error})
    other = db.execute("SELECT id, name FROM users WHERE email = ?",
                       (email,)).fetchone()
    if other is None:
        return friends_page(request.form, {
            "email": "No account uses that email yet. Ask them to register "
                     "first, then invite them."})
    if other["id"] == me:
        return friends_page(request.form,
                            {"email": "You can't invite yourself."})
    existing = db.execute(
        "SELECT id, status FROM friendships WHERE"
        " (requester_id = ? AND addressee_id = ?) OR"
        " (requester_id = ? AND addressee_id = ?)",
        (me, other["id"], other["id"], me)).fetchone()
    if existing and existing["status"] in ("pending", "accepted"):
        msg = ("You are already connected." if existing["status"] == "accepted"
               else "There is already a pending invitation between you.")
        return friends_page(request.form, {"email": msg})
    if existing:                       # declined or revoked earlier: re-open
        db.execute("UPDATE friendships SET requester_id = ?, addressee_id = ?,"
                   " status = 'pending' WHERE id = ?",
                   (me, other["id"], existing["id"]))
    else:
        db.execute("INSERT INTO friendships (requester_id, addressee_id)"
                   " VALUES (?, ?)", (me, other["id"]))
    db.commit()
    flash(f"Invitation sent to {other['name']}.")
    return redirect(url_for("friends"))


@app.route("/friends/<int:fid>/respond", methods=["POST"])
@login_required
def friends_respond(fid):
    db = get_db()
    row = db.execute("SELECT * FROM friendships WHERE id = ? AND"
                     " addressee_id = ? AND status = 'pending'",
                     (fid, g.user["id"])).fetchone()
    if row is None:
        abort(404)
    action = request.form.get("action")
    if action not in ("accept", "decline"):
        abort(400)
    db.execute("UPDATE friendships SET status = ? WHERE id = ?",
               ("accepted" if action == "accept" else "declined", fid))
    db.commit()
    flash("You are now connected." if action == "accept"
          else "Invitation declined.")
    return redirect(url_for("friends"))


@app.route("/friends/<int:fid>/revoke", methods=["POST"])
@login_required
def friends_revoke(fid):
    db, me = get_db(), g.user["id"]
    row = db.execute(
        "SELECT * FROM friendships WHERE id = ? AND status IN"
        " ('pending', 'accepted') AND (requester_id = ? OR addressee_id = ?)",
        (fid, me, me)).fetchone()
    if row is None:
        abort(404)
    db.execute("UPDATE friendships SET status = 'revoked' WHERE id = ?", (fid,))
    db.commit()
    flash("Connection removed. They can no longer see your goals.")
    return redirect(url_for("friends"))


# ------------------------------------------------------------------ contact
@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "GET":
        return render_template("contact.html", form={}, errors={},
                               sent=request.args.get("sent") == "1")
    f, errors = request.form, {}
    name = f.get("name", "").strip()
    email, email_error = validate_email(f.get("email", ""))
    message = f.get("message", "").strip()
    if not name:
        errors["name"] = "Enter your name."
    elif len(name) > 100:
        errors["name"] = "Keep your name to 100 characters or fewer."
    if email_error:
        errors["email"] = email_error
    if not message:
        errors["message"] = "Enter a message."
    elif word_count(message) > 200:
        errors["message"] = "Keep the message to 200 words or fewer."
    if errors:
        return render_template("contact.html", form=f, errors=errors,
                               sent=False), 400
    db = get_db()
    db.execute("INSERT INTO feedback (name, email, message) VALUES (?,?,?)",
               (name, email, message))
    db.commit()
    return redirect(url_for("contact", sent=1))
