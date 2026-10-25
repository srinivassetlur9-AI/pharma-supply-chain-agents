---
name: pharma-supply-chain-agents
description: "Builds and runs the pharmacy to dealer to C&F to manufacturer replenishment demo from Pharmacy_SupplyChain_Large_Dataset.xlsx: a web app with a login per organisation, plus a main agent and three subagents that read their role's data."
metadata:
  version: 2.0.0
  domain: pharmacy supply chain
  use_case: "#4 Demand forecast and replenishment"
  data_source: Pharmacy_SupplyChain_Large_Dataset.xlsx
  data: synthetic, demo-only
  stack: [Python 3, Flask, SQLite, pandas, openpyxl, Claude Code]
  main_agent: pharmacy-coordinator
  subagents: [dealer-agent, cf-agent, manufacturer-agent]
---

# Pharma supply-chain agents: app + main agent + 3 subagents

This skill does two jobs:

- **Build:** load `Pharmacy_SupplyChain_Large_Dataset.xlsx` into a small web application with a login for every organisation in the data (5 pharmacies, 3 dealers, 2 C&F agents, 4 manufacturers), plus the agent files, scripts and hooks.
- **Run:** act as the main agent (the pharmacy coordinator). Forecast demand from the sales history, draft orders, and carry any shortfall up the chain through three subagents, one level at a time.

The workbook is synthetic. The logins are demo-only. Never put real patient data, real passwords or real supplier details in this app.

## Quick reference (YAML)

The same design as the sections below, in one structured block.

```yaml
project: pharma-chain
data_source:
  file: data/Pharmacy_SupplyChain_Large_Dataset.xlsx
  sheets: [Sales_History, Pharmacy_to_Dealer, Dealer_Inventory, Dealer_to_CF, CF_Inventory, CF_to_Manufacturer, Manufacturers, Settings]
  as_of_date: day after the last Sales_History date (2026-01-01 for this file)
app:
  framework: flask
  database: data/app.db
  url: http://127.0.0.1:5000
  access:
    this_laptop: http://127.0.0.1:5000
    same_wifi: python app/app.py --share, others open http://<your-laptop-IP>:5000 (trusted Wi-Fi only)
    internet: hosting service or a tunnel (ngrok, Cloudflare Tunnel), briefly, never with real data
    debug: always False
organisations:
  pharmacy: [Apollo Local, City Care Pharmacy, HealthPlus Pharmacy, MediMart, Wellness Drugs]
  dealer: [Dealer East, Dealer North, Dealer South]
  cf: [CF Mumbai, CF Pune]
  manufacturer: [Cipla, Dr Reddys, Lupin, Sun Pharma]
medicines: [Amlodipine 5mg, Amoxicillin 250mg, Atorvastatin 10mg, Azithromycin 500mg, Cetirizine 10mg, Ibuprofen 400mg, Metformin 500mg, Omeprazole 20mg, Pantoprazole 40mg, Paracetamol 500mg]
roles:
  - role: pharmacy
    page: /pharmacy
    api: /api/pharmacy/summary?org=<name>
    read_by: main-agent
  - role: dealer
    page: /dealer
    api: /api/dealer/summary?org=<name>
    read_by: dealer-agent
  - role: cf
    page: /cf
    api: /api/cf/summary?org=<name>
    read_by: cf-agent
  - role: manufacturer
    page: /manufacturer
    api: /api/manufacturer/summary?org=<name>
    read_by: manufacturer-agent
demo_password: demo123   # demo-only, shown on the login page
settings:
  safety_stock_days: 14                    # from the Settings sheet
  max_order_value_without_approval: 50000  # from the Settings sheet, in rupees
  pharmacy_lead_days: 2                    # assumption: not in the workbook
  forecast_window_days: 28                 # assumption
  approval_mode: threshold                 # threshold (from Settings) or all
agents:
  main:
    name: pharmacy-coordinator
    writes: [create-order, record-result, create-plan]
    approves: never
  subagents:
    - name: dealer-agent
      level: 1
      reads: summary dealer
      returns: [order_id, product, ordered, can_fill, short, projected, reorder_level, replenish_qty, reason]
    - name: cf-agent
      level: 2
      called_when: a dealer is short or below its reorder level, and on every run for the C&F's own stock check
      reads: summary cf
      returns: [order_id, product, requested, can_dispatch, short, projected, reorder_point, replenish_qty, batch, expiry, reason]
    - name: manufacturer-agent
      level: 3
      called_when: a C&F is short or below its reorder point
      reads: summary manufacturer
      returns: [order_id, product, requested, manufacturer, capacity, batch_qty, ready_date, value, reason]
order_status: [draft, approved, auto_approved, filled, partial, rejected]
expected_results:   # as of 2026-01-01, approval_mode threshold
  pharmacy_orders: 10 (City Care Pharmacy 5, Apollo Local 2, MediMart 2, HealthPlus Pharmacy 1, Wellness Drugs 0)
  level_1: all 10 filled by dealers, all auto-approved (each under 50,000)
  dealer_reorder: none (no dealer falls below its reorder level)
  level_3: CF Pune Azithromycin 500mg is below its reorder point (8,013 < 8,952), requests 10,000 from Sun Pharma, value 70,700, needs a person, ready 2026-01-10
  what_if_level_2: manual order of 1,000 Amlodipine 5mg at City Care Pharmacy, Dealer East fills 829, short 171, requests 1,600 from CF Pune, CF Pune fills
```

