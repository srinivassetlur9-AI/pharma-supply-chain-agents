"""Event-driven coordinator. Polls the app; when an order is approved (by a person or by policy) it moves the work
one level up, using the same agent_api.py/summary data as the agents. It never approves anything.

  python scripts/orchestrator.py            # built-in rules (fast, no Claude needed)
  python scripts/orchestrator.py --claude   # when work is found, runs `claude -p "continue replenishment"` instead
  python scripts/orchestrator.py --forecast # also run the pharmacy forecast on start
Stop with Ctrl+C."""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = [sys.executable, str(ROOT / "scripts" / "agent_api.py")]


def api(*a):
    r = subprocess.run(API + list(a), capture_output=True, text=True, cwd=ROOT)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return json.loads(r.stdout)


PACE = 0.0
for _a in sys.argv:
    if _a.startswith("--pace="):
        PACE = float(_a.split("=")[1])


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)
    time.sleep(PACE)


def order(frm, to, med, qty, reason, source, parent=None, agent="pharmacy-coordinator"):
    o = api("create-order", "--json", json.dumps({"agent": agent, "from_org": frm, "to_org": to, "medicine": med, "qty": qty,
                                                  "reason": reason, "source": source, "parent_order_id": parent}))
    say(f"  -> order #{o['id']} {frm} -> {to}: {qty} x {med}, Rs {o['value']:,.0f}, {o['status']}"
        + ("  (waiting for a person to approve)" if o["status"] == "draft" else ""))


def forecast():
    for ph in api("orgs", "pharmacy"):
        s = subprocess.run(API + ["summary", "pharmacy", "--org", ph], capture_output=True, text=True, cwd=ROOT).stdout
        lines = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts" / "forecast.py")], input=s,
                                          capture_output=True, text=True).stdout)
        for l in lines:
            order(ph, l["dealer"], l["medicine"], l["order_qty"],
                  f"{l['medicine']}: selling {l['avg_daily']}/day, {l['on_hand']} on hand ({l['days_left']} days), "
                  f"{l['target']} needed for lead + safety cover.", "agent")


def level1():  # dealers fill approved pharmacy orders, then restock themselves
    work = False
    for d in api("orgs", "dealer"):
        s = api("summary", "dealer", "--org", d)
        if not s["incoming"]:
            continue
        work = True
        say(f"DEALER-AGENT woke up for {d}: {len(s['incoming'])} approved pharmacy order(s) to check against stock")
        firsts = {}
        for i in s["incoming"]:
            if i["short"]:
                firsts.setdefault(i["medicine"], i["order_id"])
        for i in s["incoming"]:
            api("record-result", str(i["order_id"]), "--filled", str(i["can_fill"]), "--short", str(i["short"]),
                "--note", f"filled from {d} stock", "--agent", "dealer-agent")
            say(f"  order #{i['order_id']} {i['medicine']}: filled {i['can_fill']}, short {i['short']}")
        for o in s["own_check"]:
            if o["replenish_qty"]:
                order(d, o["cf"], o["medicine"], o["replenish_qty"],
                      f"{o['medicine']}: stock after orders {o['projected']}, reorder level {o['reorder_level']}, "
                      f"short {o['total_short']}.", "agent", firsts.get(o["medicine"]), "dealer-agent")
    return work


def level2():  # C&F dispatches approved dealer requests and checks its own stock
    work = False
    for c in api("orgs", "cf"):
        s = api("summary", "cf", "--org", c)
        if s["incoming"]:
            work = True
            say(f"CF-AGENT woke up for {c}: {len(s['incoming'])} approved dealer request(s) to check against stock")
            for i in s["incoming"]:
                api("record-result", str(i["order_id"]), "--filled", str(i["can_dispatch"]), "--short", str(i["short"]),
                    "--note", f"dispatched from batch {i['batch']}", "--agent", "cf-agent")
                say(f"  order #{i['order_id']} {i['medicine']}: dispatched {i['can_dispatch']}, short {i['short']}")
            s = api("summary", "cf", "--org", c)  # stock changed
        for o in s["own_check"]:
            if o["replenish_qty"]:
                work = True
                order(c, o["manufacturer"], o["medicine"], o["replenish_qty"],
                      f"{o['medicine']}: {o['stock']} in stock ({o['days_of_cover']} days), reorder point "
                      f"{o['reorder_point']:,.0f}, lead time {o['lead_days']} days.", "own_check", None, "cf-agent")
    return work


def level3():  # manufacturers plan batches for approved C&F requests
    work = False
    for m in api("orgs", "manufacturer"):
        s = api("summary", "manufacturer", "--org", m)
        for i in s["incoming"]:
            work = True
            say(f"MANUFACTURER-AGENT woke up for {m}: request #{i['order_id']} {i['medicine']} x {i['requested']}")
            p = api("create-plan", "--json", json.dumps({
                "agent": "manufacturer-agent", "manufacturer": m, "medicine": i["medicine"], "qty": i["batch_qty"], "ready_date": i["ready_date"],
                "source_order_id": i["order_id"],
                "reason": f"{i['medicine']}: {i['batch_qty']} units at {i['unit_cost']} each (capacity {i['capacity']:,}), "
                          f"ready {i['ready_date']}."}))
            say(f"  -> plan #{p['id']} {p['qty']} units, Rs {p['value']:,.0f}, {p['status']}"
                + ("  (waiting for a person to approve)" if p["status"] == "draft" else ""))
            api("record-result", str(i["order_id"]), "--filled", str(i["batch_qty"]), "--short", str(i["requested"] - i["batch_qty"]),
                "--note", f"production planned, ready {i['ready_date']}", "--agent", "manufacturer-agent")
            if i["overflow_qty"] and i["overflow_to"]:
                order(i["cf"], i["overflow_to"], i["medicine"], i["overflow_qty"], "Overflow above capacity.", "agent", i["order_id"], "manufacturer-agent")
    return work


def pending():
    return sum(len(api("orders", "--status", "approved", "--level", lv)) for lv in ("pharmacy", "dealer", "cf"))


def main():
    use_claude = "--claude" in sys.argv
    say("Watching for approved orders. Ctrl+C to stop." + (" (Claude mode)" if use_claude else ""))
    if "--forecast" in sys.argv:
        forecast()
    last_draft_note = None
    while True:
        try:
            if use_claude:
                if pending():
                    say("Approved work found, handing to Claude...")
                    subprocess.run(["claude", "-p", "continue replenishment"], cwd=ROOT)
            else:
                moved = level1() | level2() | level3()
                if moved:
                    say("Waiting for a person: approve any draft order or production plan in the browser (the page shows which login).")
        except Exception as e:  # keep watching if the app restarts
            say(f"Problem: {e}")
        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
