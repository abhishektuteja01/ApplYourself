# Shared: docx → pdf via Word

Read by `/tailor` and `/cover-letter`.

Run `scripts/render_pdf.sh` with `OUT_DIR`, `FILE_SLUG` and `BASENAME` (`Resume`
or `Cover_Letter`) as arguments, in the SAME Bash call as the caller's variable
preamble — shell state does not persist between Bash calls:

```bash
./scripts/render_pdf.sh "${OUT_DIR}" "${FILE_SLUG}" "${BASENAME}"
```

Call the script; never inline its body. An inline `rm` on a variable path trips a
safety prompt that blocks unattended runs, even with permissions bypassed.

The script routes every conversion through the repo's fixed `.pdf_staging` dir
(Word's sandbox keeps one folder-access grant for that path), targets the document
by name rather than `active document`, and checks the PDF's text, not just that it
is non-empty. Do not change those.

If it prints a WARNING or `osascript` fails, surface it and **continue**: the docx
is the primary artifact and the PDF is supplementary. Never fail the command on
this.

Requires Microsoft Word on macOS. On any other platform, skip this step, say so,
and report the docx as the deliverable.