## 1. Architecture

```
People (browser, logged in)          Agents (Claude Code, API tokens)
---------------------------          --------------------------------
/pharmacy      pharmacist  <------>   main agent  (coordinator)
/dealer        dealer mgr  <------>     |-- dealer-agent        (subagent 1)
/cf            C&F manager <------>     |-- cf-agent            (subagent 2)
/manufacturer  planner     <------>     |-- manufacturer-agent  (subagent 3)

     one Flask app + one SQLite database, loaded from the workbook
```

The rules this design rests on:

- **Each page has an API twin.** An agent reads the JSON behind its page (`/api/<role>/summary?org=<name>`), never the HTML. The data is the same as what the person sees, but reading it is reliable.
- **Subagents are read-only.** They read their own role's summary and return a JSON verdict to the main agent. Only the main agent writes, using the coordinator token.
- **Only people approve.** Every approve button works only for a logged-in human session. Agent tokens get `403`. Orders at or under the value limit are auto-approved by the server's policy, never by an agent.
- **Subagents cannot call each other.** The main agent relays: dealer first, C&F only when a dealer needs stock, manufacturer only when a C&F needs stock.
- **Every level checks two things:** can it fill what was ordered, and is its own stock still above its reorder rule afterwards. Either one can send a request up the chain.

## 2. Page and data map: where each agent goes

| Agent | Same data as page | Reads via | What it looks at | Returns to main agent |
|---|---|---|---|---|
| Main agent | `/pharmacy` | `python3 scripts/agent_api.py summary pharmacy --org "<name>"` | Pharmacy stock, last 28 days of sales, order history, open orders | (it is the main agent) |
| dealer-agent | `/dealer` | `python3 scripts/agent_api.py summary dealer --org "<name>"` | Stock on hand, reorder level, incoming pharmacy orders, C&F orders and their status | Fill or short per line, plus a replenishment quantity |
| cf-agent | `/cf` | `python3 scripts/agent_api.py summary cf --org "<name>"` | Stock, batch, expiry, average daily outflow, reorder point, incoming dealer requests | Dispatch or short per line, plus a replenishment quantity |
| manufacturer-agent | `/manufacturer` | `python3 scripts/agent_api.py summary manufacturer --org "<name>"` | Production capacity, unit cost, lead times, incoming C&F requests | A production plan with quantity, ready date and value |

Each agent may read only its own role. A hook enforces this (section 10).

## 3. Project layout

Create this layout, in the folder the user names or `pharma-chain/` by default:

```
pharma-chain/
|-- CLAUDE.md                     # tells the main agent to use this skill
|-- requirements.txt              # flask, requests, pandas, openpyxl
|-- .env.agents                   # demo API tokens (generated by seed.py)
|-- app/
|   |-- app.py                    # Flask app: UI routes + API routes
|   |-- seed.py                   # loads the workbook into data/app.db + tokens
|   |-- rules.py                  # the reorder and approval rules (section 6)
|   |-- templates/
|   |   |-- base.html  login.html
|   |   |-- pharmacy.html  dealer.html  cf.html  manufacturer.html
|   `-- static/style.css
|-- data/
|   |-- Pharmacy_SupplyChain_Large_Dataset.xlsx   # copied from the user's file, read-only
|   `-- app.db                    # SQLite, created by seed.py
|-- scripts/
|   |-- agent_api.py              # the only way agents talk to the app
|   `-- forecast.py               # deterministic pharmacy forecast
`-- .claude/
    |-- settings.json             # hook registration
    |-- agents/
    |   |-- dealer-agent.md
    |   |-- cf-agent.md
    |   `-- manufacturer-agent.md
    `-- hooks/
        |-- guard_agents.sh       # PreToolUse Bash
        |-- protect_files.sh      # PreToolUse Write|Edit
        `-- audit_log.sh          # PostToolUse *
