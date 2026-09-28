---
name: score-judge
description: Judge a few staged slices of job rows for /score. Spawned by /score and /ingest with slice file paths. Never invoked directly by the user.
tools: Read, Write
model: sonnet
effort: medium
---

# Judge slices

Your prompt is one or more slice file paths. The top of each slice names
its packet and the file to write.

1. Read the packet once, in full. It is your rubric, resume and skill weights.
2. For each slice: Read it, judge every role in it, and Write the answer
   file it names — a JSON array, one object per role:

```json
{"job_id": "a1b2c3d4", "title": 26, "skills": 22, "seniority": 18, "domain": 12,
 "years": 3, "country": "United States",
 "sponsorship": ["no_sponsorship"], "evidence": "we do not sponsor visas",
 "keywords_to_mirror": ["LLM evals", "RAG", "TypeScript"],
 "reasoning": "strong agentic fit; thin domain signal"}
```

   Add `"action": "skip"` or `"manual-review"` only when the packet says to
   lower a role's action; say why in `reasoning`.

Read and write only those files. Reply with only the answer files you wrote.
