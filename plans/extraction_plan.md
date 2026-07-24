# Extraction v2 — Execution Plan

SCOPE: `jobs/clean.parquet` → `jobs/extracted.parquet` only. Build the provider
adapter (`src/llm/`) and the extraction sidecar (`src/extract/`). Nothing
downstream (embeddings, scoring, review, shortlist) is part of this plan. This
plan is the successor to `plans/discovery_plan.md` and consumes its output
(`clean.parquet`). Authoritative design: `docs/score/v2_architecture.md` §2 and
§7 — this plan implements those two sections and nothing beyond them.

YOU ARE THE IMPLEMENTER. Follow steps in order. Do not skip, reorder, or invent
alternatives. Every decision is already made in this document. If a step cannot
be completed as written, STOP, mark the step `[BLOCKED: reason]` in this file,
report to the user, and wait. Never work around a blocker silently.

STATUS TRACKING: Each step has a checkbox. After completing a step, edit THIS
file: mark `[x]` plus a one-line note (what was done, any deviation). On session
restart, resume at the first unchecked step. Never skip a verify sub-step.

SESSION BOUNDARIES (context is cleared between sessions):
- Every step ends committed (R7) and tests-green, so stopping after ANY checked
  step is safe. Preferred stop = end of a phase. Never stop mid-step: finish the
  current step (commit + verify), tick its box, then STOP.
- Start-of-session ritual: (1) read this file, find the first unchecked step;
  (2) read the "Files to read before starting" list once; (3) `uv run pytest -q`
  — if not green, STOP and report, do not build on red; (4) continue at the first
  unchecked step, do not redo checked steps.

## P. Prerequisites & user-gated content

PREREQUISITES (the implementer CANNOT self-provide these; if one is missing, STOP
and ask the user — never fabricate data or skip the check):
- `jobs/clean.parquet` exists with rows (discovery v2 has run). The live smokes
  (2.6, 3.2) need ≥2 rows lacking a valid `ok` sidecar.
- For the $0 live checks (2.6, 3.2, 4.1): local Ollama is running with a
  JSON-capable instruct model; `curl http://localhost:11434/v1/models` lists it.
- `claude` CLI authenticated — ONLY for the OPTIONAL claude-cli confirmations; not
  needed for a $0 build.
- Explorer container + model setup (§10.1) done — ONLY for Phase 3.3.

USER-GATED CONTENT (author a draft, then STOP and get user approval before relying
on it — do not blind-accept model-authored text):
- §4 `FEW_SHOT` example (hand-authored JD → Sidecar).
- §11 golden-set JDs + expected sidecars (the eval ground truth).
- §12 edits to CLAUDE.md (user-owned governance docs) — show the diff.

## R. Global rules (apply to every step)

- R1: Exactly ONE LLM call site in this plan's code: the extraction runner
  (`src/extract/runner.py`) via the adapter (`src/llm/`). This is the project's
  THIRD sanctioned `src/` LLM carve-out (after tailoring and the score judge);
  step 12 amends CLAUDE.md to record it. No other module in `src/` may call
  an LLM. Never add a heuristic that replaces the model's judgment.
- R2: Code is user-, vertical-, and company-agnostic. Extraction is
  USER-INDEPENDENT (§2): no profile, no vertical, no rubric ever enters the
  extraction prompt. The only inputs are the JD `title` + `description`.
- R3: Every LLM output is schema-validated + evidence-checked before it is
  cached, on EVERY backend. A weak model degrades into retries, never silent
  garbage. Values are NEVER defaulted or fabricated in code (v1 R11).
- R4: The sidecar schema is closed (§3). No extra keys, no missing keys. A
  `schema_version` bump invalidates the whole cache.
- R5: Do not touch `src/score/`, `src/tailor/`, `src/discovery/`, or any v1
  scoring/sponsorship code (the one exception is moving a single helper out of
  `judge.py` in step 1.1, which must stay behaviourally identical). Extraction is
  self-contained. v1 keeps running.
- R6: Before writing any backend against a remote endpoint (Ollama, vLLM,
  Anthropic), verify one real round-trip with `curl` and record the actual
  response shape. Never guess a payload shape. If an endpoint cannot be reached
  after 30 minutes, mark the step `[BLOCKED]` and stop.
- R7: One git commit per completed step. Message: `extraction-v2 step N.M: <what>`.
- R8: Tests never read gitignored `profile/`. Tests use a FAKE backend
  (`src/llm/fake.py`) and fixture inputs; NO live LLM call runs in CI. The
  golden-set eval (step 11) is opt-in (`-m evals`, needs a real model).
- R9: Do not build anything in §OUT OF SCOPE (end of file).
- R10: No new hard dependencies except `pydantic`. All HTTP uses the existing
  `requests` dep. Do NOT add the `openai` or `anthropic` SDKs.
- R11 (cost): the ONLY LLM call is `extract_batch`'s `conv.send` (R1). It spends
  money ONLY when the configured backend is `claude-cli` or `anthropic`;
  `openai-compatible` (Ollama/vLLM) is $0. `dump` and `ingest` modes make NO call.
  With the default verify config, BUILDING THIS PLAN COSTS $0: every unit test uses
  the fake backend (R8), and the two live checks (steps 2.6, 4.1) run against local
  Ollama. Re-running those two against claude-cli/anthropic is OPTIONAL (~cents),
  never required. No other step calls an LLM.

Files to read before starting (read fully, once):
- `docs/score/v2_architecture.md` — §2 (extraction), §7 (adapter), §9 (evals),
  §12 (caching), §14 (layout). The contract.
- `src/score/judge.py` — the existing `claude -p` carve-out. Its patterns
  (slim flags, `--output-format json` envelope, `_extract_json_array`,
  retry-with-feedback via `--resume`, `validate_batch`, ThreadPool warm-up,
  stage-nothing-on-failure) are the HOUSE STYLE. Reuse them; do not reinvent.
- `src/score/scoring_io.py` lines 78–115, 388–420 — the `hard_ineligible`
  pre-label logic to mirror (NOT import) in `src/extract/sponsorship.py`.
- `profile/sponsorship_rules.yaml` — the sponsorship rule lists.
- `src/discovery/cleaning.py` — how `clean.parquet` is read/written; the
  `job_id`/`description` columns extraction keys off.

---

## 1. Target architecture (end state)