```

Use Python 3 with Flask, SQLite, pandas and openpyxl only, so the app runs with `pip install -r requirements.txt` and nothing else.

## 4. The data source: Pharmacy_SupplyChain_Large_Dataset.xlsx

Ask the user where the workbook is if they have not said (it is usually in their Downloads folder). Copy it to `data/` and never change the original. If a sheet or column below is missing or named differently, stop and tell the user which one, rather than guessing.

| Sheet | Rows | Columns | Used for |
|---|---|---|---|
| Sales_History | 4,380 | Date, Pharmacy, Medicine, Quantity Sold | Pharmacy demand forecast; 2025-01-01 to 2025-12-31 |
| Pharmacy_to_Dealer | 3,000 | Order Date, Pharmacy, Dealer, Medicine, Quantity | Order history, preferred dealer, pharmacy stock estimate |
| Dealer_Inventory | 30 | Dealer, Medicine, Stock On Hand, Reorder Level | Dealer stock and reorder rule |
| Dealer_to_CF | 2,500 | Order Date, Dealer, CF Agent, Medicine, Quantity, Status | Preferred C&F, C&F daily outflow; Status is Pending, In Transit or Delivered |
| CF_Inventory | 20 | CF Agent, Medicine, Stock, Batch, Expiry Date | C&F stock, batch and expiry |
| CF_to_Manufacturer | 1,500 | Order Date, CF Agent, Manufacturer, Medicine, Quantity, Lead Time Days | Preferred manufacturer and lead time |
| Manufacturers | 40 | Manufacturer, Medicine, Production Capacity, Unit Cost | Capacity check, unit cost, order value |
| Settings | 2 | Setting, Value | Safety Stock Days (14), Max Order Value Without Approval (50,000) |

How the data behaves (checked against this file):

- Sales_History has 12 rows per day, and some pharmacy, medicine and date combinations appear more than once (453 repeats). Always **sum** quantities per pharmacy, medicine and date. A day with no row for a pair counts as 0 sales.
- Supply relationships are many-to-many: each pharmacy has ordered from all 3 dealers, each dealer from both C&F agents, each C&F agent from all 4 manufacturers. The **preferred supplier** for a pair and medicine is the one with the most past orders for that medicine; a tie goes to the larger total quantity, then alphabetical order.
- Dealer_to_CF statuses are a year of history. Show them on the dealer and C&F pages, but do not count old Pending or In Transit rows as incoming stock.
- Unit Cost is read as rupees. Production Capacity is the most a manufacturer can produce for one request.

What the workbook does not contain, and the assumption the app uses instead (show both on the pages as "assumption"):

| Missing | Assumption |
|---|---|
| Pharmacy stock on hand | Quantity of the pharmacy's latest order for that medicine (summed if several on that date) minus sales from that order date onwards, floored at 0 |
| Dealer to pharmacy lead time | 2 days (`pharmacy_lead_days`) |
| Price for pharmacy and dealer orders | Reference price = average Unit Cost of that medicine across the 4 manufacturers |
| Manufacturer finished stock | None; every C&F request becomes a production plan |

The as-of date for every calculation is the day after the last Sales_History date (2026-01-01 for this file), not today's date, so results are repeatable.

If the user later adds a `Pharmacy_Inventory` sheet (Pharmacy, Medicine, Stock On Hand), use it instead of the stock estimate and say so.

## 5. Database (SQLite) and seed (app/seed.py)

```
orgs(id, name, type)                                   -- 14 rows, type: pharmacy|dealer|cf|manufacturer
users(id, username, password_hash, org_id)             -- people only, one per org
agent_tokens(token, name, scope)                       -- scope: pharmacy|dealer|cf|manufacturer|coordinator
medicines(id, name, reference_price)
sales(pharmacy_org_id, medicine_id, sale_date, qty)    -- summed per day
pharmacy_stock(org_id, medicine_id, qty_on_hand, basis) -- basis: 'estimate' or 'sheet'
dealer_stock(org_id, medicine_id, qty_on_hand, reorder_level)
cf_stock(org_id, medicine_id, qty_on_hand, batch, expiry_date, avg_daily_out)
capacity(manufacturer_org_id, medicine_id, production_capacity, unit_cost)
history_pharmacy_orders(order_date, pharmacy_org_id, dealer_org_id, medicine_id, qty)
history_dealer_orders(order_date, dealer_org_id, cf_org_id, medicine_id, qty, status)
history_cf_orders(order_date, cf_org_id, manufacturer_org_id, medicine_id, qty, lead_time_days)
preferred_supplier(buyer_org_id, medicine_id, supplier_org_id)
lead_times(cf_org_id, manufacturer_org_id, medicine_id, avg_lead_days)
settings(key, value)
orders(id, from_org_id, to_org_id, medicine_id, qty, value, status, reason, source,
       parent_order_id, filled_qty, short_qty, created_by, approved_by, created_at, updated_at)
       -- source: agent | manual | own_check
production_plans(id, manufacturer_org_id, medicine_id, qty, value, ready_date, status,
                 reason, source_order_id, approved_by)
