# example_tertiary vertical rubric (TEMPLATE)

See `../example_primary/rubric.md` for the full template notes. This file
shows a third-lane shape: a lane whose titles collide with both other lanes,
so its classifier rules sit below theirs and its rubric leans on stack
evidence rather than title wording.

- `title_match` (0-30): in-lane build titles (28-30); adjacent platform or
  tooling titles (18-22); out-of-lane → 0.
- `jd_skill_overlap` (0-30): shared JD-quality gate, then this vertical's
  `skill_weights` bands (≥30 → 25-30; 15-29 → 15-24; 5-14 → 5-14; <5 → 0-4).
- `seniority_fit` (0-20): identical heuristic to the primary vertical.
- `domain_bonus` (0-20): top (16-20): the JD names the lane's own stack;
  mid (8-12): generic adjacent tooling; none → 0.
- **Example of an LLM-judged cap:** if the JD is a research role wearing a
  build-role title, cap title ≤10 / skills ≤4 / domain ≤6 and note it in
  `reasoning`.

## Additional self-check items (this vertical only)

- Did you keep research-flavored postings out of this lane's shortlist?
