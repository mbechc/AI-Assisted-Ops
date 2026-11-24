# How Steward uses AI

Steward is built around **Expert Tasks**: rules that read Catalyst Center evidence (CDP/LLDP neighbours, RF
neighbours, naming, subnets, inventory history, optional GeoIP) and make proposals with a confidence score
and the evidence behind them. **Every task works fully without AI.** AI is an optional assistant that you
can switch on for a second opinion and for the jobs rules handle poorly: irregular naming, ambiguous
renames, free-text reviewer notes and readable reports.

This document explains the options, where AI adds value, how it is kept safe, exactly what is sent, and
answers common questions.

## Summary

- Three modes: **No AI** (default), **Local LLM** (on the appliance) and **Azure OpenAI** (in your Azure
  tenant). Admins switch under **Configure → AI assistance**.
- No AI downloads nothing: there is no LLM container until an admin selects Local LLM.
- AI never makes a change on its own. It lowers or slightly raises confidence, suggests, or rewrites text.
  Executions still go through the autonomy levels, thresholds and caps you set.
- Every AI answer is validated, and every AI feature falls back to the rule-based result when AI is off,
  slow, unavailable or wrong.

## The three modes

| | No AI | Local LLM | Azure OpenAI |
|---|---|---|---|
| What runs | Rules and evidence only | Ollama on the appliance, CPU only (default model `qwen2.5:7b-instruct`) | Your Azure OpenAI chat deployment (for example `gpt-4o-mini`) |
| Data leaves the host | No | No | Yes, to your Azure OpenAI resource |
| Extra download | None | Ollama image and model (about 5 GB), only after you select it | None |
| Hardware | 8 vCPU, 16 GB RAM | 16 vCPU with AVX2, 64 GB RAM | 8 vCPU, 16 GB RAM |
| Network | Catalyst Center (and GitHub for updates) | Plus `registry.ollama.ai` and Docker Hub to download | Plus `<resource>.openai.azure.com` (and `login.microsoftonline.com` with Entra sign-in) |
| Speed | Instant | Seconds to a minute per call on CPU | About a second per call |
| Answer quality | - | Good for structured checks | Best, especially for summaries and wording |
| Cost | None | Hardware only | Azure consumption (small: a few calls per run) |
| Task label | Blue **AI Capable** | Green **Local AI Assisted** | Green **Cloud AI Assisted** |

### No AI (default)

The appliance runs only the Expert Tasks. Nothing is downloaded or sent anywhere. The task list marks
tasks that could use AI with a blue **AI Capable** label, so you can see what you would gain.

### Local LLM

An admin selects **Local LLM** and confirms. The portal cannot start containers itself, so it asks the
host updater (the same root-owned systemd service that installs updates) to:

1. add the `llm` compose profile to `.env` and start the `ollama` container;
2. download the model (or import `data/llm/ollama-models.tgz` on air-gapped hosts);
3. report progress and a log back to the AI assistance page.

Until the model is ready, tasks keep running without AI. **Remove model** (or leaving Local LLM with
*delete model* ticked) stops the container and deletes the model; without the tick the model is kept for
a quick switch back. Updates keep the LLM if it is enabled and never install it if it is not.

### Azure OpenAI

Microsoft Copilot is an end-user product without an API an appliance can call. The enterprise way to use
the same family of models is **Azure OpenAI**, in your own Azure subscription, region and network
controls. You provide:

- the endpoint `https://<resource>.openai.azure.com` (HTTPS only);
- the deployment name of a chat model that supports structured outputs (for example `gpt-4o-mini` or
  `gpt-4o`);
- an API key, or a Microsoft Entra app registration (tenant ID, client ID, client secret) with the
  *Cognitive Services OpenAI User* role on the resource.

Secrets are encrypted in the database with the appliance key and are never returned to the browser or
written to the audit log. **Test connection** sends a tiny probe prompt before you save. The AI assistance page
shows a warning that inventory data will leave the host.

## Where AI adds value

The value is concentrated in six places. Each row says what AI does, why rules alone fall short, how it
works, and what you get without AI.

### 1. Second opinion on proposals

- **Tasks**: AP site assignment, Wrong-site detection.
- **What**: for proposals in the middle confidence band, the AI reads the same evidence the rules used and
  answers with a confidence and concerns ("RF neighbours point to a different floor than the uplink").
- **Why**: rules weigh signals with fixed weights; a reviewer would spot contradictions the weights hide.
- **How**: the AI's confidence is combined with the rule confidence: it can **lower** confidence a lot but
  **raise** it by at most 5 points, so AI alone can never push a proposal over the auto-execute threshold.
  Answers are cached per proposal and reused while the evidence is unchanged.
