---
description: Three-track synonym/skills audit. Track 1 — gap-fill allowable_synonyms from shortlist JD keywords (profile/bullets.md). Track 2 — memory unlock prompts for adjacent tools/concepts the user likely touched but hasn't documented, covering BOTH the Deloitte/SAP role and the personal projects (PROVA, CapTrack, Options Pricing ML). Track 3 — same gap-fill + memory-unlock pattern applied to profile/skills_master.md. Never writes to bullets.md or skills_master.md directly.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
---

# /suggest-synonyms — gap-fill + memory unlock for bullets.md and skills_master.md

This command runs three tracks:

**Track 1 — JD gap-fill (bullets):** audits the gap between shortlist JD vocabulary and
`allowable_synonyms` already in `profile/bullets.md`. Proposes new synonym entries
that let `/tailor` mirror high-frequency JD terms.

**Track 2 — memory unlock (bullets):** independently reasons about tools, interfaces, and
processes the user *likely touched but hasn't documented*, given the specifics of
their role (Deloitte Advisory Analyst on SAP ACM S/4HANA for a Fortune 100
agri-commodity client, multi-region rollouts, COE, oil & gas Signavio engagement)
**and** the personal projects (PROVA SR 11-7 compliance tool, CapTrack
trade/P&L platform, Options Pricing ML predictor). These are surfaced as
**confirm-before-adding prompts** — the user says yes/no, then decides whether to
add a new bullet or extend an existing one.

**Track 3 — skills_master gap-fill + memory unlock:** same two-part pattern applied
to `profile/skills_master.md` instead of `profile/bullets.md`. Gap-fill checks
shortlist JD keywords against existing skill entries/`allowable_synonyms` and
proposes new alias entries. Memory unlock reasons about specific tools/libraries
the user likely used (across BOTH the Deloitte/SAP role and the personal projects)
but hasn't entered as a `SKILL-*` block.

The key distinction across tracks:
- Track 1/3-gapfill = "you did X; here's better JD vocabulary for it"
- Track 2/3-unlock = "you probably touched Y given your role/projects; did you? If yes, add it"

