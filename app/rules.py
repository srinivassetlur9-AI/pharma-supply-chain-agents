"""All maths and policy for the demo. Agents use these numbers; they never calculate their own."""
import math
import sqlite3
import datetime as dt
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "app.db"
SUPPLIER_TYPE = {"pharmacy": "dealer", "dealer": "cf", "cf": "manufacturer"}
OPEN = ("draft", "approved", "auto_approved")
LIVE = ("approved", "auto_approved")


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def q(db, sql, *a):
    return [dict(r) for r in db.execute(sql, a)]


def one(db, sql, *a):
    r = db.execute(sql, a).fetchone()
    return dict(r) if r else None


def setting(db, key):
    r = db.execute("select value from settings where key=?", (key,)).fetchone()
    return r[0] if r else None


def as_of(db):
    return dt.date.fromisoformat(setting(db, "as_of"))


def ceil_to(x, n):
    return int(math.ceil(x / n) * n) if x > 0 else 0


def audit(db, actor, actor_type, action, detail, orgs=()):
    ts = dt.datetime.now().isoformat(timespec="seconds")
    for o in (list(orgs) or [None]):
        db.execute("insert into audit_log(ts,actor,actor_type,action,detail,org_id) values(?,?,?,?,?,?)",
                   (ts, actor, actor_type, action, detail, o))


def activity(db, org_id):
    return q(db, "select ts,actor,actor_type,action,detail from audit_log where org_id=? order by rowid desc limit 10", org_id)


def preferred(db, buyer_id, med_id):
    r = one(db, "select o.id,o.name from preferred_supplier p join orgs o on o.id=p.supplier_org_id "
                "where p.buyer_org_id=? and p.medicine_id=?", buyer_id, med_id)
    return r


def price_for(db, to_org_id, med_id):
    t = one(db, "select type from orgs where id=?", to_org_id)["type"]
    if t == "manufacturer":
        r = one(db, "select unit_cost from capacity where manufacturer_org_id=? and medicine_id=?", to_org_id, med_id)
        if r:
            return r["unit_cost"]
    return one(db, "select reference_price from medicines where id=?", med_id)["reference_price"]


def lead_days(db, cf_id, mfr_id, med_id):
    r = one(db, "select avg_lead_days a from lead_times where cf_org_id=? and manufacturer_org_id=? and medicine_id=?",
            cf_id, mfr_id, med_id)
    if r:
        return r["a"]
    r = one(db, "select avg(avg_lead_days) a from lead_times where manufacturer_org_id=? and medicine_id=?", mfr_id, med_id)
    return r["a"] if r and r["a"] else 7.0


def needs_person(db, value):
    return not (setting(db, "approval_mode") == "threshold" and value <= float(setting(db, "max_order_value")))


def create_order(db, from_org, to_org, med, qty, reason, source, parent, actor, actor_type="agent"):
    """from_org/to_org/med are ids. Applies the approval policy. Returns the new order dict."""
    ft = one(db, "select type from orgs where id=?", from_org)["type"]
    tt = one(db, "select type from orgs where id=?", to_org)["type"]
    if SUPPLIER_TYPE.get(ft) != tt:
        raise ValueError(f"a {ft} cannot order from a {tt}")
    if qty <= 0:
        raise ValueError("qty must be positive")
    value = round(qty * price_for(db, to_org, med), 2)
    status = "draft" if needs_person(db, value) else "auto_approved"
    now = dt.datetime.now().isoformat(timespec="seconds")
    cur = db.execute("insert into orders(from_org_id,to_org_id,medicine_id,qty,value,status,reason,source,parent_order_id,"
                     "created_by,approved_by,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?)".replace("?,?,?,?,?,?,?,?,?,?,?,?,?", "?,?,?,?,?,?,?,?,?,?,?,?,?"),
                     (from_org, to_org, med, qty, value, status, reason, source, parent, actor,
                      "policy" if status == "auto_approved" else None, now, now))
    oid = cur.lastrowid
    m = one(db, "select name from medicines where id=?", med)["name"]
    audit(db, actor, actor_type, "create_order", f"order #{oid}: {qty} x {m}, Rs {value:,.0f}, {source}", [from_org, to_org])
    if status == "auto_approved":
        audit(db, "policy", "policy", "auto_approve",
              f"order #{oid} auto-approved: Rs {value:,.0f} <= limit {float(setting(db, 'max_order_value')):,.0f}", [from_org, to_org])
    return one(db, "select * from orders where id=?", oid)


AGENT_OF = {"dealer": "dealer-agent", "cf": "cf-agent", "manufacturer": "manufacturer-agent"}