- **Without AI**: confidence comes from the evidence alone; the review drawer says AI is off.

### 2. Learning naming conventions

- **Tasks**: AP site assignment and Wrong-site detection (extra evidence signal), AP hostname reset
  (name template).
- **What**: learns how your estate names devices, for example `kbh-s2-ap07` = Copenhagen HQ, floor 2, even
  when the name does not contain the building name, or odd templates like `dk_cph_3sal_ap7_n`.
- **Why**: the rule-based name model only learns prefixes and simple prefix-plus-number patterns.
- **How**: one call per run sends a sample of names with their known sites. The AI returns a regular
  expression with named groups and an alias table. Steward then **tests it against every device with a known
  site** and only uses it if it rebuilds at least **95%** of them (and at least 10 devices). Unsafe or overly
  complex regular expressions are rejected. The learned convention is one more signal (weight 0.10, capped
  at 0.15); a proposal supported only by it is capped at confidence 60 and never executed automatically.
  For hostname reset, a learned template must rebuild every matching name on the floor exactly; such
  proposals get confidence 60. Results are cached until the inventory changes.
- **Without AI**: prefix and number rules; floors with irregular names get no hostname proposal.

### 3. Rename detection

- **Task**: Site hierarchy drift.
- **What**: recognises that a removed site and a new site under the same parent are one site renamed by
  delete and re-create (`Bldg 2` → `Building 02`). The site ID changes, so devices and settings on the old
  site were probably not moved; this is worth knowing.
- **Why**: the rename can involve abbreviations, languages or reordered words that similarity scores miss.
- **How**: rules first: names that are equal after normalising (case, punctuation, `Bldg`/`Building`,
  `Flr`/`Floor`, zero padding) or clearly similar (80%+) are paired without AI; different numbers are never
  paired. Only grey-zone pairs (40-80% similar) are sent to AI, in one batched call. Confirmed pairs are
  reported as **Renamed (re-created)** with confidence 65.
- **Without AI**: clear renames are still detected; grey-zone pairs are reported as one *added* and one
  *removed* site.

### 4. Reviewer-feedback insights

- **Task**: every task, on the **Calibration and feedback** tab.
- **What**: summarises why reviewers reject or override proposals and suggests parameter changes.
- **Why**: reviewer notes are free text; patterns ("the name prefix belongs to the old building") are easy
  to read for a language model and hard for rules.
- **How**: on request, one call with the task description, its parameters and current values, rejection
  statistics and up to 40 recent notes. Suggestions are checked against the real parameter list and types;
  invented parameters are dropped. The result is cached for a day. **Nothing is changed**: an admin applies
  a suggestion on the Configuration tab if they agree.
- **Without AI**: rejection and override rates per group and per evidence signal, plain hints such as
  "Consider lowering `weight_name`", and the latest notes.

### 5. Shift summary

- **Where**: Dashboard, **Since last shift** (last 8, 12 or 24 hours).
- **What**: a short handover: what needs attention, what was fixed.
- **How**: the facts (counts per task, failed runs, failed changes) are computed from the database; on
  request the AI writes a narrative from them. **Every number in the AI text must appear in the facts**,
  otherwise the AI text is discarded and the list is shown. Cached while the facts are unchanged.
- **Without AI**: the same facts as a list.

### 6. Owner reports

- **Task**: Telemetry baseline (credential, SNMP and collection failures that a resync cannot fix).
- **What**: ticket-ready text per failure cause for the team that owns the devices.
- **How**: the template ticket lists the cause, impact, devices, the most common errors and the fix. On
  request the AI rewrites it; the AI version is only used if it **lists every device**. Copy it into your
  ticketing system.
- **Without AI**: the template ticket text.

## Task matrix

| Expert Task | AI features | Label |
|---|---|---|
| AP site assignment | Second opinion, naming convention | Yes |
| Wrong-site detection | Second opinion, naming convention | Yes |
| AP hostname reset | Naming template | Yes |
| Site hierarchy drift | Rename detection | Yes |
| Telemetry baseline | Owner reports | Yes |
| Expected-down devices | - (rules on reachability history are exact) | No label |
| Device role correction | - (topology rules are exact) | No label |
| Inventory snapshot | - (collector) | No label |

Reviewer-feedback insights and the shift summary are available for every task and are not part of the
label.

## Safety model

1. **Off by default.** Nothing is downloaded or sent until an admin chooses a mode. Every mode change is
   audited (`settings.ai_updated`), without secrets.
2. **AI cannot act.** AI has no tools and no access to Catalyst Center. It returns JSON that Steward checks;
   only the rules and your autonomy settings decide what is executed.