```
src/llm/
  __init__.py
  base.py           # Backend ABC, Conversation ABC, Reply, Usage, LLMError
  jsonparse.py      # tolerant JSON array/object parse (moved from judge.py)
  config.py         # profile/llm.yaml loader -> LLMConfig, RoleConfig
  ledger.py         # per-run token/cost ledger
  claude_cli.py     # claude -p backend (generalized from judge.py)
  openai_compat.py  # OpenAI-compatible backend (Ollama / vLLM), grammar-JSON
  anthropic_api.py  # Anthropic Messages API backend (synchronous)
  fake.py           # scripted backend for tests
  factory.py        # get_backend(role_cfg) -> Backend
src/extract/
  __init__.py
  __main__.py       # `uv run python -m src.extract` -> extract_cli.main
  extract_cli.py    # arg parsing, run modes, ledger print
  schema.py         # pydantic Sidecar models + EXTRACTOR_SCHEMA_VERSION + guided schema
  prompt.py         # system prompt + optional few-shot + user-prompt builders
  sponsorship.py    # hard_ineligible deterministic pre-label
  validate.py       # pydantic + two-tier containment + batch coverage validators
  cache.py          # extracted.parquet read/write + cache-key + status/attempts
  runner.py         # batch planning, inline retry loop, concurrency, dump/ingest
hpc/
  README_explorer.md   # one-time setup runbook (module loads, apptainer, HF cache)
  extract.sbatch       # in-job: vllm serve -> extract_client.py -> results.jsonl
  extract_client.py    # standalone on-node client (requests only, no repo deps)
  run_explorer.sh      # local wrapper: rsync up / sbatch / poll / rsync down
profile/
  llm.yaml            # NEW, gitignored; committed example: llm.example.yaml
  sponsorship_rules.yaml   # existing, unchanged
jobs/
  extracted.parquet   # NEW sidecar cache (§7)
  runs/<ts>_extract.md  # extraction's OWN run report (§8); never appended to discovery's
evals/
  extraction/
    jds/*.md          # held-out sanitized JDs (easy -> hardest)
    expected/*.json   # expected sidecar per JD
    test_extraction_evals.py
staging/               # gitignored scratch for dump/ingest mode files
```

Run order per day (unchanged upstream): discovery → **extraction (this plan)** →
(future: embed → score → review). Extraction processes only `clean.parquet`
rows lacking a valid `ok` sidecar (§7 cache).

---

## 2. Provider adapter (`src/llm/`)

### 2.1 `profile/llm.yaml` contract (committed example `llm.example.yaml`)

```yaml
schema_version: 1
provider: claude-cli            # default backend for all roles
roles:
  extract:
    model: claude-haiku-4-5-20251001          # any model id; e.g. claude-sonnet-5
    # --- optional per-role overrides ---
    # provider: openai-compatible
    # base_url: "http://localhost:11434/v1"   # Ollama; vLLM in-job uses :8000
    # api_key_env: OPENAI_API_KEY             # env var NAME, not the key
    # structured_output: ollama               # ollama | vllm | openai | none
    # jds_per_call: 1
    # timeout: 600
    # workers: 4
    # hpc_model: Qwen/Qwen3.5-9B              # served model for the HPC dump/sbatch
    #                                         # path ONLY; `dump` uses it in place of
    #                                         # `model`, so local inline can stay on
    #                                         # claude-cli while HPC serves vLLM
    #                                         # without editing this file. Falls back
    #                                         # to `model` when absent.
```

`llm.yaml` is gitignored per-user config: set `model:` to any id (Haiku, Sonnet,
a local Qwen, etc.). API-key VALUES live only in the env var named by
`api_key_env`, never in this file.

Backend defaults (used when the role omits the key):
- `jds_per_call`: claude-cli = 10, anthropic = 10, openai-compatible = 1.
- `workers`: claude-cli = 4, anthropic = 4, openai-compatible = 1.
- `timeout`: 600 seconds.
- `structured_output`: openai-compatible only; no default — REQUIRED for
  openai-compatible (raise if missing). Ignored by other backends.

### 2.2 `config.py`

- `RoleConfig` dataclass: `role, provider, model, base_url, api_key_env,
  structured_output, jds_per_call, workers, timeout`.
- `LLMConfig.role(name) -> RoleConfig`: resolves `provider` = role override else
  top-level; fills backend defaults (2.1).
- Model/provider per role come ONLY from the gitignored `profile/llm.yaml` — a
  user picks Haiku, Sonnet, Qwen, etc. by editing that file. NEVER hardcode a
  model id in `src/`. API-key VALUES live only in the env var named by
  `api_key_env`; the key is never written to `llm.yaml` or the repo.
- Loader behaviour (exact):
  - `profile/llm.yaml` missing → raise `LLMError` naming the file and pointing to
    `llm.example.yaml`. (No silent default provider — cost/backend must be explicit.)
  - Malformed YAML → `ValueError`.
  - `provider` not in {`claude-cli`, `openai-compatible`, `anthropic`} → `ValueError`.
  - `provider: openai-compatible` with no `base_url` → `ValueError`.
  - `provider: openai-compatible` with no `structured_output` → `ValueError`.
  - Unknown key under a role → `ValueError`.
- Tests: each error path; a valid claude-cli-default file resolves `extract`;
  a per-role openai-compatible override resolves base_url + structured_output.

### 2.3 `base.py`

```python
@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0        # 0.0 when unknown (local models)

@dataclass
class Reply:
    text: str
    usage: Usage

class LLMError(Exception):
    """Fatal, non-retryable: missing config, CLI absent, endpoint down, auth."""

class Conversation(ABC):
    @abstractmethod
    def send(self, user_message: str) -> Reply: ...
    # A stateful turn. Implementations remember prior turns so the runner can
    # append validator feedback as the next user message (retry-with-feedback).

class Backend(ABC):
    name: str
    @abstractmethod
    def start(self, system_prompt: str, guided_schema: dict | None) -> Conversation: ...
    # guided_schema is the JSON Schema for one Sidecar object (or an array of
    # them for batched backends). Backends that cannot grammar-constrain ignore it.
```

- The runner NEVER inspects backend type; it only calls `start()` then `send()`.
- All backends raise `LLMError` on setup/transport failure (fatal). A *bad model
  answer* is NOT an exception — it is returned as text and rejected by the
  validator, which drives the retry.

### 2.4 `ledger.py`

- `Ledger`: accumulates `Usage` per `(stage, model)`; `add(stage, model, usage)`;
  `render() -> str` returns a table (stage, model, calls, in/out/cache tokens,
  $cost) + a total line. Printed at end of run in the discovery-report style.
- Test: two adds under the same key aggregate; render includes a total row.

### 2.5 `claude_cli.py` — generalize `judge.py`'s call path

- `ClaudeCLIBackend(model, timeout)`; `start()` returns a `ClaudeCLIConversation`
  holding `system_prompt`, `model`, `timeout`, `session_id=None`.
