"""Reads one pharmacy summary JSON on stdin; prints order lines. All forecast maths lives here."""
import json
import math
import sys

d = json.load(sys.stdin)
s = d["settings"]
cover_days = s["pharmacy_lead_days"] + s["safety_stock_days"]
lines = []
for m in d["medicines"]:
    avg = m["sales_total"] / s["forecast_window_days"]
    target = avg * cover_days
    qty = int(math.ceil(max(0.0, target - m["on_hand"]) / 10) * 10)
    if qty > 0:
        lines.append({"pharmacy": d["org"], "medicine": m["medicine"], "order_qty": qty, "avg_daily": round(avg, 2),
                      "on_hand": m["on_hand"], "days_left": round(m["on_hand"] / avg, 1) if avg else None,
                      "target": round(target, 1), "dealer": m["dealer"]})
print(json.dumps(lines, indent=2))
