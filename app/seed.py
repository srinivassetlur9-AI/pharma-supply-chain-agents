"""Loads the workbook into data/app.db and generates demo tokens. Re-running resets the demo."""
import secrets
import sys
import datetime as dt
from pathlib import Path
import pandas as pd
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import rules  # noqa: E402

XLSX = ROOT / "data" / "Pharmacy_SupplyChain_Large_Dataset.xlsx"
EXPECTED = {
    "Sales_History": ["Date", "Pharmacy", "Medicine", "Quantity Sold"],
    "Pharmacy_to_Dealer": ["Order Date", "Pharmacy", "Dealer", "Medicine", "Quantity"],
    "Dealer_Inventory": ["Dealer", "Medicine", "Stock On Hand", "Reorder Level"],
    "Dealer_to_CF": ["Order Date", "Dealer", "CF Agent", "Medicine", "Quantity", "Status"],
    "CF_Inventory": ["CF Agent", "Medicine", "Stock", "Batch", "Expiry Date"],
    "CF_to_Manufacturer": ["Order Date", "CF Agent", "Manufacturer", "Medicine", "Quantity", "Lead Time Days"],
    "Manufacturers": ["Manufacturer", "Medicine", "Production Capacity", "Unit Cost"],
    "Settings": ["Setting", "Value"],
}
USERS = {"citycare": "City Care Pharmacy", "apollo": "Apollo Local", "healthplus": "HealthPlus Pharmacy",
         "medimart": "MediMart", "wellness": "Wellness Drugs", "dealer-east": "Dealer East",
         "dealer-north": "Dealer North", "dealer-south": "Dealer South", "cf-pune": "CF Pune",
         "cf-mumbai": "CF Mumbai", "sunpharma": "Sun Pharma", "cipla": "Cipla", "drreddys": "Dr Reddys", "lupin": "Lupin"}

SCHEMA = """
create table orgs(id integer primary key, name text unique, type text);
create table users(id integer primary key, username text unique, password_hash text, org_id integer);
create table agent_tokens(token text primary key, name text, scope text);
create table medicines(id integer primary key, name text unique, reference_price real);
create table sales(pharmacy_org_id int, medicine_id int, sale_date text, qty int);
create table pharmacy_stock(org_id int, medicine_id int, qty_on_hand int, basis text);
create table dealer_stock(org_id int, medicine_id int, qty_on_hand int, reorder_level int);
create table cf_stock(org_id int, medicine_id int, qty_on_hand int, batch text, expiry_date text, avg_daily_out real);
create table capacity(manufacturer_org_id int, medicine_id int, production_capacity int, unit_cost real);
create table history_pharmacy_orders(order_date text, pharmacy_org_id int, dealer_org_id int, medicine_id int, qty int);
create table history_dealer_orders(order_date text, dealer_org_id int, cf_org_id int, medicine_id int, qty int, status text);
create table history_cf_orders(order_date text, cf_org_id int, manufacturer_org_id int, medicine_id int, qty int, lead_time_days int);
create table preferred_supplier(buyer_org_id int, medicine_id int, supplier_org_id int);
create table lead_times(cf_org_id int, manufacturer_org_id int, medicine_id int, avg_lead_days real);
create table settings(key text primary key, value text);
create table orders(id integer primary key, from_org_id int, to_org_id int, medicine_id int, qty int, value real, status text,
    reason text, source text, parent_order_id int, filled_qty int, short_qty int, created_by text, approved_by text,
    created_at text, updated_at text);
create table production_plans(id integer primary key, manufacturer_org_id int, medicine_id int, qty int, value real,
    ready_date text, status text, reason text, source_order_id int, approved_by text);
create table audit_log(ts text, actor text, actor_type text, action text, detail text, org_id int);
"""


def preferred(df, buyer, supplier, ids):
    g = df.groupby([buyer, "Medicine", supplier]).agg(n=("Quantity", "size"), q=("Quantity", "sum")).reset_index()
    g = g.sort_values([buyer, "Medicine", "n", "q", supplier], ascending=[True, True, False, False, True])
    return g.drop_duplicates([buyer, "Medicine"])