- `send(user_message)`:
  - Build cmd exactly like `judge.call_claude`: `claude -p --output-format json
    --model <model> --max-turns 1 --tools "" --system-prompt <system>
    --setting-sources "" --effort medium --max-budget-usd 0.50`.
  - First call: pass `--system-prompt`. Subsequent calls (session_id set):
    add `--resume <session_id>` (the system prompt rides the session; keep
    passing `--system-prompt`, harmless, matches judge).
  - stdin = `user_message`. Strip `CLAUDECODE` / `CLAUDE_CODE_ENTRYPOINT` from env.
  - Parse the JSON envelope: `result`, `session_id`, `total_cost_usd`, `usage`.
    Store `session_id`. Map usage → `Usage` (cost from `total_cost_usd`).
  - `FileNotFoundError` → `LLMError` (CLI not installed / not authenticated).
    Non-zero exit → `LLMError` with stderr head. Timeout → `LLMError`.
  - `guided_schema` ignored (claude-cli cannot grammar-constrain).
- MOVE `judge._extract_json_array` to `src/llm/jsonparse.py`; `judge.py` imports
  it from there. This is the ONLY permitted edit to `judge.py`; it must stay
  behaviourally identical — re-run `uv run pytest tests/` to confirm.
- Tests (fake `subprocess`): envelope parsed; session_id threaded on 2nd send;
  non-zero exit → LLMError.

### 2.6 `openai_compat.py` — Ollama + vLLM, one backend

- `OpenAICompatBackend(base_url, model, api_key_env, structured_output, timeout)`.
- `start()` returns a conversation holding `messages=[{"role":"system",...}]`,
  plus `guided_schema`.
- `send(user_message)`:
  - Append `{"role":"user","content":user_message}`.
  - POST `<base_url>/chat/completions` via `requests` with
    `{"model", "messages", "temperature": 0, "stream": false}` plus the
    grammar-constraint payload keyed by `structured_output`:
    - `ollama`  → top-level `"format": guided_schema`
    - `vllm`    → top-level `"guided_json": guided_schema` (vLLM's OpenAI server
      accepts it in the request body — confirm shape per R6 with curl first)
    - `openai`  → `"response_format": {"type":"json_schema","json_schema":
      {"name":"sidecar","schema":guided_schema}}`
    - `none`    → no payload (plain JSON via prompt + validation only)
  - `Authorization: Bearer <env[api_key_env]>` only if `api_key_env` set.
  - Parse `choices[0].message.content` → `Reply.text`. GUARD: 2xx but `choices`
    empty/missing or `content` null → return `Reply(text="", ...)` (drives a
    retry), never IndexError/KeyError. Map `usage`
    (`prompt_tokens`/`completion_tokens`) → `Usage`; `cost_usd=0.0` (local/unknown).
  - Append `{"role":"assistant","content":text}` so the next `send()` (feedback)
    carries history (stateless server; history resent each call).
  - Connection error / non-2xx → `LLMError`.
- R6 gate: before writing the parser, `curl` one real `/chat/completions` against
  the configured endpoint with a tiny `guided_json`/`format` and record the shape.
- Tests (fake `requests`): each `structured_output` builds the right payload;
  history grows across sends; non-2xx → LLMError.

### 2.7 `anthropic_api.py` — synchronous Messages (Batch API DEFERRED)

- `AnthropicBackend(model, api_key_env="ANTHROPIC_API_KEY", timeout)`.
- `send`: POST `https://api.anthropic.com/v1/messages` via `requests` with
  headers `x-api-key`, `anthropic-version: 2023-06-01`; body `{"model",
  "max_tokens", "system", "messages", "temperature":0}`. History resent per call.
  `max_tokens = 2048 * jds_per_call` (headroom so a full sidecar-per-JD never
  truncates; truncation would surface as unparseable JSON → retry, but sizing it
  right avoids the wasted call). openai-compatible sends the same
  `max_tokens` value; claude-cli sizing is handled by the CLI.
- Parse `content[0].text` → `Reply.text`. GUARD: `content` empty or first block
  not text (e.g. a `stop_reason` with no text) → return `Reply(text="", ...)`
  (drives a retry), never index-crash. Map `usage` (`input_tokens`,
  `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`) →
  `Usage`; `cost_usd=0.0` (no per-call cost returned; leave 0).
- Missing `ANTHROPIC_API_KEY` → `LLMError`. Non-2xx → `LLMError`.
- SEAM (do not implement, leave the hook): a module docstring noting that an
  async Batch runner (submit/poll/retrieve, 50% off) plugs in here later behind a
  role flag; this plan ships synchronous only.
- Tests (fake `requests`): body/headers correct; missing key → LLMError.

### 2.8 `factory.py`

- `get_backend(role_cfg) -> Backend`: dispatch on `role_cfg.provider`. No I/O
  besides constructing the backend. Test: each provider → correct class; unknown
  → `LLMError`.

### 2.9 `fake.py` (tests only)

- `FakeBackend(scripted_replies: list[str])`: `start()` returns a conversation
  that returns the scripted replies in order (raising if exhausted), recording
  every `system`/`user` it saw. Enables the whole runner + validator + retry loop
  to be tested with zero live calls (R8).

---

## 3. Extraction schema (`src/extract/schema.py`)

`EXTRACTOR_SCHEMA_VERSION = 1` (int constant; bump invalidates the cache, §7).

Closed pydantic models (Python 3.12 `| None` syntax). Evidence is REQUIRED
conditionally, enforced by a `model_validator` (`mode="after"`):

```python
class ExperienceClause(BaseModel):
    years: int
    degree: str | None = None
    evidence: str                      # always required

class EvidencedStr(BaseModel):         # education, location_constraint
    value: str | None = None
    evidence: str | None = None
    # validator: value is not None  <=>  evidence is a non-empty str

class EvidencedBool(BaseModel):        # clearance_required, citizenship_required
    value: bool
    evidence: str | None = None
    # validator: value is True  =>  evidence is a non-empty str
    #            value is False =>  evidence is None

class HardRequirements(BaseModel):
    experience_clauses: list[ExperienceClause]
    education: EvidencedStr
    clearance_required: EvidencedBool
    citizenship_required: EvidencedBool
    location_constraint: EvidencedStr

class Sponsorship(BaseModel):
    value: Literal["sponsors", "opt_ok", "ineligible", "unknown"]
    evidence: str | None = None
    # validator: value != "unknown"  =>  evidence is a non-empty str

class Sidecar(BaseModel):
    job_id: str                        # echoed by the model; batch-coverage keyed on it
    hard_requirements: HardRequirements
    skills_required: list[str]
    skills_nice: list[str]
    domain: str                        # free text, no taxonomy
    seniority: Literal["intern", "junior", "mid", "senior", "staff+", "manager"]
    role_type: Literal["ic", "manager"]
    sponsorship: Sponsorship
```

- NO `vertical` field (extraction is vertical-independent, R2).
- `model_config = ConfigDict(extra="forbid")` on every model (closed schema, R4).
- `guided_schema_single() -> dict` = `Sidecar.model_json_schema()`.
  `guided_schema_batch(n) -> dict` = `{"type":"array","items":<single>}`.
- Tests: valid full object parses; `extra` key rejected; `EvidencedBool(value=True,
  evidence=None)` rejected; `Sponsorship(value="sponsors", evidence=None)` rejected;
  `experience_clauses` accepts two clauses; bad `seniority` enum rejected.