audit_log(ts, actor, actor_type, action, detail)       -- actor_type: human|agent|policy
```

`seed.py` steps:

1. Read every sheet with pandas. Check the sheet and column names in section 4.
2. Create the 14 orgs from the distinct names. Create one user per org with password `demo123` (hashed with `werkzeug.security`). Usernames:

| Org type | Username: org |
|---|---|
| Pharmacy | citycare: City Care Pharmacy · apollo: Apollo Local · healthplus: HealthPlus Pharmacy · medimart: MediMart · wellness: Wellness Drugs |
| Dealer | dealer-east · dealer-north · dealer-south |
| C&F | cf-pune · cf-mumbai |
| Manufacturer | sunpharma · cipla · drreddys · lupin |

3. Load the history sheets, summing Sales_History per pharmacy, medicine and date.
4. Compute and store: `reference_price`, `preferred_supplier` for every buyer and medicine, `lead_times` (mean Lead Time Days per C&F, manufacturer and medicine), `avg_daily_out` per C&F and medicine (sum of Dealer_to_CF quantity / 365), and `pharmacy_stock` (section 4).
5. Store the Settings sheet values, plus `pharmacy_lead_days=2`, `forecast_window_days=28`, `approval_mode=threshold`.
6. Generate five tokens with `secrets.token_urlsafe(24)`, store them in `agent_tokens` and write `.env.agents`:

```
APP_URL=http://127.0.0.1:5000
PHARMACY_AGENT_TOKEN=...
DEALER_AGENT_TOKEN=...
CF_AGENT_TOKEN=...
MANUFACTURER_AGENT_TOKEN=...
COORDINATOR_TOKEN=...
```

Re-running `seed.py` resets the demo: it deletes `data/app.db`, reloads the workbook and makes new tokens.

## 6. The rules (app/rules.py)

All maths lives in `rules.py` and `forecast.py`. Agents use the numbers; they never calculate in their heads. `D` = as-of date, `SAFETY` = Safety Stock Days.

### Pharmacy (forecast.py)

```
avg_daily  = sum(sales from D-28 to D-1) / 28
target     = avg_daily * (pharmacy_lead_days + SAFETY)          # 2 + 14 = 16 days
order_qty  = ceil(max(0, target - on_hand) / 10) * 10
days_left  = on_hand / avg_daily
```

The order goes to the pharmacy's preferred dealer for that medicine.

### Dealer (level 1)

```
can_fill   = min(ordered, stock_on_hand)
short      = ordered - can_fill
projected  = stock_on_hand - can_fill
replenish  = if short > 0 or projected < reorder_level:
                 ceil((short + 2 * reorder_level - projected) / 100) * 100
             else 0
```

The replenishment goes to the dealer's preferred C&F for that medicine. On every run, the dealer agent also checks all of the dealer's stock against its reorder levels (an own-check, `source=own_check`).

### C&F (level 2)

```
lead_days     = mean Lead Time Days to its preferred manufacturer for that medicine
reorder_point = avg_daily_out * (lead_days + SAFETY)
can_dispatch  = min(requested, stock)            # earliest-expiry batch first
short         = requested - can_dispatch
projected     = stock - can_dispatch
replenish     = if short > 0 or projected < reorder_point:
                    ceil((short + 2 * reorder_point - projected) / 1000) * 1000
                else 0
```

On every run, the C&F agent also checks all of its stock against its reorder points (own-check), and flags any batch expiring within 180 days of the as-of date.

### Manufacturer (level 3)

```
manufacturer = the C&F's preferred manufacturer for that medicine
if requested <= production_capacity: one plan for the full quantity
else: plan the capacity, and propose the rest to the next manufacturer
      for that medicine with spare capacity, cheapest unit cost first
ready_date   = D + ceil(lead_days)
value        = qty * that manufacturer's unit_cost
```

### Approval

```
value = qty * reference_price        (pharmacy, dealer and C&F orders)
value = qty * unit_cost              (C&F requests and production plans, using the chosen manufacturer)
approval_mode threshold: value <= 50,000 -> auto_approved by policy; above -> a person approves
approval_mode all:       every order and plan waits for a person
```

Auto-approval is done by the server when the draft is created, logged with `actor_type=policy`. An agent never approves. To show every login during a demo, set `approval_mode` to `all` on the settings line at the top of each page (visible to all users, changed only by a logged-in person).

## 7. The web app (app/app.py)

### Human routes (session cookie, Flask `SECRET_KEY` from env or random at start)

| Route | Who | Shows / does |
|---|---|---|
| `GET/POST /login` | anyone | Username + password form; the 14 demo accounts listed below it; redirect to the org's page |
| `GET /logout` | logged in | Clear session |
| `GET /pharmacy` | pharmacy users | Stock (marked "estimate"), days left, 28-day sales chart or table, draft orders with the agent's reason, value and Approve/Reject, a **manual order** form (medicine + quantity), order history |
| `GET /dealer` | dealer users | Stock vs reorder level, incoming pharmacy orders with fill status, draft replenishments to the C&F with Approve/Reject, recent C&F orders with status |
| `GET /cf` | C&F users | Stock, batch, expiry, days of cover vs reorder point, incoming dealer requests, draft requests to manufacturers with Approve/Reject |
| `GET /manufacturer` | manufacturer users | Capacity and unit cost per medicine, incoming C&F requests, draft production plans with Approve/Reject |
| `POST /orders/<id>/approve`, `/reject` | a person at the order's `from_org_id` | Change status, set `approved_by`, write audit log |
| `POST /plans/<id>/approve`, `/reject` | a person at the plan's manufacturer | Same for production plans |
| `POST /orders/manual` | pharmacy users | Create a manual draft order to the preferred dealer (`source=manual`) |
| `POST /settings/approval_mode` | any logged-in person | Switch threshold / all |

Each page shows only the logged-in user's own organisation. It checks the user's org type matches the page, otherwise it redirects to that user's own page. Approve, reject, manual-order and settings routes accept only a logged-in human session, never a Bearer token.

Each page header shows the org name, the role, a "chain" strip (Pharmacy > Dealer > C&F > Manufacturer, current one highlighted), the as-of date and Logout. Each page has an "Agent activity" panel with the last 10 audit-log rows for that org. Show money as rupees with thousands separators. Keep the design plain and readable: one CSS file, tables, status pills that also carry the status word.

How `app.py` starts (end of the file):

```python
if __name__ == "__main__":
    import sys
    share = "--share" in sys.argv
    host = "0.0.0.0" if share else "127.0.0.1"
    if share:
        print("SHARING ON: devices on this Wi-Fi can open the app. Press Ctrl+C when the demo is done.")
    app.run(host=host, port=5000, debug=False)
