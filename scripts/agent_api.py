"""The only way agents talk to the app. Picks the token by command and never prints it."""
import argparse
import json
import sys
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent


def env():
    e = {}
    for line in (ROOT / ".env.agents").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            e[k.strip()] = v.strip()
    return e


def call(method, path, token_key, **kw):
    e = env()
    r = requests.request(method, e["APP_URL"] + path, headers={"Authorization": "Bearer " + e[token_key]}, timeout=30, **kw)
    try:
        out = r.json()
    except ValueError:
        out = {"error": r.text}
    if r.status_code >= 400:
        print(json.dumps({"http_status": r.status_code, **(out if isinstance(out, dict) else {"body": out})}), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(out, indent=2))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary"); s.add_argument("role", choices=["pharmacy", "dealer", "cf", "manufacturer"]); s.add_argument("--org", required=True)
    o = sub.add_parser("orgs"); o.add_argument("role", choices=["pharmacy", "dealer", "cf", "manufacturer"])
    l = sub.add_parser("orders"); l.add_argument("--status"); l.add_argument("--level")
    c = sub.add_parser("create-order"); c.add_argument("--json", required=True)
    r = sub.add_parser("record-result"); r.add_argument("order_id", type=int); r.add_argument("--filled", type=int, required=True)
    r.add_argument("--short", type=int, required=True); r.add_argument("--note", default=""); r.add_argument("--agent")
    pl = sub.add_parser("create-plan"); pl.add_argument("--json", required=True)
    a = p.parse_args()

    if a.cmd == "summary":
        call("GET", f"/api/{a.role}/summary", a.role.upper() + "_AGENT_TOKEN", params={"org": a.org})
    elif a.cmd == "orgs":
        call("GET", "/api/orgs", "COORDINATOR_TOKEN", params={"type": a.role})
    elif a.cmd == "orders":
        call("GET", "/api/orders", "COORDINATOR_TOKEN", params={k: v for k, v in (("status", a.status), ("level", a.level)) if v})
    elif a.cmd == "create-order":
        call("POST", "/api/orders", "COORDINATOR_TOKEN", json=json.loads(a.json))
    elif a.cmd == "record-result":
        call("POST", f"/api/orders/{a.order_id}/result", "COORDINATOR_TOKEN",
             json={"filled_qty": a.filled, "short_qty": a.short, "note": a.note, "agent": a.agent})
    elif a.cmd == "create-plan":
        call("POST", "/api/plans", "COORDINATOR_TOKEN", json=json.loads(a.json))


if __name__ == "__main__":
    main()