---

## 4. Prompt (`src/extract/prompt.py`)

- `SYSTEM_PROMPT` (a constant string) instructs: you are a JD fact-extractor;
  read ONLY the title + description; output the Sidecar JSON; rules:
  - Every `evidence` field is a VERBATIM quote copied from the description.
  - `experience_clauses`: emit ONE clause per alternative. "5+ yrs OR 3+ with a
    Master's" → two clauses. Never collapse to a single number.
  - `skills_required`/`skills_nice`: copy skill phrases VERBATIM from the
    description (do not expand abbreviations; "LLMs" stays "LLMs").
  - `seniority`/`role_type`/`domain` are judgments; the rest are extractions.
  - Sponsorship labelling rules block: embed the precedence
    (`ineligible > opt_ok > sponsors > unknown`) and the `false_positive_guard`
    note ("must be authorized to work in the US" ALONE is NOT ineligible) from
    `profile/sponsorship_rules.yaml`. Load these lists at build time and render
    them into the prompt (so the rules stay single-sourced).
- `FEW_SHOT`: exactly ONE hand-authored (JD → Sidecar JSON) example, DISTINCT
  from every eval JD (R8 / user: golden set is held-out). Keep it in this module
  as a constant. Do not add more unless step 11 shows a local model failing.
- `build_system_prompt() -> str`: SYSTEM_PROMPT + sponsorship rules + FEW_SHOT.
  Fails loud (`LLMError`) if `sponsorship_rules.yaml` is missing/empty — never
  builds a prompt with a blank sponsorship-rules block.
- `build_user_prompt(rows: list[dict]) -> str`: for each row emit
  `job_id`, `title`, `description` (nothing else). For a batch of N, instruct
  "output a JSON array of exactly N objects, one per posting, echoing each
  `job_id`." For N=1, instruct "output a single JSON object."
- Test: prompt contains the false-positive-guard phrase; user prompt for 2 rows
  names both job_ids and excludes company/location.

---

## 5. Sponsorship pre-label (`src/extract/sponsorship.py`)

Mirror (do NOT import) `scoring_io.load_hard_ineligible` / `hard_ineligible_phrase`:
- `load_hard_ineligible(path=profile/sponsorship_rules.yaml) -> tuple[str,...]`:
  lowercased `hard_ineligible` list; empty/malformed → `ValueError`.
- `prelabel(description: str, phrases) -> str | None`: first case-insensitive
  substring hit → return the matched phrase, else None.
- Used by the runner (step 8): before the model call, if `prelabel` hits, remember
  the matched phrase for that row. AFTER the model returns and the sidecar
  validates, if the row was pre-labelled, OVERWRITE `sidecar.sponsorship` with
  `{value:"ineligible", evidence:<matched phrase>}` (the model's sponsorship for
  that row is discarded). Pre-labelled ineligible always wins. Free, deterministic,
  R1-safe. Set `sponsorship_prelabeled=True` in the cache row (§7).
- Tests: a `hard_ineligible` phrase hits; "must be authorized to work in the US"
  alone does NOT hit (it is not in `hard_ineligible`).

---

## 6. Validator (`src/extract/validate.py`) — deterministic, NO LLM

Normalization used everywhere: `norm(s)` = NFKC → casefold → strip markdown
backslash-escapes (`re.sub(r"\\(.)", r"\1", s)`) → collapse all whitespace runs
to single spaces → strip. (JobSpy renders `jd_text` as markdown, so JD
punctuation arrives escaped, e.g. `computer\-aided design`, `5\+ years`; the
model outputs the clean form, so un-escaping the JD side is required for
substring containment to hold.)

`validate_sidecar(obj, description: str) -> tuple[Sidecar | None, list[str]]`:
0. If `obj` is not a dict (None, list, str, number) → return
   `(None, ["model output is not a JSON object"])`. Never raise.
1. Parse via `Sidecar(**obj)` (shape, enums, conditional-evidence). Pydantic
   error → return `(None, [str(err)])`.
2. Two-tier containment (`errors: list[str]`, collect all):
   - For EVERY present `evidence` string `e`: require `norm(e) in norm(description)`;
     else `"evidence not found verbatim in JD: <e[:60]>"`.
   - `experience_clauses[i].years`: require the count appears in the evidence as
     its own token, EITHER as the digit OR its spelled-out word (`_years_pattern(years)`
     = `rf"\b({years}|{word})\b"` for 0–12 via a `_WORD_NUM` map, else `rf"\b{years}\b"`);
     else error. (JDs say "three years" as often as "3 years"; the digit-only
     check produces false negatives on correct extractions.)
   - `education.value` (if not None): require `norm(value) in norm(evidence)`.
   - `location_constraint.value` (if not None): require `norm(value) in norm(evidence)`.
   - `clearance_required` / `citizenship_required` when True: evidence⊆JD only
     (the boolean is a judgment about the quote; no value-in-evidence check).
   - `sponsorship` when `value != "unknown"`: evidence⊆JD only.
   - EACH `skills_required` / `skills_nice` string `s`: require
     `norm(s) in norm(description)`; else `"skill not found in JD: <s>"`.
     (This is the anti-fabrication guarantee: skills are verbatim-grounded.)
3. Return `(sidecar, [])` if no errors, else `(None, errors)`.

`validate_batch(objs: list[dict], rows: list[dict]) -> tuple[dict[str, Sidecar], dict[str, list[str]]]`:
- `rows` are the exact input rows (each has `job_id`, `description`).
- Coverage (mirror `judge.validate_batch`): every input `job_id` present exactly
  once; no duplicates; no strays. Missing/dupe/stray → per-job errors.
- For each returned object, run `validate_sidecar` against ITS OWN row's
  description (matched by echoed `job_id`).