def order_rows(db, where, *a):
    rows = q(db, "select o.*, f.name from_org, t.name to_org, t.type to_type, m.name medicine, "
                 "(select username from users where org_id=o.from_org_id) approver_login, "
                 "(select username from users where org_id=o.to_org_id) to_login, "
                 "p.id plan_id, p.status plan_status, p.ready_date plan_ready, p.approved_by plan_by, p.qty plan_qty "
                 "from orders o join orgs f on f.id=o.from_org_id join orgs t on t.id=o.to_org_id "
                 "join medicines m on m.id=o.medicine_id left join production_plans p on p.source_order_id=o.id "
                 f"where {where} order by o.id desc", *a)
    for r in rows:
        r["to_agent"] = AGENT_OF.get(r["to_type"], "")
    return rows


def _open_request(db, org_id, med_id):
    return one(db, "select id from orders where from_org_id=? and medicine_id=? and status in ('draft','approved','auto_approved')",
               org_id, med_id)


# ---------------------------------------------------------------- pharmacy
def pharmacy_summary(db, org_id):
    d = as_of(db)
    win = int(setting(db, "forecast_window_days"))
    start = d - dt.timedelta(days=win)
    org = one(db, "select * from orgs where id=?", org_id)
    meds = []
    for m in q(db, "select * from medicines order by name"):
        rows = q(db, "select sale_date,qty from sales where pharmacy_org_id=? and medicine_id=? and sale_date>=? and sale_date<?",
                 org_id, m["id"], start.isoformat(), d.isoformat())
        by = {r["sale_date"]: r["qty"] for r in rows}
        daily = [by.get((start + dt.timedelta(days=i)).isoformat(), 0) for i in range(win)]
        st = one(db, "select qty_on_hand,basis from pharmacy_stock where org_id=? and medicine_id=?", org_id, m["id"]) or {"qty_on_hand": 0, "basis": "estimate"}
        pd_ = preferred(db, org_id, m["id"])
        meds.append({"medicine": m["name"], "on_hand": st["qty_on_hand"], "stock_basis": st["basis"],
                     "sales_total": sum(daily), "daily_sales": daily,
                     "dealer": pd_["name"] if pd_ else None, "reference_price": m["reference_price"]})
    return {"role": "pharmacy", "org": org["name"], "as_of": d.isoformat(),
            "assumptions": ["Stock is an estimate: latest order quantity minus sales since that order (workbook has no pharmacy stock)",
                            "Dealer lead time 2 days; price = average manufacturer unit cost"],
            "settings": {"pharmacy_lead_days": int(setting(db, "pharmacy_lead_days")),
                         "safety_stock_days": int(setting(db, "safety_days")), "forecast_window_days": win},
            "medicines": meds, "orders": order_rows(db, "o.from_org_id=?", org_id)[:40]}


# ---------------------------------------------------------------- dealer
def dealer_summary(db, org_id):
    d = as_of(db)
    org = one(db, "select * from orgs where id=?", org_id)
    stock = {r["medicine_id"]: r for r in q(db, "select s.*, m.name medicine from dealer_stock s join medicines m on m.id=s.medicine_id where org_id=? order by m.name", org_id)}
    run = {k: v["qty_on_hand"] for k, v in stock.items()}
    short_tot = {k: 0 for k in stock}
    incoming = []
    for o in order_rows(db, "o.to_org_id=? and o.status in ('approved','auto_approved')", org_id)[::-1]:
        fill = min(o["qty"], run.get(o["medicine_id"], 0))
        run[o["medicine_id"]] = run.get(o["medicine_id"], 0) - fill
        short_tot[o["medicine_id"]] += o["qty"] - fill
        s = stock[o["medicine_id"]]
        incoming.append({"order_id": o["id"], "pharmacy": o["from_org"], "medicine": o["medicine"], "ordered": o["qty"],
                         "can_fill": fill, "short": o["qty"] - fill, "projected": run[o["medicine_id"]],
                         "reorder_level": s["reorder_level"]})
    own = []
    for mid, s in stock.items():
        proj, short, rl = run[mid], short_tot[mid], s["reorder_level"]
        sup = preferred(db, org_id, mid)
        pend = _open_request(db, org_id, mid)
        need = short > 0 or proj < rl
        rep = ceil_to(short + 2 * rl - proj, 100) if need else 0
        reason = ""
        if need and pend:
            rep, reason = 0, f"request #{pend['id']} to C&F already open"
        own.append({"medicine": s["medicine"], "stock": s["qty_on_hand"], "projected": proj, "total_short": short,
                    "reorder_level": rl, "replenish_qty": rep, "cf": sup["name"] if sup else None, "note": reason})
    hist = q(db, "select h.order_date,c.name cf,m.name medicine,h.qty,h.status from history_dealer_orders h "
                 "join orgs c on c.id=h.cf_org_id join medicines m on m.id=h.medicine_id where h.dealer_org_id=? "
                 "order by h.order_date desc limit 10", org_id)
    return {"role": "dealer", "org": org["name"], "as_of": d.isoformat(), "stock": list(stock.values()),
            "incoming": incoming, "own_check": own, "orders": order_rows(db, "o.from_org_id=?", org_id)[:40],
            "received": order_rows(db, "o.to_org_id=? and o.status<>'draft'", org_id)[:40], "history_to_cf": hist}


