import os
import sys
import secrets
import datetime as dt
from functools import wraps
from pathlib import Path
from flask import Flask, request, session, redirect, url_for, render_template, jsonify, flash
from werkzeug.security import check_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rules as R  # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
ROLES = ["pharmacy", "dealer", "cf", "manufacturer"]
ROLE_TITLE = {"pharmacy": "Pharmacy", "dealer": "Dealer", "cf": "C&F agent", "manufacturer": "Manufacturer"}
NO_TOKEN = ({"error": "Approvals need a logged-in person"}, 403)


@app.template_filter("rs")
def rs(v):
    return f"Rs {float(v or 0):,.0f}"


@app.template_filter("num")
def num(v):
    return f"{v:,.0f}" if isinstance(v, (int, float)) else v


@app.template_filter("spark")
def spark(vals):
    bars, hi = "▁▂▃▄▅▆▇█", max(vals) if vals else 0
    return "".join(bars[min(7, int(v / hi * 7.99))] if hi else bars[0] for v in vals)


def current_user(db):
    uid = session.get("uid")
    if not uid:
        return None
    return R.one(db, "select u.id,u.username,u.org_id,o.name org,o.type from users u join orgs o on o.id=u.org_id where u.id=?", uid)


def person_only(f):
    """Human session required. A Bearer token never approves."""
    @wraps(f)
    def w(*a, **k):
        db = R.conn()
        u = current_user(db)
        if not u:
            if request.headers.get("Authorization"):
                return NO_TOKEN
            return redirect(url_for("login"))
        return f(db, u, *a, **k)
    return w


def agent_auth(db, scope=None):
    h = request.headers.get("Authorization", "")
    t = R.one(db, "select * from agent_tokens where token=?", h[7:]) if h.startswith("Bearer ") else None
    if not t:
        return None, ({"error": "Missing or invalid token"}, 401)
    if scope and t["scope"] != scope:
        return None, ({"error": f"Token '{t['name']}' may not use this endpoint"}, 403)
    return t, None


def layout(db, u):
    return {"user": u, "as_of": R.as_of(db).isoformat(), "mode": R.setting(db, "approval_mode"),
            "limit": float(R.setting(db, "max_order_value")), "roles": ROLES, "titles": ROLE_TITLE}


