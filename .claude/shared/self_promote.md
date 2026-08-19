# Self-promotion: `saved` -> `tailored`

Shared by `/apply` Step 2c and `/cover-letter` Step 7b — both fire this exact
transition, guarded the same way, so it lives here once instead of twice.

Re-read state fresh from disk first — a call earlier in the same session
(e.g. `/apply` invoking `/cover-letter`) may have already fired it:

```bash
cd "$(git rev-parse --show-toplevel)"
STATE=$(uv run python -c "
from pathlib import Path
from src.state_io import state_path_for, load_state
print((load_state(state_path_for(Path('pipeline'), '$JOB_ID')) or {}).get('state', ''))
")
```

**Only fires from `saved`** — `transition()` permits same-state and backwards
moves, so an unguarded call would drag an `applied`/`screen` role back to
`tailored` on a re-run:

```bash
cd "$(git rev-parse --show-toplevel)"
if [ "$STATE" = "saved" ]; then
    uv run track "$JOB_ID" tailored --note "$NOTE"
else
    echo "state is '${STATE}', not 'saved' -- leaving it alone (no transition)."
fi
```

Routed through `/track`, never written directly (R10).