```

`debug` is always `False`. Never add a way to turn it on together with `--share`.

### Agent API routes (header `Authorization: Bearer <token>`)

| Route | Allowed scope | Returns / does |
|---|---|---|
| `GET /api/<role>/summary?org=<name>` | that role's token, nothing else | The same data as that org's page, as JSON |
| `GET /api/orders?status=&level=` | coordinator | Orders filtered by status and level (pharmacy, dealer, cf) |
| `POST /api/orders` | coordinator | Create a draft order `{from_org, to_org, medicine, qty, reason, source, parent_order_id}`; the server computes `value` and applies the approval rule |
| `POST /api/orders/<id>/result` | coordinator | Record `{filled_qty, short_qty, note}`; set `filled` or `partial`; move stock from supplier to buyer for `filled_qty` |
| `POST /api/plans` | coordinator | Create a draft production plan; the server computes `value` and applies the approval rule |

Any approve attempt with a token returns `403 {"error":"Approvals need a logged-in person"}`. A role token used on another role's summary returns `403`. Every API call writes an audit-log row with `actor_type=agent` and the token's name.

## 8. Scripts

### scripts/agent_api.py

This script is the only way agents talk to the app. It reads `.env.agents`, picks the token by command, and prints JSON. It never prints the tokens.

```
python3 scripts/agent_api.py summary <pharmacy|dealer|cf|manufacturer> --org "<name>"   # that role's token
python3 scripts/agent_api.py orgs <pharmacy|dealer|cf|manufacturer>                     # list org names
python3 scripts/agent_api.py orders --status approved --level dealer                    # coordinator
python3 scripts/agent_api.py create-order --json '{...}'                                 # coordinator
python3 scripts/agent_api.py record-result <order_id> --filled N --short M               # coordinator
python3 scripts/agent_api.py create-plan --json '{...}'                                  # coordinator
```

### scripts/forecast.py

Input: one pharmacy summary JSON on stdin. Output: JSON order lines `{medicine, order_qty, avg_daily, on_hand, days_left, target, dealer}` for every medicine with `order_qty > 0`, using the pharmacy rule in section 6.

## 9. Subagent files

Write each file in `.claude/agents/`. They share one shape; only the role, page and decision rules change.

### .claude/agents/dealer-agent.md

```markdown
---
name: dealer-agent
description: Checks a dealer's stock for approved pharmacy orders and its own reorder levels, and reports fill, shortfall and replenishment per line. Use for level 1 of the replenishment chain.
tools: Bash, Read
disallowedTools: Write, Edit
model: sonnet
---
You act for one dealer (Dealer East, Dealer North or Dealer South). You read; you never write or approve.

1. Run `python3 scripts/agent_api.py summary dealer --org "<dealer>"` for the dealer the main agent named. This is the data on the /dealer page.
2. For each order ID the main agent gave you, apply the dealer rule from the pharma-supply-chain-agents skill (section 6): can_fill, short, projected, replenish.
3. Then check every medicine the dealer holds against its reorder level (own-check).
4. Return only this JSON:
   {"orders": [{"order_id": 0, "medicine": "", "ordered": 0, "can_fill": 0, "short": 0,
                "projected": 0, "reorder_level": 0, "replenish_qty": 0, "cf": "", "reason": ""}],
    "own_check": [{"medicine": "", "stock": 0, "reorder_level": 0, "replenish_qty": 0, "cf": "", "reason": ""}]}
   Each reason is one sentence a dealer manager can act on.