# ---------------------------------------------------------------- C&F
def cf_summary(db, org_id):
    d = as_of(db)
    safety = int(setting(db, "safety_days"))
    org = one(db, "select * from orgs where id=?", org_id)
    stock = {}
    for r in q(db, "select s.*, m.name medicine from cf_stock s join medicines m on m.id=s.medicine_id where org_id=? order by m.name", org_id):
        mf = preferred(db, org_id, r["medicine_id"])
        ld = lead_days(db, org_id, mf["id"], r["medicine_id"]) if mf else 7.0
        r.update(manufacturer=mf["name"] if mf else None, lead_days=round(ld, 2),
                 reorder_point=round(r["avg_daily_out"] * (ld + safety), 1))
        r["days_of_cover"] = round(r["qty_on_hand"] / r["avg_daily_out"], 1) if r["avg_daily_out"] else None
        r["expires_soon"] = dt.date.fromisoformat(r["expiry_date"]) <= d + dt.timedelta(days=180)
        stock[r["medicine_id"]] = r
    inc = order_rows(db, "o.to_org_id=? and o.status in ('approved','auto_approved')", org_id)[::-1]
    req_tot = {}
    for o in inc:
        req_tot[o["medicine_id"]] = req_tot.get(o["medicine_id"], 0) + o["qty"]
    run = {k: v["qty_on_hand"] for k, v in stock.items()}
    short_tot = {k: 0 for k in stock}
    incoming = []
    for o in inc:
        mid, avail, tot = o["medicine_id"], stock[o["medicine_id"]]["qty_on_hand"], req_tot[o["medicine_id"]]
        # several dealers short on the same medicine: split in proportion to requests
        fill = o["qty"] if tot <= avail else int(avail * o["qty"] / tot)
        fill = min(fill, run[mid])
        run[mid] -= fill
        short_tot[mid] += o["qty"] - fill
        s = stock[mid]
        incoming.append({"order_id": o["id"], "dealer": o["from_org"], "medicine": o["medicine"], "requested": o["qty"],
                         "can_dispatch": fill, "short": o["qty"] - fill, "projected": run[mid],
                         "reorder_point": s["reorder_point"], "batch": s["batch"], "expiry": s["expiry_date"],
                         "split_note": "split in proportion to requests" if tot > avail else ""})
    inbound = {r["medicine_id"]: r["n"] for r in q(db, "select medicine_id, sum(filled_qty) n from orders where from_org_id=? and status in ('filled','partial') group by medicine_id", org_id)}
    own = []
    for mid, s in stock.items():
        proj, short, rp = run[mid], short_tot[mid], s["reorder_point"]
        on_order = inbound.get(mid, 0)
        need = short > 0 or proj + on_order < rp
        rep = ceil_to(short + 2 * rp - proj - on_order, 1000) if need else 0
        pend = _open_request(db, org_id, mid)
        note = ""
        if rep and pend:
            rep, note = 0, f"request #{pend['id']} to manufacturer already open"
        own.append({"medicine": s["medicine"], "stock": s["qty_on_hand"], "projected": proj, "total_short": short,
                    "on_order_from_manufacturer": on_order, "avg_daily_out": round(s["avg_daily_out"], 1), "reorder_point": rp, "days_of_cover": s["days_of_cover"],
                    "lead_days": s["lead_days"], "replenish_qty": rep, "manufacturer": s["manufacturer"],
                    "batch": s["batch"], "expiry": s["expiry_date"], "expires_within_180_days": s["expires_soon"], "note": note})
    return {"role": "cf", "org": org["name"], "as_of": d.isoformat(), "stock": list(stock.values()),
            "incoming": incoming, "own_check": own, "orders": order_rows(db, "o.from_org_id=?", org_id)[:40],
            "received": order_rows(db, "o.to_org_id=? and o.status<>'draft'", org_id)[:40]}