- Return `(valid_by_job_id, errors_by_job_id)`.
- Tests: verbatim-evidence pass; fabricated evidence fail; fabricated skill fail;
  `years` not in its evidence fail; two-clause experience passes; missing job_id
  flagged; stray job_id flagged; "LLMs" extracted where JD says "large language
  models" → fails (documented, expected); markdown-escaped skill in JD (`computer\-aided
  design`) vs clean model output → passes; spelled-out years ("three years", years=3)
  → passes; `validate_sidecar(None, ...)` and `validate_sidecar([...], ...)` return
  an error, never raise.

---

## 7. Cache (`src/extract/cache.py`) — `jobs/extracted.parquet`

Columns (flat + one JSON-string sidecar column; R4):
| column | type | note |
|---|---|---|
| job_id | str | |
| description_sha1 | str | `sha1(description.encode())` hex |
| schema_version | int | `EXTRACTOR_SCHEMA_VERSION` at extraction time |
| status | str | `ok` \| `extraction_failed` |
| attempts | int | cumulative extraction attempts across runs |
| model | str | model id used |
| extracted_at | pd.Timestamp | UTC |
| sidecar_json | str | validated `Sidecar` JSON; `""` when status=`extraction_failed` |
| sponsorship_prelabeled | bool | pre-label set the sponsorship deterministically |

- `cache_key(row) = (job_id, sha1(description), EXTRACTOR_SCHEMA_VERSION)`.
- `load(path) -> DataFrame` (empty typed frame if absent).
- `needs_extraction(clean_df, cache_df) -> DataFrame` of rows to process:
  a clean row needs extraction unless a cache row matches its key AND
  `status == "ok"`. A key match with `status == "extraction_failed"` AND
  `attempts < MAX_EXTRACT_ATTEMPTS` (=3) → RE-attempt. `attempts >= 3` →
  sticky failed, SKIP (do not re-attempt, do not re-spend).
- `upsert(cache_df, records) -> DataFrame`: replace-by-`job_id`
  (append-or-overwrite); write atomically (temp file + rename).
- Rows whose `job_id` left the 14-day window are KEPT (cache forever, §12).
- Tests: new row needs extraction; unchanged `ok` row skipped; changed
  description (new sha1) re-extracts; schema bump re-extracts all; failed row
  under attempt cap re-attempts; failed row at cap is skipped; upsert overwrites
  one job_id and leaves others.

---

## 8. Runner (`src/extract/runner.py`)

Constants: `MAX_RETRIES = 2` (in-run retry-with-feedback per batch),
`MAX_EXTRACT_ATTEMPTS = 3` (cross-run cap, step 7).

`plan_batches(rows, jds_per_call) -> list[list[dict]]`: slice `needs_extraction`
rows into batches of `jds_per_call` (never truncate a JD; §OUT).

`extract_batch(batch, backend, ledger) -> dict`:
- Build system prompt once (`prompt.build_system_prompt`).
- `guided = guided_schema_single()` if `len(batch)==1` else `guided_schema_batch(len(batch))`.
- `conv = backend.start(system, guided)`.
- Before the loop: `accumulated = {}` (job_id → Sidecar, valid in some attempt),
  `remaining = {every batch job_id}`.
- Attempt loop (≤ `MAX_RETRIES+1`):
  - `reply = conv.send(user_prompt or feedback)`; `ledger.add("extract", model, reply.usage)`.
  - Parse array/object from `reply.text` (`src/llm/jsonparse`), coerce single
    object → 1-element list. DEGENERATE OUTPUT (empty/whitespace `reply.text`,
    JSON `null`, a scalar/string instead of object/array, or unparseable/truncated
    JSON) is NOT an exception here: catch it, set `objs = []`, set `feedback =
    "output was empty or not valid JSON; return the JSON object(s) only"`, and
    loop. Never let a bad response crash the batch.
  - `valid, errors = validate_batch(objs, batch)`.
  - For each `jid` in `valid` that is still in `remaining`: apply the sponsorship
    pre-label override (step 5), move it into `accumulated`, drop from `remaining`.
    (Accumulate across attempts — a row valid in attempt 1 stays staged even if a
    batch-mate keeps failing; NEVER discard already-valid rows.)
  - If `remaining` is empty → return `{valid: accumulated, failed: []}`.
  - Else set `feedback` = the collected validator error lines for the STILL-remaining
    job_ids (judge `_RETRY_PROMPT` style) and loop.
- After the loop: return `{valid: accumulated, failed: sorted(remaining)}`.
- Per-row staging rule: a `job_id` that validated in ANY attempt is staged `ok`;
  a `job_id` never valid after all attempts is recorded `extraction_failed` with
  `attempts += 1` (never defaulted). NEVER raise on bad model output.
- Each staged record is a dict with exactly the §7 columns: `job_id,
  description_sha1, schema_version, status, attempts, model, extracted_at,
  sidecar_json, sponsorship_prelabeled`. `attempts` = prior cache `attempts`
  (0 if new) + 1. `sidecar_json` = `Sidecar.model_dump_json()` for `ok`, `""`
  for `extraction_failed`.

`run(mode, ...)` — shared pre-filter (all modes): read `clean.parquet`
(MISSING file → `LLMError` naming it; EMPTY frame → write report with the
ZERO-rows flag and stop, no crash). Drop rows whose `description` is empty or
whitespace-only BEFORE `needs_extraction` (never spend a call on an empty JD);
count them as a `skipped-empty-description` report line.
- `--mode inline` (default): `cache.needs_extraction`, `plan_batches`, fan out
  `extract_batch` over a `ThreadPoolExecutor(workers)` reusing judge's warm-up
  pattern (first batch alone to write the prompt cache for claude-cli, then the
  rest fan out), then write report + print ledger. `workers=1` for
  openai-compatible (single local endpoint). INCREMENTAL FLUSH: `cache.upsert`
  each batch's records as it completes (guarded by a lock), not once at the end,
  so a mid-run abort keeps completed rows and the next run resumes via the cache.
- `--mode dump --out staging/`: build `staging/extract_meta.json`
  (`{system, guided_schema: guided_schema_single(), model}`) + `staging/prompts.jsonl`
  (one line per JD: `{"job_id","description_sha1","user":<user_prompt for that
  one JD>}`) for all `needs_extraction` rows (create `staging/` if absent; zero
  rows → write an empty `prompts.jsonl` + `meta`, report ZERO). NO LLM call.
  Stops. (Explorer path is always 1 JD/line for reliability.)
- `--mode ingest --results staging/results.jsonl`: read results
  (`{"job_id","raw_output"}` per line), match to `clean.parquet` rows by job_id,
  `validate_sidecar` each, apply pre-label, `cache.upsert` (`ok`/`extraction_failed`
  with attempt increment), write report, print ledger. NO LLM call.
  Edge cases (all reported, none fatal): a non-JSON LINE in results.jsonl → skip
  + `malformed-result-line` count (never crash the whole ingest); a results
  `job_id` not in `clean.parquet` → skip + `stale-result` count; a `job_id` that
  was dumped but is ABSENT from results (partial HPC run) → left uncached,
  re-dumps next run (no record written); an empty/`null`/unparseable `raw_output`
  → `extraction_failed` + attempt increment (same path as a degenerate inline
  response).
- Report: extraction writes its OWN file `jobs/runs/<ts>_extract.md`
  (`<ts>` = local start time `%Y-%m-%d_%H%M`; do NOT append to discovery's
  report). Contents: counts of new / re-attempted / ok / failed / sticky-skipped;
  per-model call+token+cost from the ledger; a ZERO-rows flag; timing.
- `LLMError` (fatal setup) propagates and aborts before any cache write.
- Tests (FakeBackend, R8): a batch all-valid stages ok; one bad row retries then
  fails and is recorded `extraction_failed` while its batch-mates stage ok;
  pre-labelled ineligible row overwrites model sponsorship; dump writes
  prompts.jsonl + meta; ingest of a hand-written results.jsonl writes cache;
  inline run over a 3-row fixture with a scripted fake produces 3 ok rows.
  Edge cases (all mandatory): scripted `""` then valid → retried then ok (no
  crash); scripted `"null"` all attempts → `extraction_failed` (no crash);
  a whitespace-only-description row is skipped pre-call (0 backend calls);
  missing `clean.parquet` → `LLMError`; empty `clean.parquet` → ZERO-flag report,
  no crash; a fake that raises after 2 of 3 batches leaves those 2 flushed in the
  cache (incremental flush); ingest with a stale job_id skips it; ingest with an
  empty `raw_output` records `extraction_failed`.

---

## 9. CLI + wiring (`src/extract/extract_cli.py`, `__main__.py`, pyproject)

- `extract_cli.main(argv=None)`: subcommand `run` with `--mode {inline,dump,ingest}`
  (default inline), `--out` (dump), `--results` (ingest), `--limit` (optional cap
  for smoke tests only; default none — never cap in normal use, §2).
- `__main__.py`: `from .extract_cli import main; main()`.
- `pyproject.toml [project.scripts]`: add `extract = "src.extract.extract_cli:main"`
  (mirror `score`/`tailor`). Add `pydantic` to dependencies.
- Verify: `uv run extract run --mode inline --limit 2` end-to-end against the
  configured backend (Phase-2 step 2.6 runs this live smoke on claude-cli).

---

## 10. Explorer (Northeastern HPC) transport — apptainer vLLM, in-job server

Recon facts (VERIFIED live, do not re-derive): GPUs live on the `gpu` /
`gpu-short` / `gpu-interactive` partitions (8h / 2h / 2h) — the `short` partition
is CPU-ONLY (gres null), never use it for the job. Production target = a single
`v100-sxm2` (Tesla V100-SXM2-32GB, 32 GB VRAM, abundant/`mix` state) which fits
Qwen3.5-9B FP16; A100/H200 also exist but are FULLY GPU-saturated (2026-07-21 recon:
0 free A100s, all 32 H200s allocated despite `mix` node state). `apptainer` 1.4.5 at
`/usr/bin/apptainer`; `docker://` pull works; login node has internet; `/scratch`
is 871 TB free (VAST, no xattr support). The served model is NOT hardcoded: it is
read from the `model` field of `extract_meta.json` (which the local `dump` step
wrote from `profile/llm.yaml`), so `llm.yaml` stays the single source of truth.