**Hard constraints (same as /tailor's R2):**
- A synonym must re-package the SAME underlying process or claim.
- ACM commodity-contract settlement is NOT generic SD Order-to-Cash. Do not propose
  that relabeling.
- No standalone MM/SD/PP config claims. Integration-level mentions only.
- Memory-unlock suggestions are hypotheses only — never assert the user did something;
  always frame as a question ("Did you use X? If yes, this would be addable.")
- If a JD keyword has no honest anchor, write `UNMAPPABLE` — do not invent.
- Track 3 skill suggestions must trace to evidence already in `bullets.md` or to a
  confirmed Track 2/3 memory-unlock answer — never invent a skill with no anchor.

Output is a draft file for the user to review. You never write to `profile/bullets.md`
or `profile/skills_master.md` directly.

---

## Step 1 — load profile and today's scored shortlist

```bash
# Get today's shortlist file
SHORTLIST=$(ls -t shortlist/*.md 2>/dev/null | head -1)
echo "shortlist: $SHORTLIST"

# Extract all keywords_to_mirror from scored.parquet for shortlist rows
uv run python -c "
import json, pandas as pd
from pathlib import Path

scored = pd.read_parquet('jobs/scored.parquet')
# company lives only in clean.parquet; scored.parquet has no company column
clean = pd.read_parquet('jobs/clean.parquet', columns=['job_id', 'company'])
scored = scored.merge(clean, on='job_id', how='left')
# Only shortlist-worthy rows (fit >= 50, not ineligible)
keepers = scored[
    (scored.fit_score >= 50) &
    (scored.sponsorship_label != 'ineligible')
].copy()

# Collect all keywords
all_kw = []
for _, row in keepers.iterrows():
    kws = row.get('keywords_to_mirror', None)
    if kws is None:
        continue
    import numpy as np
    if isinstance(kws, (list, np.ndarray)):
        for kw in kws:
            kw = str(kw).strip()
            if kw:
                all_kw.append({'keyword': kw, 'job_id': row['job_id'],
                               'company': row.get('company') or '', 'fit': float(row.get('fit_score',0))})
    elif isinstance(kws, str) and kws:
        all_kw.append({'keyword': kws.strip(), 'job_id': row['job_id'],
                       'company': row.get('company') or '', 'fit': float(row.get('fit_score',0))})

# Deduplicate + count frequency
from collections import Counter
freq = Counter(item['keyword'] for item in all_kw)
# Top 40 by frequency, then by fit of first-appearing row
seen = set()
top = []
for item in sorted(all_kw, key=lambda x: -x['fit']):
    if item['keyword'] not in seen:
        seen.add(item['keyword'])
        top.append({**item, 'frequency': freq[item['keyword']]})
    if len(top) >= 40:
        break
top.sort(key=lambda x: -x['frequency'])
print(json.dumps(top, indent=2, default=str))
" > /tmp/shortlist_keywords.json
echo "keywords dumped: $(python3 -c "import json; d=json.load(open('/tmp/shortlist_keywords.json')); print(len(d))")"
```

Then `Read` these files:
- `/tmp/shortlist_keywords.json`
- `profile/bullets.md` — canonical bullets + existing `allowable_synonyms` per bullet

## Step 2 — build the gap map

For each keyword in `/tmp/shortlist_keywords.json`, do the following analysis in
your head before writing any output:

1. **Check coverage:** Is this keyword (or a close variant) already present in
   any bullet's `allowable_synonyms` list or in the canonical text of that bullet?
   If yes → mark `already_covered`. No new entry needed.

2. **Find the best anchor bullet:** Which canonical bullet, if any, describes the
   same underlying process/claim that this JD keyword refers to? Consider:
   - B-DEL-01: MTM risk report, position reconciliation, P&L validation, landed cost
   - B-DEL-02: defect investigation, settlement chain, UAT, production support
   - B-DEL-03: global rollout, KT, COE leadership, localization
   - B-DEL-04: Signavio, risk & controls, SOX/RCM, process mapping, oil & gas
   - B-TA-01: teaching, Python/Java/SQL instruction
   - B-PROVA-01..06: multi-agent LLM, SR 11-7 compliance, PDF report generation,
     Next.js/Supabase production app, prompt-injection defense, AI regression suite
   - B-CAPTRACK-01..05: trade/portfolio P&L, accounting logic, accuracy testing,
     multi-tenant Next.js/Supabase app, market-data/FX API integration
   - B-OPT-01..05: ML models, Black-Scholes, XGBoost benchmarking, option Greeks
     feature engineering, grid-search/K-Fold tuning pipeline
   (Always re-check against the current `profile/bullets.md` — this list is a
   pointer, not the source.)

3. **Honest mapping only:** The synonym must be a re-phrasing of what the bullet
   already claims — same scope, same tool, same metric. If the JD keyword implies
   a capability the user does not have (e.g. "configured SD pricing procedures"),
   write `UNMAPPABLE — no canonical anchor`.

4. **Draft the synonym text:** A short phrase (2–6 words) using JD vocabulary that
   a reader would recognize as equivalent to the canonical claim.

## Step 3 — write the draft output

Write to `profile/synonyms_draft_YYYY-MM-DD.md` (use today's date).

Format:

```markdown
# Synonym & Skills Suggestions — <today's date>

Generated from top-N shortlist keywords in `scored.parquet` (fit ≥ 50 rows).
**Review each entry before adding to profile/bullets.md or profile/skills_master.md.**
Only add entries you can stand behind on a call — these are attested claims.

---

## Ready to add

These map cleanly to a canonical bullet. Copy the `allowable_synonyms:` patch
block into the corresponding bullet in `profile/bullets.md` after review.

### B-DEL-01 (MTM / P&L reconciliation)
**Add to `allowable_synonyms`:**
- "phrase one" — maps from JD keyword "X" (seen N times across shortlist)
- "phrase two" — maps from JD keyword "Y" (seen M times across shortlist)

### B-DEL-02 (defect investigation / UAT)
**Add to `allowable_synonyms`:**
- ...

(repeat per bullet — only include bullets that have new suggestions)

---

## Unmappable keywords

JD terms that appeared in the shortlist but have no honest anchor in any
canonical bullet. Do NOT add these as synonyms.

| keyword | frequency | reason unmappable |
|---|---|---|
| "SAP SD configuration" | 8 | user configured no SAP module; ACM integration-level only (R3) |
| ...                    |   |                                                                |

---

## Already covered

Keywords already present in a bullet's canonical text or allowable_synonyms.
No action needed.

| keyword | frequency | covered by |
|---|---|---|
| "UAT" | 12 | B-DEL-02 canonical |
| ...   |    |                    |
```

Rules for the "Ready to add" section:
- Only include bullets where you have ≥1 genuinely new synonym to propose.
- If a bullet has no gaps, omit it entirely — don't pad with zeroes.
- Each suggestion line must show: the proposed synonym phrase AND the JD keyword
  it mirrors AND the frequency count so the user can prioritize.

Rules for "Unmappable":
- Be specific about WHY it's unmappable (R2b, R3, or "no canonical anchor").
- Do not soft-pedal R3 violations — if it's a standalone module config claim, say so.

## Step 3b — Track 2: memory unlock audit

Independently of the JD keyword list, reason about the user's specific role context
and generate a list of tools/interfaces/processes they **probably encountered** but
haven't documented in any bullet's canonical text or `allowable_synonyms`.

Use this role context as your inference base:

> Deloitte Advisory Analyst, May 2022 – Jul 2024. Supported a Fortune 100
> agricultural commodities client on SAP ACM (Agricultural Contract Management)
> running on S/4HANA. Daily production support: MTM risk reports, defect
> investigation across contract→load-capture→settlement chain, UAT validation
> with ABAP developers. Led a 3-analyst Center of Excellence (COE). Supported
> global S/4HANA ACM rollouts across LATAM, EMEA, APAC (regional localization,
> KT). Separate engagement: SAP Signavio-driven ERP transformation for a US
> downstream oil & gas client (risk & controls, SOX/non-SOX RCM, process workshops).

Think through: what tools, interfaces, transaction codes, and adjacent processes
does someone in this *exact* environment almost certainly encounter, even if not
explicitly mentioned in their bullets?

Candidate categories to reason through (not exhaustive — use your knowledge of
large SAP S/4HANA implementations):

- **UI layer:** SAP Fiori launchpad, Fiori apps (used by end-users and functional
  analysts in S/4HANA environments — almost universal)
- **Transport & change management:** Transport requests (SE09/SE10), ChaRM /
  Solution Manager change control (standard in Deloitte engagements)
- **Testing tools:** SAP Solution Manager Test Suite, HPQC/ALM for UAT defect
  tracking, or equivalent
- **Collaboration / ITSM:** ServiceNow (Deloitte standard for incident management),
  JIRA, or similar — the user mentions "daily open issues to near-zero"
- **Reporting / analytics layer:** SAP BW (already in B-DEL-01), Analysis for
  Office (AO) — common companion to BW reporting
- **Data tools used during reconciliation:** SAP GUI SE16/SE16N table browsing,
  SQVI, or Excel-based reconciliation — what did they actually use to pull records?
- **ACM-specific transactions:** e.g., ACM Cockpit, SAPLM or equivalent ACM
  transaction codes touched during daily support
- **Signavio engagement specifics:** SAP Signavio Process Manager, process
  collaboration hub, swim-lane diagrams — what specific Signavio capability did
  they use?
- **Roll-out toolkit:** SAP Activate methodology artifacts (Fit-to-Standard, BBPs),
  cutover planning, data migration support (LTMC/LTMOM) — common on global rollouts
- **Communication / documentation:** MS Teams/SharePoint (Deloitte standard),
  Confluence, or similar — relevant for KT documentation claim in B-DEL-03

For each candidate, rate your confidence: `likely` (would be surprising if not
used given the role) vs `possible` (plausible but not universal). Only include
`likely` items in the output — omit `possible` unless it appears in the shortlist
keywords, in which case note it as lower-confidence.

**Also reason through the personal projects** — separate inference base,
same `likely`/`possible` discipline:

> PROVA — SR 11-7 Model Documentation Compliance Tool (Mar 2026 – May 2026,
> personal project). Multi-agent LLM system (3 concurrent agents + judge/orchestrator),
> compliance pipeline evaluating model docs against 20 SR 11-7 elements across the
> three validation pillars (Conceptual Soundness, Outcomes Analysis, Ongoing
> Monitoring), PDF report generation with weighted scoring.
>
> CapTrack (Dec 2025 – Feb 2026, personal project). Trade/portfolio analysis
> platform: broker trade ingestion/normalization, multi-asset/multi-currency
> position construction with realized/unrealized P&L, deterministic accounting
> logic for edge cases (partial fills, shorts), reconciliation across large
> structured datasets, P&L accuracy testing framework.
>
> Options Pricing ML Predictor (Dec 2025 – Feb 2026, personal project). Benchmarked
> Linear Regression / Random Forest / XGBoost against real options market data
> (yfinance), compared against a Black-Scholes baseline via RMSE/R², identified
> XGBoost as the top performer.

Candidate categories for the personal-projects context (not exhaustive):

- **LLM tooling layer (PROVA):** which specific LLM provider/API, orchestration
  framework (e.g. LangChain, raw API calls), prompt-versioning approach, retry/
  backoff implementation — what did the judge/orchestrator layer actually call?
- **Document handling (PROVA):** PDF parsing/extraction library used to ingest
  source model-documentation, structured-output parsing (e.g. Pydantic/JSON schema
  enforcement) for the per-element gap findings
- **Data ingestion (CapTrack):** specific broker-feed format(s) parsed, the
  normalization library/approach, how trade data was validated on ingest
  (schema checks?)
- **Testing approach (CapTrack):** what testing framework backed the "P&L accuracy
  testing framework" claim — pytest, custom assertions, golden-file comparisons?
- **Data science tooling (Options Pricing):** cross-validation/train-test split
  methodology, feature engineering specifics, hyperparameter tuning approach for
  XGBoost, visualization library for the model comparison
- **Source data (Options Pricing):** what exactly yfinance pulled (options chains,
  implied vol, underlying price history) — any other data source blended in?

Also check the shortlist keywords for any terms that fall into either "memory
unlock" bucket (Deloitte/SAP role OR personal projects) — JD terms the user likely
knows from practice but hasn't documented.

Add a section to the draft output file:

```markdown
---

## Memory unlocks — confirm before adding

Tools and concepts you likely touched (across the Deloitte/SAP role AND the
personal projects) but haven't documented. **For each: answer yes/no.
If yes, decide whether to add it as a synonym to an existing bullet, or flag
it for a new bullet entirely.**

| tool / concept | source context | confidence | why you probably touched it | which bullet to extend (or "new bullet") | question to confirm |
|---|---|---|---|---|---|
| SAP Fiori launchpad | Deloitte/SAP | likely | Standard UI layer in all S/4HANA environments; end-users and functional analysts interact with it daily | B-DEL-01 or B-DEL-02 (daily ops context) | Did you use Fiori tiles/apps during daily reconciliation or defect investigation, even passively? |
| Pydantic / JSON schema validation | PROVA (personal project) | likely | Structured per-element gap findings need enforced output shape from an LLM call | B-PROVA-02 | Did you enforce a structured output schema (Pydantic, JSON schema, or similar) on the LLM's gap-finding output? |
| ... | ... | ... | ... | ... | ... |
```

Only include entries where the confidence is `likely`. Cap at 12 rows total
across both contexts — if you have more candidates, rank by shortlist keyword
frequency (Deloitte/SAP items) and project-specificity (personal-project items),
and cut the tail.

For items that appear in the shortlist keywords AND are memory-unlock candidates,
add a note: `(also appears in shortlist — high value if confirmable)`.

After the user confirms items from this table, they can either:
a) Add a phrase to an existing bullet's `allowable_synonyms` (if it's a synonym
   for something already claimed), or
