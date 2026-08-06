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
# Entry format (all five keys):
#   ## SKILL-<ID>        Referenced by name from each lane's tailoring.md
#                        "Skills layout". Renaming an ID orphans it there.
#   name:                the display string, exactly as it should print.
#   category:            coarse tag for your own grouping. NOT the display
#                        driver and not validated by any code — the lines,
#                        headers and ordering come from each vertical's
#                        profile/verticals/<v>/tailoring.md "Skills layout".
#                        A skill may appear under different headers in
#                        different lanes, or be omitted from one entirely.
#   evidence:            which bullets in profile/bullets.md back this, by ID.
#                        A skill with no bullet behind it does not belong here.
#   allowable_synonyms:  pre-approved display aliases for the same skill
#                        (e.g. "RAG" for "Retrieval-Augmented Generation").
#                        Empty list is fine and common.
#   vertical_lean:       which of YOUR configured verticals this leans toward.
#                        Values must be vertical names from
#                        profile/verticals.yaml. /tailor ranks a skill up when
#                        the lean includes the row's vertical AND the skill
#                        matches a `keywords_to_mirror` entry.
#
# Each vertical resume's own Skills block is BASELINE ONLY, for that file's
# standalone render. It is not read by /tailor for a real job_id and does not
# cap what is listed here.
#
# Add entries yourself as real skills surface. /suggest-synonyms proposes
# additions and aliases but never writes to this file.
#
# The entries below define exactly the SKILL IDs the committed
# profile/verticals/example_* lanes reference, so the shipped example
# configuration resolves end to end. `example_tertiary` appears in the leans
# because a third lane directory ships as a template; delete those tags if you
# do not configure a third vertical.

## SKILL-WIDGET-ASSEMBLY
name: Widget Assembly Operations
category: domain
evidence: B-WID-01 (daily throughput reconciliation across four plants)
allowable_synonyms: ["widget assembly", "assembly operations", "line operations"]
vertical_lean: [example_primary]

## SKILL-WIDGET-CONFIG
name: Widget Line Configuration
category: domain
evidence: B-WID-02 (parameter configuration across three plant rollouts)
allowable_synonyms: ["line configuration", "parameter configuration", "calibration configuration"]
vertical_lean: [example_primary]

## SKILL-GIZMO-LEDGER
name: Gizmo Ledger Reconciliation
category: domain
evidence: B-WID-03 (SQL rebuild of the monthly ledger reconciliation)
allowable_synonyms: ["ledger reconciliation", "month-end reconciliation"]
vertical_lean: [example_primary, example_secondary]

## SKILL-SPROCKET-VALIDATION
name: Sprocket Tolerance Validation
category: domain
evidence: B-SPR-01 (Python validation harness over 12,000 measurements)
allowable_synonyms: ["tolerance validation", "compliance validation", "measurement validation"]
vertical_lean: [example_secondary]

## SKILL-SPROCKET-RISK
name: Sprocket Failure Risk Modelling
category: ai_ml
evidence: B-SPR-02 (logistic baseline versus tree ensemble in R)
allowable_synonyms: ["failure risk modelling", "risk modelling", "reliability modelling"]
vertical_lean: [example_secondary]

## SKILL-COG-TRAINING
name: Cog Maintenance QA Assistant
category: ai_ml
evidence: B-COG-01 (LLM assistant with an 80-question graded eval set)
allowable_synonyms: ["question-answering assistant", "domain QA assistant"]
vertical_lean: [example_tertiary]

## SKILL-COG-PLATFORM
name: Cog Platform Engineering
category: domain
evidence: B-COG-02 (TypeScript service in Docker with Postgres-backed retrieval)
allowable_synonyms: ["platform engineering", "service deployment"]
vertical_lean: [example_tertiary]

## SKILL-PYTHON
name: Python (pandas, scikit-learn)
category: programming
evidence: B-SPR-01 (validation harness), B-COG-01 (assistant + eval harness), B-EDU-01 (capstone forecasting)
allowable_synonyms: ["Python", "pandas", "Python data analysis"]
vertical_lean: [example_primary, example_secondary, example_tertiary]

## SKILL-SQL
name: SQL
category: programming
evidence: B-WID-03 (ledger reconciliation queries), B-EDU-01 (database systems coursework, capstone)
allowable_synonyms: ["relational queries", "relational modelling"]
vertical_lean: [example_primary, example_secondary, example_tertiary]

## SKILL-TYPESCRIPT
name: TypeScript
category: programming
evidence: B-COG-02 (deployed TypeScript service)
allowable_synonyms: ["TypeScript", "Node service development"]
vertical_lean: [example_tertiary]

## SKILL-R
name: R
category: programming
evidence: B-SPR-02 (risk models built and compared in R)
allowable_synonyms: ["R statistical modelling"]
vertical_lean: [example_secondary]

## SKILL-LLM-APPS
name: LLM Application Development
category: ai_ml
evidence: B-COG-01 (hosted LLM API, prompt templates, graded eval set)
allowable_synonyms: ["LLM applications", "prompt engineering", "LLM API integration"]
vertical_lean: [example_primary, example_secondary, example_tertiary]

## SKILL-RAG
name: Retrieval-Augmented Generation
category: ai_ml
evidence: B-COG-02 (retrieval over a Postgres-backed document store)
allowable_synonyms: ["RAG", "retrieval augmentation", "document retrieval"]
vertical_lean: [example_primary, example_secondary, example_tertiary]

## SKILL-POSTGRES
name: PostgreSQL
category: tools
evidence: B-WID-03 (queries against a Postgres replica), B-COG-02 (document store)
allowable_synonyms: ["Postgres", "PostgreSQL"]
vertical_lean: [example_primary, example_secondary, example_tertiary]

## SKILL-DOCKER
name: Docker
category: tools
evidence: B-COG-02 (containerized deployment)
allowable_synonyms: ["containers", "containerized deployment"]
vertical_lean: [example_primary, example_tertiary]

## SKILL-GIT
name: Git
category: tools
evidence: B-COG-02 (versioned releases with a pre-release test gate), B-SPR-01 (harness under version control)
allowable_synonyms: ["version control", "Git"]
vertical_lean: [example_primary, example_secondary, example_tertiary]