@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    db = R.conn()
    if request.method == "POST":
        u = R.one(db, "select * from users where username=?", request.form.get("username", "").strip())
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session["uid"] = u["id"]
            R.audit(db, u["username"], "human", "login", "logged in", [u["org_id"]])
            db.commit()
            return redirect(url_for("home"))
        flash("Wrong username or password")
    accounts = R.q(db, "select u.username, o.name org, o.type from users u join orgs o on o.id=u.org_id order by o.type, o.name")
    return render_template("login.html", accounts=accounts, titles=ROLE_TITLE, user=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/home")
@person_only
def home(db, u):
    return redirect("/" + u["type"])


def role_page(role):
    @person_only
    def view(db, u):
        if u["type"] != role:
            return redirect("/" + u["type"])
        data = R.SUMMARY[role](db, u["org_id"])
        ctx = layout(db, u)
        return render_template(f"{role}.html", d=data, act=R.activity(db, u["org_id"]), **ctx)
    view.__name__ = f"page_{role}"
    return view


for _r in ROLES:
    app.add_url_rule("/" + _r, view_func=role_page(_r))


def _decide(db, u, table, row_id, approve):
    if table == "orders":
        o = R.one(db, "select * from orders where id=?", row_id)
        owner = o and o["from_org_id"]
    else:
        o = R.one(db, "select * from production_plans where id=?", row_id)
        owner = o and o["manufacturer_org_id"]
    if not o or owner != u["org_id"] or o["status"] != "draft":
        return "Not allowed: only a draft of your own organisation can be decided", 403
    status = "approved" if approve else "rejected"
    if table == "orders":
        db.execute("update orders set status=?, approved_by=?, updated_at=? where id=?",
                   (status, u["username"], dt.datetime.now().isoformat(timespec="seconds"), row_id))
    else:
        db.execute("update production_plans set status=?, approved_by=? where id=?", (status, u["username"], row_id))
    R.audit(db, u["username"], "human", "approve" if approve else "reject", f"{table[:-1]} #{row_id} {status}", [u["org_id"]])
    db.commit()
    return None, None


def decide_route(table, approve):
    @person_only
    def view(db, u, rid):
        err, code = _decide(db, u, table, rid, approve)
        if err:
            return err, code
        return redirect(request.referrer or "/home")
    view.__name__ = f"{table}_{'approve' if approve else 'reject'}"
    return view


for _t, _p in (("orders", "orders"), ("plans", "plans")):
    app.add_url_rule(f"/{_p}/<int:rid>/approve", view_func=decide_route(_t, True), methods=["POST"])
    app.add_url_rule(f"/{_p}/<int:rid>/reject", view_func=decide_route(_t, False), methods=["POST"])


@app.route("/orders/manual", methods=["POST"])
@person_only
def manual_order(db, u):
    if u["type"] != "pharmacy":
        return "Only pharmacy users place manual orders", 403
    med = R.one(db, "select id from medicines where name=?", request.form.get("medicine", ""))
    try:
        qty = int(request.form.get("qty", "0"))
        dealer = R.preferred(db, u["org_id"], med["id"])
        R.create_order(db, u["org_id"], dealer["id"], med["id"], qty, f"Manual order placed by {u['username']}",
                       "manual", None, u["username"], "human")
        db.commit()
    except (TypeError, ValueError) as e:
        flash(f"Order not created: {e}")
    return redirect("/pharmacy")


@app.route("/settings/approval_mode", methods=["POST"])
@person_only
def set_mode(db, u):
    mode = request.form.get("mode")
    if mode in ("threshold", "all"):
        db.execute("update settings set value=? where key='approval_mode'", (mode,))
        R.audit(db, u["username"], "human", "setting", f"approval_mode = {mode}", [u["org_id"]])
        db.commit()
    return redirect(request.referrer or "/home")


# ------------------------------------------------------------------ agent API
def who(t, j):
    """Actor name for the audit log: the sub-agent that did the work, via the coordinator's token."""
    a = (j or {}).get("agent")
    return f"{a} (via {t['name']})" if a else t["name"]


def log_api(db, t, action, detail, orgs=()):
    R.audit(db, t["name"], "agent", action, detail, list(orgs))


@app.route("/api/<role>/summary")
def api_summary(role):
    db = R.conn()
    t, err = agent_auth(db, role if role in ROLES else "none")
    if err:
        return err
    org = R.one(db, "select * from orgs where name=? and type=?", request.args.get("org", ""), role)
    if not org:
        return {"error": "Unknown org for this role. Pass ?org=<name>"}, 404
    data = R.SUMMARY[role](db, org["id"])
    log_api(db, t, "read_summary", f"read {role} summary for {org['name']}", [org["id"]])
    db.commit()
    return jsonify(data)


@app.route("/api/orgs")
def api_orgs():
    db = R.conn()
    t, err = agent_auth(db, "coordinator")
    if err:
        return err
    return jsonify([r["name"] for r in R.q(db, "select name from orgs where type=? order by name", request.args.get("type", ""))])


@app.route("/api/orders", methods=["GET", "POST"])
def api_orders():
    db = R.conn()
    t, err = agent_auth(db, "coordinator")
    if err:
        return err
    if request.method == "GET":
        status, level = request.args.get("status"), request.args.get("level")
        w, a = ["1=1"], []
        if status == "approved":
            w.append("o.status in ('approved','auto_approved')")
        elif status:
            w.append("o.status=?")
            a.append(status)
        if level:
            w.append("f.type=?")
            a.append(level)
        rows = R.order_rows(db, " and ".join(w), *a)
        rows.sort(key=lambda r: (r["from_org"], r["medicine"]) if level == "pharmacy" else (r["id"],))
        return jsonify(rows)
    j = request.get_json(force=True)
    try:
        ids = {k: R.one(db, f"select id from {tb} where name=?", j[k]) for k, tb in
               (("from_org", "orgs"), ("to_org", "orgs"), ("medicine", "medicines"))}
        if not all(ids.values()):
            return {"error": "Unknown from_org, to_org or medicine"}, 400
        o = R.create_order(db, ids["from_org"]["id"], ids["to_org"]["id"], ids["medicine"]["id"], int(j["qty"]),
                           j.get("reason", ""), j.get("source", "agent"), j.get("parent_order_id"), who(t, j))
    except (KeyError, ValueError) as e:
        return {"error": str(e)}, 400
    db.commit()
    return jsonify(o), 201


@app.route("/api/orders/<int:oid>/result", methods=["POST"])
def api_result(oid):
    db = R.conn()
    t, err = agent_auth(db, "coordinator")
    if err:
        return err
    j = request.get_json(force=True)
    try:
        o = R.record_result(db, oid, int(j["filled_qty"]), int(j["short_qty"]), j.get("note"), who(t, j))
    except (KeyError, ValueError) as e:
        return {"error": str(e)}, 400
    db.commit()
    return jsonify(o)


@app.route("/api/plans", methods=["POST"])
def api_plans():
    db = R.conn()
    t, err = agent_auth(db, "coordinator")
    if err:
        return err
    j = request.get_json(force=True)
    try:
        mfr = R.one(db, "select id from orgs where name=? and type='manufacturer'", j["manufacturer"])
        med = R.one(db, "select id from medicines where name=?", j["medicine"])
        p = R.create_plan(db, mfr["id"], med["id"], int(j["qty"]), j["ready_date"], j.get("reason", ""),
                          j.get("source_order_id"), who(t, j))
    except (KeyError, TypeError, ValueError) as e:
        return {"error": f"bad plan: {e}"}, 400
    db.commit()
    return jsonify(p), 201


if __name__ == "__main__":
    share = "--share" in sys.argv
    host = "0.0.0.0" if share else "127.0.0.1"
    if share:
        print("SHARING ON: devices on this Wi-Fi can open the app. Press Ctrl+C when the demo is done.")
    app.run(host=host, port=5000, debug=False)