b) Draft a new canonical bullet for a genuinely undocumented capability — which
   is outside this command's scope; `/tailor` will prompt them when needed.

## Step 3c — Track 3: skills_master gap-fill + memory unlock

`Read` `profile/skills_master.md` (every `SKILL-*` block: `name`, `category`,
`evidence`, `allowable_synonyms`, `vertical_lean`).

**Gap-fill half:** reuse `/tmp/shortlist_keywords.json` from Step 1. For each
keyword that names a tool/technology/library (skip process-y keywords already
handled by Track 1 — those belong to bullets, not skills), check:

1. **Already covered?** Does the keyword (or a close variant) already match a
   `SKILL-*` entry's `name` or `allowable_synonyms`? If yes → no action.
2. **Honest anchor in an existing skill?** If the keyword is just a different
   display name for a skill already listed (e.g. JD says "LLM orchestration"
   and `SKILL-MULTIAGENT-LLM` already covers this underlying capability), propose
   it as a new `allowable_synonyms` entry on that skill.
3. **No anchor at all?** Do NOT propose a brand-new `SKILL-*` entry from a JD
   keyword alone — a skill entry asserts the user has used something; the
   gap-fill track only ever proposes *aliases* on existing entries, same
   no-fabrication discipline as bullets' Track 1. Route anything with no
   existing skill anchor to the memory-unlock half below instead, since that's
   the part of this command that explicitly asks the user to confirm.

