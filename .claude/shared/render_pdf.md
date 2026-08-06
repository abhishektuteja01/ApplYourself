# Shared: docx → pdf via Word

Read by `/tailor` and `/cover-letter`. Both had a byte-identical copy of this
block differing only in the filename, so a fix to the sandbox workaround had to
be made twice.

**Caller sets two variables first:** `OUT_DIR`, `FILE_SLUG`, and `BASENAME`
(`Resume` or `Cover_Letter`). Then run this verbatim.

Word's sandbox only reliably keeps a folder-access grant for one unchanging
path, so every conversion is routed through the same fixed staging dir — the
one-time grant never needs re-approval even though each job gets a new
`${OUT_DIR}`.

```bash
STAGING="$(pwd)/.pdf_staging"
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