Never read another role's summary, never create or change orders, never approve.
```

### .claude/agents/cf-agent.md

This file has the same shape. Set `name: cf-agent`, act for one C&F agent (CF Pune or CF Mumbai), read `summary cf --org "<name>"`, and handle level 2. Apply the C&F rule from section 6 to each approved dealer request, dispatching from the earliest-expiry batch, and run the own-check on all its stock. Flag batches expiring within 180 days. If several dealers want the same short medicine, split it in proportion to their requests and say so in `reason`. Return `{"orders": [{"order_id", "medicine", "requested", "can_dispatch", "short", "projected", "reorder_point", "replenish_qty", "manufacturer", "batch", "expiry", "reason"}], "own_check": [{"medicine", "stock", "reorder_point", "days_of_cover", "replenish_qty", "manufacturer", "reason"}]}`.

### .claude/agents/manufacturer-agent.md

This file has the same shape. Set `name: manufacturer-agent`, act for one manufacturer (Sun Pharma, Cipla, Dr Reddys or Lupin), read `summary manufacturer --org "<name>"`, and handle level 3. Apply the manufacturer rule from section 6 to each approved C&F request: check capacity, set the ready date from the lead time, compute the value at this manufacturer's unit cost, and if capacity is not enough, name the next manufacturer to ask. Return `[{"order_id", "medicine", "requested", "manufacturer", "capacity", "batch_qty", "ready_date", "unit_cost", "value", "overflow_to", "reason"}]`.

## 10. Hooks (guardrails in code)

### .claude/settings.json

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [{ "type": "command", "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard_agents.sh" }] },
      { "matcher": "Write|Edit", "hooks": [{ "type": "command", "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/protect_files.sh" }] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/audit_log.sh" }] }
    ]
  }
}
```

### .claude/hooks/guard_agents.sh

Inside a subagent, the hook input carries `agent_type` (the subagent's name). In the main agent, it is absent.

```bash
#!/bin/bash
input=$(cat)
cmd=$(jq -r '.tool_input.command // ""' <<<"$input")
agent=$(jq -r '.agent_type // "main"' <<<"$input")

# Nobody approves or changes settings through the shell, and nobody reads tokens or touches the database directly.
if grep -qE '/approve|/reject|/settings/|/orders/manual' <<<"$cmd"; then
  echo "Blocked: approvals, manual orders and settings are done by a logged-in person in the web app." >&2; exit 2
fi
if grep -qE '\.env\.agents|sqlite3|app\.db' <<<"$cmd"; then
  echo "Blocked: agents use scripts/agent_api.py only." >&2; exit 2
fi

if grep -q 'agent_api.py' <<<"$cmd"; then
  case "$agent" in
    dealer-agent)       allowed="summary dealer" ;;
    cf-agent)           allowed="summary cf" ;;
    manufacturer-agent) allowed="summary manufacturer" ;;
    main)               allowed="" ;;
    *)                  echo "Blocked: unknown agent $agent." >&2; exit 2 ;;
  esac
  if [ -n "$allowed" ] && ! grep -q "agent_api.py $allowed" <<<"$cmd"; then
    echo "Blocked: $agent may only run '$allowed' (read-only, its own page)." >&2; exit 2
  fi
  if [ "$agent" = "main" ] && grep -qE 'summary (dealer|cf|manufacturer)' <<<"$cmd"; then
    echo "Blocked: the main agent delegates other roles' data to their subagents." >&2; exit 2
  fi
fi
exit 0
```

### .claude/hooks/protect_files.sh

```bash
#!/bin/bash
path=$(jq -r '.tool_input.file_path // ""')
if [[ "$path" == *app.db || "$path" == *.env.agents || "$path" == *.xlsx ]]; then
  echo "Blocked: the database, tokens and source workbook are never edited by an agent." >&2; exit 2
fi
exit 0
```

Apply this hook only after the build is done. During the build, `seed.py` creates the database and tokens by running, not by Write, and the workbook is copied with a shell command.

### .claude/hooks/audit_log.sh

```bash
#!/bin/bash
mkdir -p "${CLAUDE_PROJECT_DIR}/data"
jq -c '{ts: (now | todate), agent: (.agent_type // "main"), tool: .tool_name, input: .tool_input}' \
  >> "${CLAUDE_PROJECT_DIR}/data/agent_audit.log"
exit 0
```

The hooks need `jq`. On Windows, Claude Code runs them through Git Bash; install `jq` with `winget install jqlang.jq`. `chmod +x .claude/hooks/*.sh` after writing them.

There are two locks on every approval: the hook blocks approve calls from the shell, and the server refuses approvals from tokens. Say this when demoing.

## 11. Build procedure

1. Ask where to create the project and where the workbook is, if the user has not said. Default project: `pharma-chain/` in the current folder.
2. Copy the workbook to `data/`. Check its 8 sheets and columns against section 4; stop and report any mismatch.
3. Write `requirements.txt`, `app/app.py`, `app/rules.py`, `app/seed.py`, the templates and CSS, and both scripts.
4. Run `pip install -r requirements.txt` and `python3 app/seed.py`.
5. Write the three subagent files, the hooks and `settings.json`, then `chmod +x` the hooks.
6. Write `CLAUDE.md` with one line: "For replenishment, follow the pharma-supply-chain-agents skill, section 12."
7. Start the app in the background: `python3 app/app.py` (port 5000, local only; never `--share` during the build).
8. Verify before saying it works:
   - `curl -s localhost:5000/login` returns the login page.
   - `agent_api.py orgs pharmacy` lists the 5 pharmacies.
   - Calling `/api/dealer/summary` with the pharmacy token returns 403.
   - Running the forecast for all 5 pharmacies returns 10 order lines (City Care Pharmacy 5, Apollo Local 2, MediMart 2, HealthPlus Pharmacy 1, Wellness Drugs 0), matching section 14.
   - The C&F own-check flags only CF Pune Azithromycin 500mg (8,013 below 8,952).
   - `curl -X POST -H "Authorization: Bearer <coordinator>" localhost:5000/orders/1/approve` returns 403.
   - Pipe sample JSON into each hook: a subagent reading another role exits 2, and an approve command exits 2.
9. Report what was built, the demo logins, the assumptions in section 4, and how to start the demo (section 12). Mention that the hooks become active the next time Claude Code is opened in the project folder.

## 12. Run procedure (you are the main agent)

When the user says "run replenishment" (for all pharmacies, or one they name):

1. For each pharmacy, run `agent_api.py summary pharmacy --org "<name>" | python3 scripts/forecast.py`.
2. For each line, write a one-sentence reason from the numbers, e.g. "Azithromycin 500mg: 0 in stock, selling 3.6 a day, 16 days of cover needed."
3. Create one draft order per line to the preferred dealer with `create-order` (`source=agent`). The server values each order and auto-approves it if it is at or under 50,000.
4. Tell the user what was created and which orders, if any, wait for a person (with the login to use).

When the user says "continue replenishment", work through whichever level has approved work. Repeat until nothing is waiting.

- **Level 1:** group approved pharmacy orders without a result by dealer. Delegate each group to **dealer-agent** with the dealer name and order IDs. Record each line with `record-result`. For each `replenish_qty > 0` (from orders or the own-check), create a draft order dealer to C&F with `parent_order_id` (when there is one) and the reason.
- **Level 2:** group approved dealer orders without a result by C&F. Delegate to **cf-agent**, and ask it for its own-check on every run even when no dealer request is waiting. Record results, and create draft C&F to manufacturer requests for each `replenish_qty > 0`.
- **Level 3:** group approved C&F requests without a result by manufacturer. Delegate to **manufacturer-agent**. Create a draft production plan per line; if `overflow_to` is set, create a second request to that manufacturer.

After each level, tell the user which drafts were auto-approved and which wait for a person, and the login to use. When nothing is waiting, give one summary table: medicine, from, to, quantity, level reached, status, and ready date.

Rules while running:

- Never approve, never read another role's summary yourself, never calculate quantities in your head (the scripts and the subagents' numbers are the facts).
- If a hook blocks you, say what it blocked and why. Never look for a way around it.
- Subagents cannot call each other. You always carry the request to the next level.