**Memory-unlock half:** reason about specific tools/libraries the user likely
used but hasn't entered as a `SKILL-*` block, anchored to evidence already
established in `profile/bullets.md` (the same Deloitte/SAP and personal-project
context from Step 3b above) — e.g. if a Track 2 memory-unlock item gets
confirmed `yes` by the user in a prior run, or is independently inferable as
`likely` from the bullet text already on record, propose it here too. Frame
every row as a yes/no question, never an assertion.

Add a section to the draft output file:

```markdown
---

## Track 3 — skills_master.md gap-fill (ready to add)

New `allowable_synonyms` aliases mapping a JD keyword onto an EXISTING skill
entry. Copy the patch into the corresponding `SKILL-*` block in
`profile/skills_master.md` after review.

### SKILL-MULTIAGENT-LLM (Multi-Agent LLM Systems)
**Add to `allowable_synonyms`:**
- "phrase" — maps from JD keyword "X" (seen N times across shortlist)

(repeat per skill — only include skills that have new alias suggestions)

---

## Track 3 — skills_master.md memory unlock — confirm before adding

Tools/libraries you likely used but haven't entered as a `SKILL-*` block.
**For each: answer yes/no. If yes, add a new `SKILL-*` block yourself** (this
command never writes a new skill entry directly — same discipline as bullets).

| tool / library | category | anchor evidence | confidence | question to confirm |
|---|---|---|---|---|
| Pydantic | ai_ml | B-PROVA-02 (structured per-element gap findings) | likely | Did you use Pydantic (or an equivalent schema-validation library) to enforce structured LLM output in PROVA? |
| ... | ... | ... | ... | ... |
```

Cap the memory-unlock table at 8 rows — rank by how directly the tool would be
visible to a recruiter scanning the Skills section (concrete library/tool names
over vague process terms).

## Step 4 — report

Tell the user:
- Path to the draft file
- Track 1: N ready-to-add synonyms across M bullets, K unmappable keywords
- Track 1: top 3 highest-frequency unmappable keywords (market wants this; profile can't claim it)
- Track 2: X memory-unlock prompts (Y flagged as "also in shortlist — high value if confirmable"),
  broken out by source context (Deloitte/SAP vs personal projects)
- Track 3: P ready-to-add skill aliases across Q skills, R memory-unlock prompts
  for skills_master.md
- Reminder: "Answer the yes/no questions in the Memory unlocks tables first — that's
  the fastest path to new claimable vocabulary and new skill entries. Then review
  the Ready-to-add synonyms/aliases. Only add what you can stand behind on a
  recruiter call. New skills_master.md entries are written by you, not this command."
