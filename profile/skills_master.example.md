# TEMPLATE — copy to profile/skills_master.md and replace with your own skills.
#
# The closed vocabulary for every Skills section /tailor renders. Read for any
# real job_id run. Mirrors bullets.md's discipline: every entry is a skill you
# will stand behind on a call, and every `allowable_synonyms` alias is a display
# alias for the SAME skill — never a way to introduce a new claim.
#
# /tailor SELECTS a subset and ORDERS it. It never invents an item that is not
# listed here.
#
# Entry format:
#   ## SKILL-<ID>        a stable id; renaming it only affects this file.
#   name:                the display string, exactly as it should print.
#   evidence:            which bullets in profile/bullets.md back this, by ID.
#                        A skill with no bullet behind it does not belong here.
#   allowable_synonyms:  pre-approved display aliases for the same skill
#                        (e.g. "RAG" for "Retrieval-Augmented Generation").
#                        Empty list is fine and common.
#   show:                {lane: line label} — the Skills line this skill sits
#                        on in each lane's resume. Labels and their order are
#                        the lane's `resume.skill_lines` in verticals.yaml. A
#                        lane not listed never shows the skill. Within a line,
#                        skills print in this file's order until /tailor
#                        reorders them for a JD.
#
# Deleting a skill is deleting its entry. `uv run verticals-check` names any
# pointer that no longer resolves.
#
# Add entries yourself as real skills surface. /suggest-synonyms proposes
# additions and aliases but never writes to this file.
#
# The entries below are fictional and match the example_* lanes.

## SKILL-WIDGET-ASSEMBLY
name: Widget Assembly Operations
evidence: B-WID-01 (daily throughput reconciliation across four plants)
allowable_synonyms: ["widget assembly", "assembly operations", "line operations"]
show: {example_primary: "Domain & Enterprise"}

## SKILL-WIDGET-CONFIG
name: Widget Line Configuration
evidence: B-WID-02 (parameter configuration across three plant rollouts)
allowable_synonyms: ["line configuration", "parameter configuration", "calibration configuration"]
show: {example_primary: "Domain & Enterprise"}

## SKILL-GIZMO-LEDGER
name: Gizmo Ledger Reconciliation
evidence: B-WID-03 (SQL rebuild of the monthly ledger reconciliation)
allowable_synonyms: ["ledger reconciliation", "month-end reconciliation"]
show: {example_primary: "Domain & Enterprise", example_secondary: "Domain & Enterprise"}

## SKILL-SPROCKET-VALIDATION
name: Sprocket Tolerance Validation
evidence: B-SPR-01 (Python validation harness over 12,000 measurements)
allowable_synonyms: ["tolerance validation", "compliance validation", "measurement validation"]
show: {example_secondary: "AI & Machine Learning"}

## SKILL-SPROCKET-RISK
name: Sprocket Failure Risk Modelling
evidence: B-SPR-02 (logistic baseline versus tree ensemble in R)
allowable_synonyms: ["failure risk modelling", "risk modelling", "reliability modelling"]
show: {example_secondary: "Domain & Enterprise"}

## SKILL-COG-TRAINING
name: Cog Maintenance QA Assistant
evidence: B-COG-01 (LLM assistant with an 80-question graded eval set)
allowable_synonyms: ["question-answering assistant", "domain QA assistant"]
show: {example_tertiary: "AI & Machine Learning"}

## SKILL-COG-PLATFORM
name: Cog Platform Engineering
evidence: B-COG-02 (TypeScript service in Docker with Postgres-backed retrieval)
allowable_synonyms: ["platform engineering", "service deployment"]
show: {example_tertiary: "Domain & Enterprise"}

## SKILL-PYTHON
name: Python (pandas, scikit-learn)
evidence: B-SPR-01 (validation harness), B-COG-01 (assistant + eval harness), B-EDU-01 (capstone forecasting)
allowable_synonyms: ["Python", "pandas", "Python data analysis"]
show: {example_primary: "Programming", example_secondary: "Programming", example_tertiary: "Programming"}

## SKILL-SQL
name: SQL
evidence: B-WID-03 (ledger reconciliation queries), B-EDU-01 (database systems coursework, capstone)
allowable_synonyms: ["relational queries", "relational modelling"]
show: {example_primary: "Programming", example_secondary: "Programming", example_tertiary: "Programming"}

## SKILL-TYPESCRIPT
name: TypeScript
evidence: B-COG-02 (deployed TypeScript service)
allowable_synonyms: ["TypeScript", "Node service development"]
show: {example_tertiary: "Programming"}

## SKILL-R
name: R
evidence: B-SPR-02 (risk models built and compared in R)
allowable_synonyms: ["R statistical modelling"]
show: {example_secondary: "Programming"}

## SKILL-LLM-APPS
name: LLM Application Development
evidence: B-COG-01 (hosted LLM API, prompt templates, graded eval set)
allowable_synonyms: ["LLM applications", "prompt engineering", "LLM API integration"]
show: {example_primary: "AI & Machine Learning", example_secondary: "AI & Machine Learning", example_tertiary: "AI & Machine Learning"}

## SKILL-RAG
name: Retrieval-Augmented Generation
evidence: B-COG-02 (retrieval over a Postgres-backed document store)
allowable_synonyms: ["RAG", "retrieval augmentation", "document retrieval"]
show: {example_primary: "AI & Machine Learning", example_secondary: "AI & Machine Learning", example_tertiary: "AI & Machine Learning"}

## SKILL-POSTGRES
name: PostgreSQL
evidence: B-WID-03 (queries against a Postgres replica), B-COG-02 (document store)
allowable_synonyms: ["Postgres", "PostgreSQL"]
show: {example_primary: "Databases & Tools", example_secondary: "Databases & Tools", example_tertiary: "Databases & Tools"}

## SKILL-DOCKER
name: Docker
evidence: B-COG-02 (containerized deployment)
allowable_synonyms: ["containers", "containerized deployment"]
show: {example_primary: "Databases & Tools", example_tertiary: "Databases & Tools"}

## SKILL-GIT
name: Git
evidence: B-COG-02 (versioned releases with a pre-release test gate), B-SPR-01 (harness under version control)
allowable_synonyms: ["version control", "Git"]
show: {example_primary: "Databases & Tools", example_secondary: "Databases & Tools", example_tertiary: "Databases & Tools"}
