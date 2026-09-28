# example_secondary vertical rubric (TEMPLATE)

See `../example_primary/rubric.md` for the full template notes. This file
shows the secondary-vertical shape: scored on its own merits, ranked only
within its own shortlist section, never capped relative to the primary.

- `title` (0-30): two tiers — compliance/governance lean (28-30);
  generic risk tail (18-22, one band below by design); out-of-lane → 0.
- `skills` (0-30): this vertical's
  `skill_weights` bands (≥30 → 25-30; 15-29 → 15-24; 5-14 → 5-14; <5 → 0-4).
- `seniority` (0-20): identical heuristic to the primary vertical.
- `domain` (0-20): top (16-20): sprocket-audit signals; mid (8-12):
  generic compliance; none → 0.
- **Example of an LLM-judged cap:** if the JD names a closed credential list
  with no path this profile holds, cap title ≤10 / skills ≤4 / domain ≤6 and
  note it in `reasoning` — the kind of semantic call that stays with the
  judge rather than a `src/` drop rule.
