# apply fixtures

Two kinds, both captured from live Greenhouse boards and scrubbed before being
committed.

**`greenhouse_*.json`** — question-API payloads (`?questions=true`), six of
them, covering the shapes `schema.py` has to survive: no compliance block, a
compliance block, a multi-select, and three flavours of
`demographic_questions` (required with a `decline_to_answer` flag, optional with
a label-only opt-out, and one with no opt-out at all).

**`form_*.html` + `form_*.json`** — DOM/API **pairs from the same posting**, five
of them. The HTML is what the applicant's browser renders; the JSON is that
posting's question API. They are paired because the two disagree, and
`reconcile.py` exists to settle the disagreement — testing it needs both halves
of one real form.

| pair | covers |
|---|---|
| `form_minimal` | identity, `country`, `candidate-location`, both file inputs, three custom react-selects, the EEOC block, aria-hidden decoys |
| `form_multiselect` | a `multi_value_multi_select` rendered as a checkbox fieldset; employer-authored diversity questions sitting in the ordinary questions block |
| `form_education` | the education block at its widest — months, numeric years, `add-another-button` |
| `form_demographic` | `demographic-section` with bare-numeric ids, multi react-selects, a *required* education block |
| `form_employment` | the employment block (`employment--container`), an optional `resume`, no `cover_letter` at all, and `employment_required` as a top-level key |

`form_employment` was captured after a 60-board probe found the employment block
rendering with no section of its own — every one of its seven fields, six of them
required, was falling into `questions` and being keyword-matched as if the
employer had authored it. It also disproved two assumptions the first four pairs
had made look universal: `resume` is optional on 9 of 25 boards sampled, and
`cover_letter` is not rendered at all on some.

## Scrubbing

The repo is public, so every fixture is company-scrubbed:

- company name and board slug replaced with a fictional mechanical-parts name,
  consistent with the `profile/*.example.*` widget world
- job id replaced with a synthetic one
- every absolute URL replaced with `https://example.com`, except the two generic
  government/Greenhouse references the EEOC boilerplate genuinely links to —
  careers-page and CDN logo URLs both carry the real account id
- `content` (the JD body) dropped: it is the bulk of the payload size and the
  main source of stray real-world proper nouns, and no module reads it

The HTML is reduced to the `<main>` subtree with `<style>`, `<svg>` and
`<script>` stripped. Nothing the scanner reads is removed — `class`, `role`,
`aria-*` and `id` all survive, and the fixture still contains the page header,
so `scan_form` has to locate `#application-form` itself rather than being handed
it.

Do not re-capture into these filenames without re-running the scrub. A raw
Greenhouse form names the employer in its EEOC text, its question labels, its
logo URL and its form action.
