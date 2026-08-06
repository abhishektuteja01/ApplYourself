# Shared: docx → pdf via Word

Read by `/tailor` and `/cover-letter`.

**`OUT_DIR`, `FILE_SLUG` and `BASENAME`** (`Resume` or `Cover_Letter`) must be set
in the SAME Bash call as the block below — shell state does not persist between
Bash calls. `/tailor` prepends its Step 1 preamble
(`. /tmp/tailor_<job_id>_env.sh`) and sets `BASENAME`; `/cover-letter` substitutes
all three as literals. All three must be non-empty; the block checks, because an
unset one silently produces `_.docx` and then reports the failed copy as if Word
were at fault.

Every conversion routes through the repo's fixed `.pdf_staging` dir: Word's
sandbox keeps a folder-access grant for one unchanging path, so a single grant
covers every job even though each gets a new `OUT_DIR`. Do not parameterize it.

```bash
# Prepend the caller's variable preamble to this block.
test -n "${OUT_DIR}" && test -n "${FILE_SLUG}" && test -n "${BASENAME}" || {
    echo "ERROR: render_pdf needs OUT_DIR, FILE_SLUG and BASENAME set."; exit 1
}
# The repo root, not $(pwd): the whole point of a single staging dir is that the
# path never changes, and $(pwd) changes with the shell.
STAGING="$(git rev-parse --show-toplevel)/.pdf_staging"
mkdir -p "$STAGING"
cp "${OUT_DIR}/${FILE_SLUG}_${BASENAME}.docx" "${STAGING}/${FILE_SLUG}_${BASENAME}.docx"
DOCX_ABS="${STAGING}/${FILE_SLUG}_${BASENAME}.docx"
PDF_ABS="${STAGING}/${FILE_SLUG}_${BASENAME}.pdf"
osascript <<ASEOF
tell application "Microsoft Word"
    open POSIX file "${DOCX_ABS}"
    set theDoc to active document
    save as theDoc file format format PDF file name "${PDF_ABS}"
    close active document saving no
end tell
ASEOF
if [ -s "${PDF_ABS}" ]; then
    cp "${PDF_ABS}" "${OUT_DIR}/${FILE_SLUG}_${BASENAME}.pdf"
    rm -f "${DOCX_ABS}" "${PDF_ABS}"
    echo "pdf rendered: ${OUT_DIR}/${FILE_SLUG}_${BASENAME}.pdf"
else
    echo "WARNING: PDF conversion via Word failed — docx is primary, PDF supplementary."
fi
```

If `osascript` fails, surface the error and **continue**: the docx is the
primary artifact and the PDF is supplementary. Never fail the command on this.

Requires Microsoft Word on macOS. On any other platform, skip this step, say so,
and report the docx as the deliverable.
