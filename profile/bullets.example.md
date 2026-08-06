# TEMPLATE — copy to profile/bullets.md and replace every entry with your own.
#
# The source of truth for every generated document. Read by /tailor, /score's
# judges, /cover-letter and /outreach. Nothing downstream may claim anything
# that is not here.
#
# THE RULE: a bullet's `canonical` text is what you will stand behind on a call.
# /tailor may reword it ONLY using words already in the canonical text or an
# entry in that bullet's `allowable_synonyms`. No free substitution from JD
# vocabulary, ever. If a JD wants a claim you cannot attest, the answer is that
# the resume does not make it.
#
# Entry format (all six keys, one blank line between entries):
#   ## B-<CTX>-NN        <CTX> = a short context tag you invent per employer or
#                        project; NN = zero-padded sequence within that context.
#                        IDs are referenced by skills_master.md's `evidence`, so
#                        do not renumber an existing bullet.
#   source:              where the work happened, with dates. Also what
#                        /outreach mines for a shared affiliation.
#   canonical:           ONE sentence, attestable, specific. Scope + what you
#                        did + a measurable outcome where you have one.
#   tags:                snake_case list. Your own vocabulary for grouping.
#   evidence:            why you can defend this — system, cadence, tenure,
#                        scale. Written for you, not for a recruiter.
#   allowable_synonyms:  pre-approved re-packagings of the SAME claim. Start
#                        short and grow it with /suggest-synonyms against real
#                        JD keywords. A synonym that widens the claim is a
#                        fabrication with extra steps.
#
# Tier-2 diction linting treats verbatim canonical text as exempt only when
# `bullets_diction_pass_completed: true` in profile/de_ai_rules.yaml. Set that
# once you have read your own bullets for diction, not before.
#
# The entries below are fictional, and match the widget/sprocket/cog world of
# the committed profile/verticals/example_* lanes.

## B-WID-01
source: Widget Corp, Widget Operations Analyst (Exampletown, Jan 2023 - Jun 2025)
canonical: Owned the daily widget assembly throughput report for a national manufacturing client, reconciling line output against the production plan across four plants and flagging variances to the operations lead each morning.
tags: [widget_assembly, daily_ops, reconciliation, reporting]
evidence: production system, daily cadence, 2.5 year tenure, four-plant scope
allowable_synonyms: ["throughput reporting", "daily production reporting", "output reconciliation", "plan-versus-actual reconciliation", "variance reporting", "multi-plant reporting", "line output analysis"]

## B-WID-02
source: Widget Corp, Widget Operations Analyst (Exampletown, Jan 2023 - Jun 2025)
canonical: Configured widget line parameters for three plant rollouts, translating operator requirements into calibration settings and validating each change in a staging line before release.
tags: [widget_config, rollout, requirements, validation]
evidence: three rollouts, staging-then-release discipline, direct operator handoff
allowable_synonyms: ["parameter configuration", "line configuration", "calibration settings", "requirements translation", "staged validation", "pre-release validation", "rollout support"]

## B-WID-03
source: Widget Corp, Widget Operations Analyst (Exampletown, Jan 2023 - Jun 2025)
canonical: Rebuilt the gizmo ledger reconciliation in SQL against a Postgres replica, replacing a manual spreadsheet process and cutting the monthly close from three days to one.
tags: [gizmo_ledger, sql, postgres, automation, month_end]
evidence: replaced a named manual process, measured close-time reduction, owned the queries
allowable_synonyms: ["ledger reconciliation", "month-end close", "close process automation", "SQL reporting", "query development", "replacing manual reconciliation", "spreadsheet replacement"]

## B-SPR-01
source: Sprocket compliance side project (Jul 2025 - Oct 2025)
canonical: Built a Python validation harness for sprocket tolerance data that scored 12,000 historical measurements against published compliance bands and produced a per-batch pass/fail report.
tags: [sprocket_validation, python, validation, compliance]
evidence: own code, real published bands, 12k-record dataset, reproducible output
allowable_synonyms: ["validation harness", "tolerance validation", "compliance validation", "batch scoring", "measurement validation", "automated pass/fail reporting"]

## B-SPR-02
source: Sprocket compliance side project (Jul 2025 - Oct 2025)
canonical: Modelled sprocket failure risk in R against the same measurement set, comparing a logistic baseline to a tree ensemble and documenting why the simpler model shipped.
tags: [sprocket_risk, r, modelling, model_selection]
evidence: both models built and compared, written rationale, single dataset
allowable_synonyms: ["risk modelling", "failure prediction", "logistic regression baseline", "tree ensemble comparison", "model selection", "model comparison writeup"]

## B-COG-01
source: Cog training assistant (personal project, Nov 2025 - Feb 2026)
canonical: Built a cog-maintenance question-answering assistant on a hosted LLM API, wiring prompt templates and an evaluation set of 80 graded questions to catch regressions between prompt revisions.
tags: [cog_training, llm_apps, prompting, evaluation]
evidence: working project, hand-graded eval set, versioned prompt revisions
allowable_synonyms: ["LLM application", "question-answering assistant", "prompt engineering", "prompt templates", "evaluation set", "regression evaluation", "LLM API integration"]

## B-COG-02
source: Cog training assistant (personal project, Nov 2025 - Feb 2026)
canonical: Deployed the assistant as a TypeScript service in Docker with retrieval over a Postgres-backed document store, versioned in Git with the eval suite running before each release.
tags: [cog_platform, typescript, docker, rag, postgres, git]
evidence: deployed and running, containerized, retrieval over real documents
allowable_synonyms: ["retrieval-augmented generation", "RAG", "document retrieval", "containerized deployment", "Docker deployment", "TypeScript service", "vector search", "pre-release test gate"]

## B-EDU-01
source: BS Industrial Engineering, Example University (Sep 2019 - Dec 2022)
canonical: Completed coursework in statistics, database systems and programming, including a capstone that used Python and SQL to forecast plant demand from three years of order history.
tags: [coursework, python, sql, statistics, forecasting]
evidence: graded capstone, real order-history dataset, named coursework
allowable_synonyms: ["demand forecasting", "time-series forecasting", "database systems coursework", "statistical coursework", "capstone project"]
