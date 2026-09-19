# The 3-minute demo

Setup: `./run.sh` → http://localhost:8642. The clock is pinned to Monday 09:15 IST;
yesterday was a soft Sunday. Everything below is live data — nothing is mocked in the UI.

## 0. The Monday-morning moment (15s)
The dashboard opens on **Sharma Tea Stall**: 30-day GMV ₹27,300 · repeat 22% · 480 regulars.
The signal feed already shows three fires from the nightly scan:
- **Yesterday (Sun) sales drop 18%** — severity 1.00
- **212 regulars haven't been back in 45+ days** — 0.71
- **Repeat rate −17pp vs 20 peer tea stalls nearby** — 0.67

*Say:* "Paytm soundbox already knows the shop. Vyapaar AI is the layer that acts on it."

## 1. Ask, in Hinglish (30s)
Click 🎙 (or type) **"कल बिक्री कम क्यों लगी?"** — watch the tool calls stream in:
brief → **detect_anomalies** ("Sun: −18% vs 4-Sun baseline, z=−8") → **list_lapsed_customers**
(212) → **estimate_impact** → **request_approval**. Then the answer:

> *Sun ki bikri ₹8,200 thi, pichhle 4 Sun ka average ₹10,000 — 18% kam. Mot wajah: 212
> regular customer 45+ din se nahi aaye. Wapas lane ka plan: WhatsApp par 10% chhoot
> (max ₹20). Anumaan: ₹4,800–₹6,500 extra bikri, kharcha cap ₹1,100.*

*Say:* "Every number came from a tool — the model never does arithmetic on money."
Open the **audit drawer** (bottom right): the trace shows each prompt, tool call and
result, timestamped.

## 2. One-tap approve (20s)
The prescription card: audience 212, offer 10% (max ₹20), band **₹4,800–₹6,500**,
cap **₹1,100**, min ROI **3.78×**. Tap **✓ Bhejo**.
The connector sends (≈202 delivered, some fail — an honest funnel), cost booked,
and the tracker freezes two arms: **212 treated + 150 matched controls** who are
never messaged.

*Say:* "The merchant owns the money. Nothing sends without this tap — and the caps
(spend, frequency, quiet hours 21:30–08:00, audience ≤1000) are enforced in code,
not in a prompt."

## 3. Let a week pass (30s)
Tap **⏩ Advance 7 days** (labelled sandbox control — in production, time does this).
The world happens: 31 of the messaged customers come back, ₹7,287 of GMV; 4 of the
150 controls return organically — that's the counterfactual. The measure job runs and
the tracker closes the loop:

> **7-day result (diff-in-diff): ₹6,100 · 31 redemptions · realized cost ₹1,044 · ROI 4.84×**

*Say:* "Not 'sales after the campaign' — the difference against what comparable
never-messaged customers did. That is the number a merchant can trust."

## 4. The report card (25s)
Tap **🧾 Weekly report card**:
- **6 actions run** (5 prior + this one; prior runs labelled `seeded_history`, never blended silently)
- **₹21,400 recovered revenue** — ₹6,100 measured live + ₹15,300 track record
- **Repeat rate 22% → 31%** — 66 of the 212 messaged customers came back within 30 days,
  against the pre-campaign merchant baseline (tap **⏩ Advance** again then **📊 Measure**
  if you want the 30-day figure live)

## 5. The guardrails bite (20s) — the trust close
Point at the guardrail chips (spend this week vs ₹5,000 cap, campaigns 1/2). If asked:
the policy engine demonstrably refuses — `python -m eval.verify_demo` includes a case
where a ₹10,000 blast to 1,000 people is blocked with named violations (spend_cap_run,
spend_cap_week). Quiet hours queue the send; they never block a draft.

## If a judge asks…
- **"Is the AI real?"** — `VYAPAAR_LLM=claude` flips the brain to `claude-opus-5` with the
  same tools, guardrails and audit log (needs `ANTHROPIC_API_KEY` in `.env`). The scripted
  planner is the offline fallback — the same principle #5 every model in the stack has.
- **"Are the numbers real?"** — `python -m eval.verify_demo`: 40 assertions, from the
  ledger, through the live query path. Any drift fails loudly.
- **"Why the 150 controls?"** — matched mid-lapsed regulars (31–44 days silent): comparable
  people the campaign never touched. The diff-in-diff subtracts their organic return,
  scaled per head, from the treated arm's lift.
- **"Where's the PII?"** — tokenized (`cust_…`) at ingest; phones live in a vault only the
  connector reads at send time; the audit log shows masked numbers only.
- **"What ships first?"** — exactly this loop on one vertical (tea stalls/kirana) in one
  pincode; the schemas, tool contracts and state machine are already production-shaped.