## 13. Demo script (about 10 minutes)

1. Show the login page: 14 organisations, one app.
2. Log in as **citycare**. Point out the stock estimate and that Azithromycin 500mg is at 0.
3. Say "run replenishment". 10 orders appear across the 5 pharmacies, 5 of them at City Care. All are under 50,000, so policy auto-approves them: small routine orders flow without a click.
4. Say "continue replenishment". The dealer agents fill all 10. No dealer falls below its reorder level.
5. The C&F agent's own-check finds CF Pune Azithromycin 500mg below its reorder point (8,013 vs 8,952, about 20 days of cover) and drafts a 10,000-unit request to Sun Pharma, worth 70,700. That is above the limit, so log in as **cf-pune** and approve it.
6. Continue. The manufacturer agent plans the batch at Sun Pharma (capacity 66,658), ready 2026-01-10, value 70,700. Log in as **sunpharma** and approve.
7. **What-if for level 2:** log in as **citycare** and place a manual order for 1,000 Amlodipine 5mg (for example a bulk clinic order). Continue replenishment. Dealer East fills 829, is short 171, and requests 1,600 from CF Pune, which fills it.
8. Ask the main agent to "approve the Sun Pharma batch yourself". The hook blocks it.
9. Open each page's Agent activity panel and `data/agent_audit.log`. Every step is traced, including which steps were auto-approved by policy.

The message to land: the agents forecast, check and escalate from real history; routine orders flow automatically under the limit; a person at each business approves anything bigger; the rules are enforced in code.

## 14. Expected results with this workbook

As of 2026-01-01, `approval_mode: threshold`. If the build gives different numbers, check the rules in section 6 before changing anything.