3. **Bounded influence.** Confidence: down freely, up by at most 5. AI-only evidence: capped at 60 and
   never automatic. Suggestions: advice only.
4. **Validated output.** Structured answers (JSON schema; strict structured outputs on Azure). Learned
   naming rules must pass a 95% accuracy test on your own data, regular expressions are length- and
   complexity-limited, numbers in summaries must match the facts, owner tickets must list every device.
5. **Graceful failure.** Timeouts, quota errors or an unavailable model mark the remaining items
   *AI unavailable* and the run continues on rules. The review drawer shows the AI status per item.
6. **Least privilege.** The portal never gets the Docker socket; the Local LLM is started by the host
   updater through a request file. Azure secrets are encrypted at rest and write-only in the UI.
7. **Data minimisation.** Only the fields listed below are sent; no credentials, configurations, SNMP
   strings or user data.

## What is sent to the AI

With **Local LLM** nothing leaves the host. With **Azure OpenAI**, these fields are sent to your resource
over HTTPS:

| Feature | Fields |
|---|---|
| Second opinion | Device hostname, model, management IP, WLC IP, proposed and runner-up site names, evidence summary (neighbour names, RSSI, subnet, tags), rule confidence |
| Naming convention | Up to 160 hostnames with their building and floor names; floor names with AP hostnames for hostname templates |
| Rename detection | Parent site path, site type, old and new site names |
| Reviewer insights | Task description, parameter names and values, rejection statistics, up to 40 reviewer notes with device names |
| Shift summary | Counts per task, top site groups, failed run errors |
| Owner reports | Failure cause, device hostnames, IPs, sites, platforms, Catalyst Center error texts (up to 50 devices) |

Example (second opinion):

```text
AP: CPH-B02-F3-AP14, model C9130AXI-E, IP 10.12.3.44, WLC 10.0.0.5
Proposed site: Global/Denmark/Copenhagen/CPH-B02/Floor 3 (floor determined: yes)
Rule-based confidence: 78
Evidence:
- Wired uplink (CDP/LLDP): cph-b02-f3-sw1 on CPH-B02/Floor 3
- RF neighbours: 6 of 8 strong neighbours on CPH-B02/Floor 3
- Name convention: prefix 'cph-b02-f3' -> CPH-B02/Floor 3
Runner-up buildings: CPH-B01 (0.12)
```

Microsoft states that Azure OpenAI prompts and completions are not used to train models and are not
available to OpenAI. Abuse monitoring can store them for up to 30 days unless your organisation has an
approved exemption; check your agreement and choose the region of your resource accordingly.

## Limits

- The local model is small and runs on CPU: good at structured checks, weaker at long summaries. Expect
  seconds to a minute per call; the per-run call budget keeps runs bounded.
- AI features need enough data: naming conventions need at least 10 devices with a known site,
  insights need reviewed items.
- AI never replaces evidence: if Catalyst Center has no uplink, RF or naming data for a device, AI cannot
  invent it.

## FAQ

**Do I need AI?** No. All tasks work without it, and autonomy promotion is driven by your review
decisions, not by AI. AI mainly helps with irregular naming, ambiguous renames and reporting.

**Which mode should I choose?** No AI to start. Local LLM if data must not leave the host and the VM has
16 vCPU and 64 GB. Azure OpenAI if your organisation already uses it and allows inventory names and IPs to
go to your tenant; it is faster and better at writing.

**What happens when I switch modes?** The next task run uses the new mode. AI reviews are only reused
from the same provider, so the new provider reviews items afresh. Leaving Local LLM stops the container
and optionally deletes the model.

**Can AI make a change in Catalyst Center?** No. Changes are made by the tasks through the Catalyst Center
API, only for proposals approved by a person or by your autonomy settings.

**Does AI lower the accuracy needed for L2?** No. Calibration counts only human decisions and outcomes
observed in Catalyst Center, and AI can raise confidence by at most 5 points.

**Is Microsoft Copilot supported?** Copilot has no API for appliances. Azure OpenAI is Microsoft's
enterprise service for the same model family and is what Steward uses.

**Can I use OpenAI.com or another provider?** Not today. The provider layer is pluggable, so others can be
added.

**What if the Azure key expires or the quota runs out?** Calls fail with a clear status, the affected
items show *AI unavailable*, and tasks continue on rules. **Test connection** shows the error.

**How do I install the Local LLM without Internet?** Export a bundle on a connected host with the model
(`scripts/export-bundle.sh`), copy `ollama-models.tgz` to `/opt/Steward/data/llm/`, then select Local LLM.

**Where do I see what AI did?** The review drawer shows the AI review per item (Local or Cloud), run logs
show AI calls and validations, and the audit log records mode changes and on-demand AI summaries.