### 10.1 `hpc/README_explorer.md` — ONE-TIME setup (cached forever)

Document exact commands:
1. Persistent roots (point apptainer's temp+cache at `/scratch`, else the ~10GB
   OCI→SIF build hits small `/tmp`/`$HOME`):
   `export EXPLORER_ROOT=/scratch/$USER/extract`
   `export APPTAINER_TMPDIR=$EXPLORER_ROOT/tmp APPTAINER_CACHEDIR=$EXPLORER_ROOT/cache`
   `mkdir -p "$EXPLORER_ROOT" "$APPTAINER_TMPDIR" "$APPTAINER_CACHEDIR"`.
   (`/scratch` is 871 TB but may be purged; container/model re-download cheaply.)
2. Pull the vLLM container once, INSIDE `tmux`/`screen` so an SSH disconnect can't
   kill the multi-minute build (`tmux new -s vllmpull`; run the pull; detach with
   Ctrl-b d; reattach `tmux attach -t vllmpull`):
   `apptainer pull $EXPLORER_ROOT/vllm-openai.sif docker://vllm/vllm-openai:v0.19.1`
   The `xattr ... ENOTSUP` / `rootless` warnings on VAST are HARMLESS. The final
   `.sif` (~10GB) appears only at the `Creating SIF file` line at the very end; a
   killed pull leaves NO valid sif — re-running reuses the cached blobs (fast).
   TAG CHOICE (research 2026-07-21, supersedes the `:latest` blocker): pin
   `vllm/vllm-openai:v0.19.1` — the unsuffixed tag is the CUDA 12.9.1 default build
   (its only suffixed variant `-cu130` is the CUDA-13 alt — do NOT use it). It is the
   one tag satisfying a THREE-way collision (only v0.17.0–v0.19.1 does):
   (a) Qwen3.5 arch first added in vLLM v0.17.0; (b) Volta `sm_70` kernels present
   only ≤v0.19.x — DROPPED at v0.20.0 (v0.19.1 image `TORCH_CUDA_ARCH_LIST` =
   "7.0 7.5 8.0 8.9", V100 is in; v0.20.0+ incl. `v0.25.1-cu129` omit sm_70 → would
   clear the driver error then die "no kernel image for sm_70"); (c) CUDA major must
   be 12 — the `:latest` failure was CUDA 13 (libcuda.580) crossing the 12→13 MAJOR
   boundary (Error 803), and forward-compat only works WITHIN a major. v0.19.1 =
   CUDA 12.9 > native 12.3 so it STILL needs the compat prepend, but the compat lib
   is CUDA-12.9 (libcuda ~575), a within-major-12 jump from 545, so this time it
   should load (V100 is datacenter-class; 545 ≥ R525 min base for cuda-compat-12-x).
   Residual one-liner to verify at smoke: cuda-compat-12-9 accepts base driver 545.
   Launch under `apptainer exec --nv` with prepend + FP16 (no bf16 on Volta):
   `bash -c 'export LD_LIBRARY_PATH=/usr/local/cuda/compat:$LD_LIBRARY_PATH; export VLLM_ATTENTION_BACKEND=XFORMERS; exec vllm serve "$MODEL" --port 8000 --dtype float16'`
   (XFORMERS or TORCH_SDPA — FlashAttention/FlashInfer need sm_80+, unusable on V100).
   FUTURE (your check, §10.2): if A100/H200 nodes carry a newer driver (≥575 for
   CUDA 12.9, ≥580 for CUDA 13) they'd run `:latest`/`v0.25.1-cu129` NATIVELY with
   full Qwen3.5 (sm_80/sm_90 NOT dropped) — ideal if schedulable despite contention.