# ---------------------------------------------------------------- manufacturer
def manufacturer_summary(db, org_id):
    d = as_of(db)
    org = one(db, "select * from orgs where id=?", org_id)
    cap = {r["medicine_id"]: r for r in q(db, "select c.*, m.name medicine from capacity c join medicines m on m.id=c.medicine_id where manufacturer_org_id=? order by m.name", org_id)}
    incoming = []
    for o in order_rows(db, "o.to_org_id=? and o.status in ('approved','auto_approved')", org_id)[::-1]:
        c = cap[o["medicine_id"]]
        batch = min(o["qty"], c["production_capacity"])
        over = o["qty"] - batch
        nxt = None
        if over:
            nxt = one(db, "select o.name from capacity c join orgs o on o.id=c.manufacturer_org_id where c.medicine_id=? and c.manufacturer_org_id<>? "
                          "and c.production_capacity>=? order by c.unit_cost limit 1", o["medicine_id"], org_id, over)
            nxt = nxt["name"] if nxt else None
        ld = lead_days(db, o["from_org_id"], org_id, o["medicine_id"])
        incoming.append({"order_id": o["id"], "cf": o["from_org"], "medicine": o["medicine"], "requested": o["qty"],
                         "capacity": c["production_capacity"], "batch_qty": batch, "overflow_qty": over, "overflow_to": nxt,
                         "lead_days": round(ld, 2), "ready_date": (d + dt.timedelta(days=math.ceil(ld))).isoformat(),
                         "unit_cost": c["unit_cost"], "value": round(batch * c["unit_cost"], 2)})
    plans = q(db, "select p.*, m.name medicine from production_plans p join medicines m on m.id=p.medicine_id "
                  "where manufacturer_org_id=? order by p.id desc", org_id)
    return {"role": "manufacturer", "org": org["name"], "as_of": d.isoformat(), "capacity": list(cap.values()),
            "incoming": incoming, "plans": plans,
            "received": order_rows(db, "o.to_org_id=? and o.status<>'draft'", org_id)[:40],
            "assumption": "No finished stock is held; every C&F request becomes a production plan"}


SUMMARY = {"pharmacy": pharmacy_summary, "dealer": dealer_summary, "cf": cf_summary, "manufacturer": manufacturer_summary}


def create_plan(db, mfr_id, med_id, qty, ready_date, reason, source_order_id, actor):
    c = one(db, "select unit_cost from capacity where manufacturer_org_id=? and medicine_id=?", mfr_id, med_id)
    value = round(qty * c["unit_cost"], 2)
    status = "draft" if needs_person(db, value) else "auto_approved"
    cur = db.execute("insert into production_plans(manufacturer_org_id,medicine_id,qty,value,ready_date,status,reason,source_order_id,approved_by) "
                     "values(?,?,?,?,?,?,?,?,?)", (mfr_id, med_id, qty, value, ready_date, status, reason, source_order_id,
                                                  "policy" if status == "auto_approved" else None))
    pid = cur.lastrowid
    audit(db, actor, "agent", "create_plan", f"plan #{pid}: {qty} units, Rs {value:,.0f}, ready {ready_date}", [mfr_id])
    if status == "auto_approved":
        audit(db, "policy", "policy", "auto_approve", f"plan #{pid} auto-approved: Rs {value:,.0f}", [mfr_id])
    return one(db, "select * from production_plans where id=?", pid)


def record_result(db, order_id, filled, short, note, actor):
    o = one(db, "select * from orders where id=?", order_id)
    if not o or o["status"] not in LIVE:
        raise ValueError("order must be approved before a result is recorded")
    if filled < 0 or short < 0 or filled + short != o["qty"]:
        raise ValueError("filled_qty + short_qty must equal the order quantity")
    status = "filled" if short == 0 else "partial"
    db.execute("update orders set status=?, filled_qty=?, short_qty=?, updated_at=? where id=?",
               (status, filled, short, dt.datetime.now().isoformat(timespec="seconds"), order_id))
    buyer = one(db, "select type from orgs where id=?", o["from_org_id"])["type"]
    seller = one(db, "select type from orgs where id=?", o["to_org_id"])["type"]
    if filled and seller != "manufacturer":
        tbl = {"dealer": "dealer_stock", "cf": "cf_stock"}[seller]
        db.execute(f"update {tbl} set qty_on_hand=qty_on_hand-? where org_id=? and medicine_id=?", (filled, o["to_org_id"], o["medicine_id"]))
        btbl = {"pharmacy": "pharmacy_stock", "dealer": "dealer_stock"}[buyer]
        db.execute(f"update {btbl} set qty_on_hand=qty_on_hand+? where org_id=? and medicine_id=?", (filled, o["from_org_id"], o["medicine_id"]))
    audit(db, actor, "agent", "record_result", f"order #{order_id} {status}: filled {filled}, short {short}. {note or ''}".strip(),
          [o["from_org_id"], o["to_org_id"]])
    return one(db, "select * from orders where id=?", order_id)