def main():
    x = pd.read_excel(XLSX, sheet_name=None)
    for sh, cols in EXPECTED.items():
        if sh not in x:
            sys.exit(f"Missing sheet: {sh}")
        miss = [c for c in cols if c not in x[sh].columns]
        if miss:
            sys.exit(f"Sheet {sh} is missing columns: {miss}")
    try:
        rules.DB.unlink(missing_ok=True)
    except PermissionError:  # Windows: the running app still holds the file, so empty it instead
        old = rules.conn()
        for (t,) in old.execute("select name from sqlite_master where type='table'").fetchall():
            old.execute(f"drop table {t}")
        old.commit()
        old.close()
    db = rules.conn()
    db.executescript(SCHEMA)

    types = {}
    for n in x["Sales_History"]["Pharmacy"].unique():
        types[n] = "pharmacy"
    for n in x["Dealer_Inventory"]["Dealer"].unique():
        types[n] = "dealer"
    for n in x["CF_Inventory"]["CF Agent"].unique():
        types[n] = "cf"
    for n in x["Manufacturers"]["Manufacturer"].unique():
        types[n] = "manufacturer"
    oid = {}
    for n, t in sorted(types.items(), key=lambda kv: (kv[1], kv[0])):
        oid[n] = db.execute("insert into orgs(name,type) values(?,?)", (n, t)).lastrowid
    pw = generate_password_hash("demo123")
    for u, n in USERS.items():
        db.execute("insert into users(username,password_hash,org_id) values(?,?,?)", (u, pw, oid[n]))

    mf = x["Manufacturers"]
    ref = mf.groupby("Medicine")["Unit Cost"].mean().round(3)
    mid = {}
    for n in sorted(mf["Medicine"].unique()):
        mid[n] = db.execute("insert into medicines(name,reference_price) values(?,?)", (n, float(ref[n]))).lastrowid
    for r in mf.itertuples(index=False):
        db.execute("insert into capacity values(?,?,?,?)", (oid[r[0]], mid[r[1]], int(r[2]), float(r[3])))

    s = x["Sales_History"].copy()
    s["Date"] = pd.to_datetime(s["Date"])
    s = s.groupby(["Pharmacy", "Medicine", "Date"], as_index=False)["Quantity Sold"].sum()
    last = s["Date"].max().date()
    asof = last + dt.timedelta(days=1)
    for r in s.itertuples(index=False):
        db.execute("insert into sales values(?,?,?,?)", (oid[r[0]], mid[r[1]], r[2].date().isoformat(), int(r[3])))

    p2d, d2c, c2m = x["Pharmacy_to_Dealer"].copy(), x["Dealer_to_CF"].copy(), x["CF_to_Manufacturer"].copy()
    for df in (p2d, d2c, c2m):
        df["Order Date"] = pd.to_datetime(df["Order Date"])
    for r in p2d.itertuples(index=False):
        db.execute("insert into history_pharmacy_orders values(?,?,?,?,?)", (r[0].date().isoformat(), oid[r[1]], oid[r[2]], mid[r[3]], int(r[4])))
    for r in d2c.itertuples(index=False):
        db.execute("insert into history_dealer_orders values(?,?,?,?,?,?)", (r[0].date().isoformat(), oid[r[1]], oid[r[2]], mid[r[3]], int(r[4]), r[5]))
    for r in c2m.itertuples(index=False):
        db.execute("insert into history_cf_orders values(?,?,?,?,?,?)", (r[0].date().isoformat(), oid[r[1]], oid[r[2]], mid[r[3]], int(r[4]), int(r[5])))

    for df, buyer, sup in ((p2d, "Pharmacy", "Dealer"), (d2c, "Dealer", "CF Agent"), (c2m, "CF Agent", "Manufacturer")):
        for r in preferred(df, buyer, sup, None).itertuples(index=False):
            db.execute("insert into preferred_supplier values(?,?,?)", (oid[r[0]], mid[r[1]], oid[r[2]]))
    lt = c2m.groupby(["CF Agent", "Manufacturer", "Medicine"])["Lead Time Days"].mean()
    for (c, m, med), v in lt.items():
        db.execute("insert into lead_times values(?,?,?,?)", (oid[c], oid[m], mid[med], float(v)))

    for r in x["Dealer_Inventory"].itertuples(index=False):
        db.execute("insert into dealer_stock values(?,?,?,?)", (oid[r[0]], mid[r[1]], int(r[2]), int(r[3])))
    out = d2c.groupby(["CF Agent", "Medicine"])["Quantity"].sum() / 365.0
    for r in x["CF_Inventory"].itertuples(index=False):
        db.execute("insert into cf_stock values(?,?,?,?,?,?)",
                   (oid[r[0]], mid[r[1]], int(r[2]), r[3], pd.to_datetime(r[4]).date().isoformat(), float(out.get((r[0], r[1]), 0))))

    # pharmacy stock estimate: latest order qty (summed per date) minus sales since that order date
    for (ph, med), g in p2d.groupby(["Pharmacy", "Medicine"]):
        d0 = g["Order Date"].max()
        qty = int(g[g["Order Date"] == d0]["Quantity"].sum())
        sold = s[(s["Pharmacy"] == ph) & (s["Medicine"] == med) & (s["Date"] >= d0)]["Quantity Sold"].sum()
        db.execute("insert into pharmacy_stock values(?,?,?,?)", (oid[ph], mid[med], max(0, int(qty - sold)), "estimate"))
    for ph in [n for n, t in types.items() if t == "pharmacy"]:
        for med in mid:
            if not db.execute("select 1 from pharmacy_stock where org_id=? and medicine_id=?", (oid[ph], mid[med])).fetchone():
                db.execute("insert into pharmacy_stock values(?,?,?,?)", (oid[ph], mid[med], 0, "estimate"))

    st = dict(zip(x["Settings"]["Setting"], x["Settings"]["Value"]))
    for k, v in {"as_of": asof.isoformat(), "safety_days": int(st["Safety Stock Days"]),
                 "max_order_value": float(st["Max Order Value Without Approval"]), "pharmacy_lead_days": 2,
                 "forecast_window_days": 28, "approval_mode": "threshold"}.items():
        db.execute("insert into settings values(?,?)", (k, str(v)))

    names = {"pharmacy": "pharmacy-agent", "dealer": "dealer-agent", "cf": "cf-agent",
             "manufacturer": "manufacturer-agent", "coordinator": "pharmacy-coordinator"}
    env = ["APP_URL=http://127.0.0.1:5000"]
    for scope, n in names.items():
        t = secrets.token_urlsafe(24)
        db.execute("insert into agent_tokens values(?,?,?)", (t, n, scope))
        env.append(f"{'COORDINATOR' if scope == 'coordinator' else scope.upper() + '_AGENT'}_TOKEN={t}")
    (ROOT / ".env.agents").write_text("\n".join(env) + "\n")
    db.commit()
    print(f"Seeded {len(oid)} orgs, as-of {asof}. Tokens written to .env.agents")


if __name__ == "__main__":
    main()