| Pharmacy | Medicine | Stock (estimate) | Avg/day | Order | Dealer | Dealer stock | Filled | Value (Rs) |
|---|---|---|---|---|---|---|---|---|
| Apollo Local | Amlodipine 5mg | 64 | 4.07 | 10 | Dealer East | 829 | 10 | 56 |
| Apollo Local | Cetirizine 10mg | 113 | 11.79 | 80 | Dealer South | 2,907 | 80 | 1,151 |
| City Care Pharmacy | Amoxicillin 250mg | 142 | 9.89 | 20 | Dealer South | 1,923 | 20 | 233 |
| City Care Pharmacy | Azithromycin 500mg | 0 | 3.57 | 60 | Dealer North | 3,928 | 60 | 627 |
| City Care Pharmacy | Ibuprofen 400mg | 93 | 10.36 | 80 | Dealer East | 2,991 | 80 | 515 |
| City Care Pharmacy | Omeprazole 20mg | 38 | 5.29 | 50 | Dealer East | 4,785 | 50 | 658 |
| City Care Pharmacy | Paracetamol 500mg | 38 | 4.07 | 30 | Dealer East | 4,311 | 30 | 290 |
| HealthPlus Pharmacy | Cetirizine 10mg | 0 | 15.29 | 250 | Dealer South | 2,827 | 250 | 3,598 |
| MediMart | Cetirizine 10mg | 108 | 8.14 | 30 | Dealer North | 3,705 | 30 | 432 |
| MediMart | Ibuprofen 400mg | 92 | 7.11 | 30 | Dealer North | 4,577 | 30 | 193 |

Dealer stock is shown before the order, after earlier orders in this list (HealthPlus Cetirizine sees 2,827 because Apollo's 80 came first). Process orders in the order of this table: pharmacy, then medicine, alphabetical.

- **Dealers:** no dealer goes below its reorder level, so no dealer replenishment.
- **C&F own-check:** CF Pune Azithromycin 500mg, stock 8,013, average outflow 395.9 a day, lead time 8.61 days to Sun Pharma, reorder point 8,952. Request 10,000 from Sun Pharma.
- **Manufacturer:** Sun Pharma, capacity 66,658, unit cost 7.07, value 70,700 (needs a person), ready 2026-01-10.
- **What-if:** manual 1,000 Amlodipine 5mg at City Care Pharmacy goes to Dealer East (stock 829, reorder level 685): fills 829, short 171, requests 1,600 from CF Pune (stock 49,442, stays above its reorder point of 9,523). Values 5,575 and 8,920, both auto-approved.

## 15. Sharing the demo

By default the app runs on localhost (`http://127.0.0.1:5000`), so only the laptop it runs on can open it. Sharing the screen is the simplest and safest way to demo.

- **In the same room, on the same Wi-Fi:** start the app with `python app/app.py --share`. This sets `host="0.0.0.0"`. Others can then open `http://<your-laptop-IP>:5000`. On Windows, find the IP with `ipconfig` (the IPv4 Address under the Wi-Fi adapter), and when Windows asks to allow Python through the firewall, tick **Private** only.
- **Over the internet:** deploy it to a hosting service, or use a tunnel tool like ngrok or Cloudflare Tunnel for a temporary link. With demo passwords like `demo123`, only do this briefly, and never with real data.

Use `--share` only when the user asks to share the demo. A plain `python app/app.py` always starts on `127.0.0.1`, so there is no line to edit or forget to change back.

### Safety rules for sharing

- Share only on a trusted network (home or office Wi-Fi), never on public Wi-Fi (cafe, airport, hotel, conference).
- `debug` stays `False`. Debug mode has a console that can run commands on the laptop.
- Synthetic data only.
- Sharing the screen while the app runs on `127.0.0.1` is the safest option and the default suggestion.

### After the demo: close access

1. In the terminal running the app, press **Ctrl + C**. A stopped app cannot be reached by anyone. If the terminal was closed, end any **Python** process in Task Manager.
2. Start it again without `--share` when needed: `python app/app.py` (local only).
3. Remove the firewall permission: Start > "Allow an app through Windows Firewall" > Change settings > find **Python** > untick Private and Public (or Remove) > OK.
4. Check: run `netstat -ano | findstr :5000` while the app runs. `127.0.0.1:5000` means local only; `0.0.0.0:5000` means still shared. As a final test, `http://<your-laptop-IP>:5000` on a phone on the same Wi-Fi should fail to load.

Remind the user of steps 1 and 3 whenever a `--share` demo ends.

## 16. Production notes (for the "how it scales" slide)

- Replace demo logins with real authentication (SSO or OAuth) and per-user roles.
- Each business runs its own agent against its own system. The coordinator passes orders through APIs or MCP connectors instead of one shared database.
- The pharmacy side connects to the real billing system's API, or to scheduled export and import files, which also supplies real pharmacy stock instead of the estimate.
- Tokens move to a secrets manager. Approval rules (limits, second approver) live in the server, with the hooks as a second lock.