3. Pre-download the model once into a persistent HF cache. Use the SAME model id
   set in `profile/llm.yaml`'s `extract` role — chosen model `Qwen/Qwen3-8B`
   (FP16, text-only instruct; fits V100-SXM2-32GB). MODEL REVERTED 2026-07-21 from
   Qwen3.5-9B → Qwen3-8B: Qwen3.5 is multimodal and stock vLLM does NOT cleanly serve
   it on V100 (needs vLLM-from-source + PR #36026 + `--mm-encoder-attn-backend
   TORCH_SDPA`, i.e. the 1Cat-vLLM fork territory — violates container-only). Extraction
   is text-only, so multimodality was never needed and was the sole cause of the V100
   break; text-only Qwen3-8B runs cleanly on v0.19.1 (sm_70 + XFORMERS). Download via
   the container's `hf` (the container's `huggingface_hub` 1.23 killed `huggingface-cli`;
   use `hf download`) and run on a COMPUTE node — the LOGIN node OOM-kills the download:
   `export HF_HOME=$EXPLORER_ROOT/hf_cache`;
   `srun --partition=short --cpus-per-task=4 --mem=16G apptainer exec --bind /scratch "$EXPLORER_ROOT/vllm-openai.sif" hf download Qwen/Qwen3-8B`.
   (The cached Qwen3.5-9B at `$HF_HOME/hub/models--Qwen--Qwen3.5-9B` can be deleted.)
4. Every job runs OFFLINE: `HF_HUB_OFFLINE=1`, reading `$HF_HOME`.

### 10.2 `hpc/extract.sbatch` — self-contained job (no tunnel)

- `#SBATCH` : `--partition=gpu`, `--gres=gpu:v100-sxm2:1`, `--time=08:00:00`,
  `--cpus-per-task=8`, `--mem=64G`, job name, `-o`/`-e` logs. (NEVER `--partition=short`
  — CPU-only. `v100-sxm2` = 32 GB, abundant, fits Qwen3.5-9B FP16.)
- Body:
  1. `export HF_HOME=$EXPLORER_ROOT/hf_cache HF_HUB_OFFLINE=1`; also
     `export APPTAINERENV_HF_HOME=$HF_HOME APPTAINERENV_HF_HUB_OFFLINE=1` so the
     value is guaranteed inside the container (do not rely on default env
     pass-through for a load-bearing var).
     `MODEL=$(jq -r .model extract_meta.json)` — the served model id, written by
     `dump` from `llm.yaml.extract.hpc_model` (falls back to `model`; see §2.1/3.1).
  2. Launch the server in the container, background, bound to `127.0.0.1:8000`.
     [VERIFY FIRST at step 3.3 (R6): run `apptainer exec "$SIF" vllm serve --help`
     against the pulled image — newer images use `vllm serve "$MODEL"`, older ones
     the `python -m` module form below. Use whichever the image supports.]
     `apptainer exec --nv --bind /scratch $EXPLORER_ROOT/vllm-openai.sif \
        vllm serve "$MODEL" --port 8000 &`   # or: python -m vllm.entrypoints.openai.api_server --model "$MODEL" --port 8000
     [`--bind /scratch` is REQUIRED (verified 2026-07-21): Explorer does not
     auto-bind `/scratch` into the container, so without it `vllm serve` cannot
     read the HF cache at `$HF_HOME` and the job fails with "missing HF cache".]
  3. Poll `http://127.0.0.1:8000/health` until 200 (timeout guard).
  4. Run the on-node client against the local server:
     `apptainer exec --nv --bind /scratch $EXPLORER_ROOT/vllm-openai.sif \
        python hpc/extract_client.py --prompts prompts.jsonl \
        --meta extract_meta.json --out results.jsonl \
        --base-url http://127.0.0.1:8000/v1`
  5. Kill the server; exit. (Everything happens inside one job; the server never
     outlives it.)

### 10.3 `hpc/extract_client.py` — standalone, `requests`-only (no repo import)

- Reads `extract_meta.json` (`system`, `guided_schema`, `model`) + `prompts.jsonl`
  (per-line `{job_id, user}`). Per line: POST `/chat/completions` with
  `temperature:0` and the vLLM grammar payload (`guided_json` = meta schema).
- CONCURRENCY (mandatory; do not reduce to a sequential loop — that blows the
  8h walltime on a large backlog): fire `--concurrency N` requests in parallel
  (default 32) via a thread pool so vLLM's continuous batching saturates the GPU.
- Append `{job_id, raw_output}` to `results.jsonl` as each request returns, and
  FLUSH per line — so a walltime kill leaves a valid partial file (completed JDs
  survive, the rest re-dump next run). One sample per JD; NO on-node validation
  (authoritative validation is the local `--mode ingest`).
- Kept dependency-light (`requests` + `concurrent.futures`, both stdlib-ish) so it
  runs inside the container unchanged.

### 10.4 `hpc/run_explorer.sh` — LOCAL wrapper

- `uv run extract run --mode dump --out staging/` (local).
- `rsync staging/{prompts.jsonl,extract_meta.json} explorer:<jobdir>/`.
- `ssh explorer "cd <jobdir> && sbatch hpc/extract.sbatch"`; poll `squeue` until
  the job leaves the queue, then read its terminal state (`sacct`).
- `rsync explorer:<jobdir>/results.jsonl staging/` and ALWAYS ingest whatever is
  there — ingest is per-line-safe (each line validated independently; truncated
  last line skipped; missing job_ids re-dump). Do NOT gate ingestion on job state:
  - COMPLETED → normal.
  - TIMEOUT → partial results.jsonl; completed JDs ingest, the rest re-dump next
    night (backlog resumes across runs, no work lost).
  - FAILED/OOM with an EMPTY results.jsonl → nothing to ingest, all rows re-dump;
    print a LOUD warning + the sbatch log path so the user fixes the setup
    (usually model-too-big-for-GPU or a missing HF cache).
- `uv run extract run --mode ingest --results staging/results.jsonl` (local).

- Verify (user-run; mark `[NEEDS USER]` if unattended): one small
  `dump → sbatch → ingest` cycle over ~20 rows; confirm `results.jsonl` returns,
  ingest writes `extracted.parquet` rows with `status=ok`, evidence spans
  validate against the JDs.

---

## 11. Minimal golden-set evals (`evals/extraction/`)

- 5–10 sanitized JDs (`evals/extraction/jds/*.md`), easy → hardest, PARAPHRASED
  or public postings (OSS hygiene, §15); none reused as the step-4 few-shot.
- One expected sidecar each (`evals/extraction/expected/*.json`), user-reviewed.
- `test_extraction_evals.py` (pytest, marked `-m evals`, opt-in per R8):
  runs `runner.extract_batch` (real configured backend) over each JD; asserts
  (a) validator passes (evidence + containment), (b) closed-enum fields
  (`seniority`, `role_type`, `sponsorship.value`) match expected exactly, (c)
  `experience_clauses` count matches. Skills/domain compared loosely (set overlap
  ≥ threshold) since wording varies. Same test runs against claude-cli, Ollama,
  and vLLM to prove same-behaviour-across-backends.
- Verify ($0): `uv run pytest -m evals` with the role pointed at local Ollama;
  paste results. OPTIONAL (~cents): also run on claude-cli to confirm
  same-behaviour-across-backends.

---

## 12. Governance amendment (docs, no code)

- CLAUDE.md §5.1 R7: add EXTRACTION as the third sanctioned
  `src/` LLM carve-out — `src/extract/runner.py` makes single-purpose extraction
  calls via `src/llm/`, mechanically validated (pydantic + evidence containment +
  batch coverage) before caching; all other `src/extract/` and `src/llm/` modules
  stay LLM-free. Note the sidecar `jobs/extracted.parquet` and `profile/llm.yaml`
  in CLAUDE.md §5.5 file map. Mirror the schema/rule shapes into any fixture
  touched. Do NOT alter the existing tailoring/scoring carve-out wording.
- Verify: `uv run python -c "import src.llm, src.extract"` imports clean;
  `uv run pytest -q` green; `git grep -n "carve-out" CLAUDE.md` shows three.

---

## Phased checklist

### Phase 1 — Adapter foundation (`src/llm/`)
- [x] 1.1 `base.py` (§2.3) + `jsonparse.py` (move `_extract_json_array` out of
  judge; judge imports it; `pytest tests/` still green). Verify: `uv run pytest tests/ -x`. (Done)
- [x] 1.2 `config.py` + committed `profile/llm.example.yaml` (§2.1/2.2). ALSO add
  `profile/llm.yaml` to `.gitignore` (per-user config, like `verticals.yaml`), and
  copy the example to `profile/llm.yaml` locally so live steps have a config.
  Confirm `git status` does NOT list `profile/llm.yaml`. Verify:
  `uv run pytest tests/test_llm_config.py -x`. (Done)
- [x] 1.3 `ledger.py` (§2.4). Verify: `uv run pytest tests/test_ledger.py -x`. (Done)
- [x] 1.4 `claude_cli.py` (§2.5) + `fake.py` (§2.9). Verify: `uv run pytest tests/test_claude_cli.py -x`. (Done)
- [x] 1.5 `openai_compat.py` (§2.6) — R6 curl first. Verify: `uv run pytest tests/test_openai_compat.py -x`. (Done)
- [x] 1.6 `anthropic_api.py` (§2.7) + `factory.py` (§2.8). Verify: `uv run pytest tests/test_anthropic.py tests/test_factory.py -x`. (Done)

### Phase 2 — Extraction core (`src/extract/`)
- [x] 2.1 `schema.py` (§3). Verify: `uv run pytest tests/test_extract_schema.py -x`. Added tests and closed pydantic schema.
- [x] 2.2 `sponsorship.py` (§5). Verify: `uv run pytest tests/test_extract_sponsorship.py -x`. Added sponsorship pre-label logic.
- [x] 2.3 `validate.py` (§6). Verify: `uv run pytest tests/test_extract_validate.py -x`. Added deterministic validation.
- [x] 2.4 `prompt.py` (§4). Verify: `uv run pytest tests/test_extract_prompt.py -x`. Added prompt builders.
- [x] 2.5 `cache.py` (§7). Verify: `uv run pytest tests/test_extract_cache.py -x`. Added cache and upsert logic.
- [x] 2.6 `runner.py` inline mode (§8, inline only) + `extract_cli.py` +
  `__main__.py` + pyproject script + `pydantic` dep (§9). Code committed;
  `uv run pytest -q` green (464 passed). LIVE local-Ollama smoke DEFERRED by user
  (2026-07-21): local Ollama on this M3/16GB never returns within timeout for the
  full 4KB system-prompt + nested `Sidecar` grammar, even on a 264-char JD — a
  grammar-decode/prefill speed limit, NOT qwen3 reasoning (grammar suppresses the
  thinking trace). Revisit the smoke via claude-cli (~cents, the OPTIONAL path) or
  the Explorer vLLM path (3.3), not a non-reasoning model swap. `structured_output`
  must be `openai` (Ollama ignores top-level `format`); use the `qwen3-jd`
  16K-ctx derived model. See memory `project_extraction_2_6_local_llm`.

### Phase 3 — Offline file-batch + Explorer
- [x] 3.1 `runner.py` dump + ingest modes (§8). Done: dump writes
  `extract_meta.json` (system/guided_schema_single/model) + one-JD-per-line
  `prompts.jsonl`; ingest validates each result line, applies pre-label, upserts
  ok/failed with attempt increment, reports malformed/stale counts. `config.py`
  gained optional `hpc_model` (dump uses it over `model` when present). Backend
  construction moved into inline branch (dump/ingest make no LLM call). 8 new
  tests in `tests/test_extract_filemode.py`; full suite 472 passed.
- [~] 3.2 Local openai-compatible live run — DEFERRED by user (2026-07-21): user
  will run backlog via vLLM/HPC (3.3) and rest-days via claude -p; local LLM to be
  configured later when needed. Same M3 grammar-decode wall as 2.6. Not a blocker.
- [x] 3.3 `hpc/` files authored (§10.1–10.4): `README_explorer.md`,
  `extract.sbatch` (both `apptainer exec` lines carry `--bind /scratch`; exports
  `APPTAINERENV_HF_HOME`/`APPTAINERENV_HF_HUB_OFFLINE`; CUDA/`:latest` blocker
  documented), `extract_client.py` (requests-only, `--concurrency` default 32,
  per-line flush, no repo import), `run_explorer.sh` (dump→rsync→sbatch/poll→
  rsync→ingest, ingests any partial). All syntax-valid; suite 472 passed.
  **[NEEDS USER]** live Explorer cycle (dump → sbatch → ingest over ~20 rows,
  confirm `status=ok` + evidence validates) — not run; user drives HPC.

### Phase 4 — Evals + governance
- [ ] 4.1 `evals/extraction/` set + opt-in test (§11). Verify ($0):
  `uv run pytest -m evals` with `extract` at local Ollama; paste results.
  (OPTIONAL, ~cents: also run on claude-cli to confirm cross-backend parity.)
- [ ] 4.2 Governance amendment (§12). Verify: `uv run pytest -q` green;
  `git grep -n "carve-out" CLAUDE.md` shows three carve-outs.

---

## OUT OF SCOPE — do not build, do not propose

- Anything downstream of `extracted.parquet`: embeddings, hybrid matcher,
  deterministic scoring, review, shortlist, tailoring v2, fit_report,
  calibration, onboarding. Separate plans.
- The async Anthropic Batch API (submit/poll/retrieve, 50% off). Seam only (§2.7).
- The embed-first / extract-top-K escape hatch (§2 says DO NOT BUILD NOW).
- Any provider beyond claude-cli / openai-compatible / anthropic.
- Any edit to `src/score/`, `src/tailor/`, `src/discovery/`, v1 scoring, the
  seen-ledger, or the LaunchAgent/scheduling (except the step-1.1 helper move).
- On-node validation or on-node retry-with-feedback (Explorer client stays thin;
  authoritative validation is local `--mode ingest`).
- A skill/synonym canonicalization pass at extraction time (§2: embeddings
  absorb variance; skills stay verbatim).
- Adding the `openai` or `anthropic` SDKs (use `requests`, R10).
- Splitting a single oversized JD to fit a local context window (reduce
  `jds_per_call` via config instead; extraction never truncates a JD).
