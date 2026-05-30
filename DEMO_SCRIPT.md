# The 11 PM Ops Brief — 3-Minute Demo Script

**Submission:** Databricks DNB Customer Hackathon · **Track 1 [Technical]** — *Build an agent on apps with Genie under the hood*

**App URL:** https://ops-brief-apatel-7474650842988229.aws.databricksapps.com
**Workspace:** `dnb-hackathon-west-2`
**Target length:** 3:00 (hard ceiling 3:15)

---

## Before you record — 5-minute pre-flight (do not skip)

1. **Warm the app.** Open the App URL in Chrome. If it shows a Databricks login screen, sign in once so the SSO cookie is fresh. Then hit refresh — you should land on the 4-pane operational console.
2. **Generate one practice brief.** Click **"Generate tonight's full ops brief"** and let it finish. This warms the model cache, the MCP discovery, and the Lakebase fallback path — so during the real recording, the brief takes ~25–40s instead of the cold-start ~60s.
3. **Open MLflow in a second tab.** Workspace UI → Experiments → search `4469475842996134` (the Ops Brief experiment). Click into the most recent trace so it's ready to flip to at 2:30.
4. **Close everything else.** Slack, email, notifications. Quiet recording, no popups.
5. **Browser zoom:** Cmd+0 to reset, then Cmd+Plus once (110%) so the panel labels are readable in 1080p.
6. **Mic check:** record 10 seconds and play back. If the room sounds boomy, throw a sweater over your monitor.

---

## The script

> Two columns: **what you SAY** (verbatim, you can paraphrase but stay tight) and **what you DO** (where the mouse goes, what you click). Time markers are when each beat should START — if you slip more than 10s, cut the next "optional" line.

### [0:00 → 0:25] Hook — the problem (25s)

| What you say | What you do |
|---|---|
| *"It's 11 PM. The supply-chain ops director just went home. Overnight there's severe weather in Memphis, congestion at the Port of Long Beach, an FDA recall, a wildcat strike, and forty-five hundred packages mid-flight. By 7 AM tomorrow, the SLAs are either saved — or already broken. Today, that whole job is one human at home, checking fourteen dashboards on a phone."* | Title card on screen for the first 3-5 seconds (just text: **"The 11 PM Ops Brief"**), then cut to the **Chrome tab on the App URL**, full-screen, showing the 4-pane operational console. **Do not click anything yet.** Let the viewer see the layout — 17 signal tiles on the left, brief pane center, action queue right, agent timeline bottom. |

### [0:25 → 0:55] App tour + kick off the brief (30s)

| What you say | What you do |
|---|---|
| *"This is The 11 PM Ops Brief — a multi-agent supply-chain operations system running as a Databricks App. Four panes. Left rail: seventeen live signal sources from public agencies — NWS, IMF PortWatch, USGS earthquakes, FAA, CBP border waits, openFDA recalls, EIA fuel prices. They refresh every sixty seconds. Center: the brief composition pane. Right: the action queue. Bottom: the agent timeline. Let me kick off tonight's brief and walk you through what's happening under the hood."* | Mouse moves slowly across the left rail (visibly hover 2-3 tiles so the viewer sees the status dot + count). Then move to the center pane and click **"Generate tonight's full ops brief"** at the **0:50 mark exactly.** This starts the agent — it will run for ~25-40s while you keep narrating. |

### [0:55 → 1:35] Genie under the hood + multi-agent fan-out (40s)

| What you say | What you do |
|---|---|
| *"Watch the agent timeline at the bottom. The Supervisor — that's the LangGraph supervisor node — just produced a Pydantic-typed plan. It's now dispatching five subagents in parallel. The DataAnalyst is the key one: it does not write SQL directly. It hits a **managed Genie MCP server** — that's Genie under the hood — and Genie is the only data path into our Unity Catalog warehouse. The SignalScout is fanning out to those seventeen external APIs. The Historian is recalling past briefs from a Vector Search index. And the Composer — when it runs — will call three **Unity Catalog AI Functions** I've registered: governed SQL UDFs powered by `ai_classify` and `ai_query`, lineage and ACLs flow through Unity Catalog like any other SQL function."* | Mouse hovers over the **agent timeline** at the bottom as each subagent shows up. Then mouse moves up to the **center pane** as the brief begins to render. Don't click — just watch. The brief should be ~50-70% done by 1:35. |

### [1:35 → 2:05] The brief lands — read the Exec summary (30s)

| What you say | What you do |
|---|---|
| *"And here's the brief. The first thing on the page is the **Exec summary** — a single paragraph, three to five sentences, that reads like a human ops lead briefing the director on the phone. Numbers and named entities are bolded inline. This is the thing that goes to a phone screen at 7 AM. Below it, the top themes — each cites its source — and recommended actions with owners. Notice every numeric claim is grounded in a citation. That's enforced by an MLflow LLM-as-judge scorer in our eval suite."* | Mouse highlights the **Exec summary** paragraph at the top. Read the first sentence aloud verbatim from the screen if it's punchy — improvises great evidence of grounding. Then scroll down to show the **Top themes** and **Recommended actions** sections. |

