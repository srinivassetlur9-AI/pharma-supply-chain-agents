# Run steps: pharma supply-chain demo

All commands run from the project folder `pharma-supply-chain-agents` (not from `app/`).
Passwords for every demo login: `demo123` (see `Demo_Logins.xlsx` for all 14 logins).

## 1. One-time setup

```
pip install -r requirements.txt
python app/seed.py
```

`seed.py` loads `data/Pharmacy_SupplyChain_Large_Dataset.xlsx` into the database, creates the 14 logins and writes `.env.agents` (demo agent tokens). The jq tool is needed for the Claude Code hooks: `winget install jqlang.jq`.

## 2. Reset before every demo

```
python app/seed.py
```

This wipes all orders and plans and starts again at the as-of date 2026-01-01. Do it before each run, and never run the orchestrator with `--forecast` twice on the same data (it would create duplicate orders).

## 3. Start the app (terminal 1)

```
python app/app.py
```

Open http://127.0.0.1:5000. Stop it with Ctrl+C. If it says the address is already in use, an old copy is running: stop it in Task Manager (end the Python process) or in the terminal where it is running.

## 4. Open the logins

One browser holds one login, so use different browsers, or normal and private windows.

| Window | Login | Page |
|---|---|---|
| 1 | `citycare` | /pharmacy |
| 2 | `dealer-east` (or dealer-north) | /dealer |
| 3 | `cf-pune` | /cf |
| 4 | `sunpharma` | /manufacturer |

## 5. Start the agents (terminal 2)

```
python scripts/orchestrator.py --forecast --pace=2
```

- `--forecast` creates the pharmacy orders first (use it once per reset).
- `--pace=2` pauses 2 seconds between steps so the audience can follow.
- Without `--forecast` it only watches and moves approved work up the chain.
- `--claude` hands the work to Claude Code instead (`claude -p "continue replenishment"`). It is untested and uses tokens.

## 6. What you will see

1. **Pharmacy orders.** The coordinator creates 10 orders across the 5 pharmacies. Each is under Rs 50,000, so policy auto-approves it. Nobody clicks.
2. **Level 1, dealers.** `DEALER-AGENT woke up for Dealer East / North / South` and fills each order. No dealer needs restocking. Check the dealer page: "Orders received" shows what the dealer-agent filled.
3. **Level 2, C&F.** CF-AGENT finds CF Pune's Azithromycin 500mg (8,013) below its reorder point (8,952) and drafts a request of 10,000 to Sun Pharma, worth Rs 70,700. It is over the Rs 50,000 limit, so the orchestrator prints "Waiting for a person".
4. **Approve (person 1).** Log in as `cf-pune`, open /cf, click **Approve** on the draft.
5. **Level 3, manufacturer.** Within about 5 seconds MANUFACTURER-AGENT drafts a batch plan: 10,000 units, ready 2026-01-10, Rs 70,700.
6. **Approve (person 2).** Log in as `sunpharma`, open /manufacturer, click **Approve** on the plan.
7. **Result.** The CF Pune page now shows "Filled by manufacturer-agent: Sun Pharma planned 10,000 units, ready 2026-01-10. Plan #1 approved by sunpharma."

## 7. Optional what-if: shortfall at a dealer

Log in as `citycare` and place a manual order of 1,000 Amlodipine 5mg. In the full demo, Dealer East has 819 left after Apollo's order, so the dealer-agent fills 819 and is 181 short. It then requests 1,600 from CF Pune (about Rs 8,900, auto-approved), and the cf-agent fills it. No clicks needed.

## 8. Show the guardrails

- **Only people approve.** In Claude Code, ask: "approve the Sun Pharma batch yourself". The hook blocks it. A token calling an approve URL gets 403.
- **Agents see only their own role's data.** A pharmacy token reading the dealer summary gets 403.
- **Every step is traced.** Each page has an "Agent activity" panel. The agent log is `data/agent_audit.log` (written by the Claude Code hooks).
- To make every order wait for a person, use the approval-mode switch in the page header ("Switch to all").

## 9. Who approves what

| Draft | Created by | Approved by login |
|---|---|---|
| Pharmacy order (small) | coordinator | auto, by policy |
| Dealer restock to C&F (over limit) | dealer-agent | the dealer's login, e.g. `dealer-east` |
| C&F request to manufacturer (over limit) | cf-agent | `cf-pune` / `cf-mumbai` |
| Production plan (over limit) | manufacturer-agent | `sunpharma` / `cipla` / `drreddys` / `lupin` |

The buyer approves its own spending. The seller's agent just checks stock and fills.

## 10. Stop and clean up

- Ctrl+C in both terminals.
- If you shared the app with `python app/app.py --share`, stop it and remove the Windows firewall permission for Python (see section 15 of `SKILL.md`).

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| `No such file or directory` for `app/app.py` | You are inside `app/`. Run `cd ..` or use `python app.py`. |
| Address already in use | An old copy of the app is running. Stop it, then start again. |
| Orchestrator shows `Problem:` and keeps going | Usually the app was restarted or two orchestrators are running. Keep only one terminal running it. |
| Pages look stale after a code change | Restart the app (Ctrl+C, then `python app/app.py`). |
| Nothing happens after approving | Check that the orchestrator is still running in terminal 2. |
| Login keeps switching users | Two logins in the same browser share one session. Use another browser or a private window. |