### [2:05 → 2:35] Object drill-in + HITL action approval (30s)

| What you say | What you do |
|---|---|
| *"Now watch this. The brief mentions a fulfillment center — let me click the object chip."* (click) *"That's a Foundry-style investigation drawer. The property block is pulled live from the warehouse — same Genie MCP path. The chip on the right shows active disruptions overlapping this FC. And over here in the action queue — the Composer didn't just write a recommendation, it called a structured tool to **propose** a write-back action. Approve, and the system files the ticket — but with a human in the loop before any side effect lands. Reject, and it's logged. This is the difference between a report and an ops system."* | Click an `[OBJ:FC-XXX]` chip in the brief (the investigation drawer slides in). Hover the property panel briefly so the viewer sees it has real data. Then mouse over to the **right-rail Action Queue**, hover an action card, and click **Approve** on one. The action card flips to a ✓ APPROVED state. |

### [2:35 → 3:00] MLflow trace + production-ready close (25s)

| What you say | What you do |
|---|---|
| *"Quick cut to MLflow — every span is captured: supervisor planning, every subagent's tool calls, every Unity Catalog AI Function invocation, latency, token cost. We ran a twenty-item golden eval with three LLM-as-judge scorers: groundedness, action quality, subagent routing. Mean scores 0.90, 0.88, 0.81. So — recap. Genie under the hood via managed MCP. Seventeen live signal sources. Three governed Unity Catalog AI Functions. Multi-agent supervisor. Human-in-the-loop action layer. Deployed as a Databricks App with secret-scope OAuth and full MLflow tracing. That's the 11 PM Ops Brief."* | Cmd+Tab to the **MLflow trace** tab. Show the trace tree — spans nested supervisor → subagents → tools. Zoom into one span for 1-2 seconds so the viewer registers "real telemetry." Optionally flip briefly to the eval results page (the table with the three scorer columns). **End on the architecture diagram** if you have time — otherwise just freeze on the MLflow trace and stop recording. |

---

## Backup lines (use if you slip a beat or have spare seconds)

- **If the brief is taking too long:** *"While the Composer finishes synthesizing, let me show you the eval suite —"* Cmd+Tab to MLflow.
- **If you go over time:** drop the **MLflow trace** beat (2:35–3:00) — the trace is "nice to have," not load-bearing.
- **If the Exec summary reads weird:** don't read it verbatim — just describe it: *"This paragraph is the deliverable — designed to read like an ops lead briefing the director, not like dashboard output."*

---

## If something breaks during recording

| Failure mode | What to do |
|---|---|
| App URL shows "Service Unavailable" | Refresh once. If still down, run `databricks bundle run ops_brief --profile dnb-hackathon` in a separate terminal **before** starting the recording. |
| Brief generation hangs > 90s | Stop recording. Run a curl warm-up: `curl -X POST https://ops-brief-apatel-…/invocations -H 'Content-Type: application/json' -d '{"input": "tonights brief"}'` and wait for response. Restart recording. |
| Investigation drawer doesn't open on chip click | Skip the drill-in beat. Pivot to the action queue directly — the HITL story still lands without the drill-in visual. |
| MLflow trace tab is logged out | Skip the trace shot. Close on the architecture diagram (which is at the top of `SUBMISSION.md`). |

---

## What the demo proves against each rubric criterion

| Rubric (weight) | Proved by | Beat |
|---|---|---|
| Technical Excellence (25%) | Genie MCP + UC AI Functions + multi-agent supervisor + MLflow tracing + Apps deployment | 0:55–1:35, 2:35–3:00 |
| Innovation & Creativity (25%) | Plan-driven supervisor, 17 signals fused, object-chip Foundry drill-in, HITL action layer | 0:25–0:55, 2:05–2:35 |
| Business Impact & Relevance (25%) | Named 11 PM persona, quantified at-risk revenue, Exec summary on a phone screen | 0:00–0:25, 1:35–2:05 |
| Data & App Quality (15%) | 4-pane console, live signal tiles, severity gauge, object drill-down, action queue | 0:25–0:55, 2:05–2:35 |
| Demo (10%) | This script, hitting all 5 buckets in 3 minutes flat | The whole thing |

---

## File map of the demo

- **Title card:** make a 5-second title card in Keynote or Canva: black background, white text, two lines — "The 11 PM Ops Brief" / "Databricks DNB Hackathon · Track 1 · Aayush Patel". Drop it at 0:00–0:05.
- **Architecture diagram (optional close):** see `SUBMISSION.md` line ~24 (the mermaid block — render in any markdown previewer and screenshot).
- **Trace:** MLflow experiment `4469475842996134`.
- **App URL:** https://ops-brief-apatel-7474650842988229.aws.databricksapps.com

When done: export the video as MP4, 1080p, H.264, target file size < 200 MB so Loom/Microsoft Stream don't recompress aggressively. Then paste the share link into the submission form.
