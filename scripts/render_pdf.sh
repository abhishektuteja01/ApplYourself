#!/usr/bin/env bash
# docx -> pdf via Microsoft Word. Usage: render_pdf.sh <OUT_DIR> <FILE_SLUG> <BASENAME>
# Called by /tailor and /cover-letter; see .claude/shared/render_pdf.md.
set -u

OUT_DIR="${1:-}"
FILE_SLUG="${2:-}"
BASENAME="${3:-}"
test -n "${OUT_DIR}" && test -n "${FILE_SLUG}" && test -n "${BASENAME}" || {
    echo "ERROR: render_pdf needs OUT_DIR, FILE_SLUG and BASENAME set."; exit 1
}
# The repo root, not $(pwd): the whole point of a single staging dir is that the
# path never changes, and $(pwd) changes with the shell.
STAGING="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)/.pdf_staging"
mkdir -p "$STAGING"
# The staging name is per-job: Word resolves `document "..."` by name and reuses
# an already-open same-named doc, exporting the wrong role. Deliverable names in
# OUT_DIR are unaffected.
TOKEN="$(basename "${OUT_DIR}")"
cp "${OUT_DIR}/${FILE_SLUG}_${BASENAME}.docx" "${STAGING}/${TOKEN}_${BASENAME}.docx"
DOCX_ABS="${STAGING}/${TOKEN}_${BASENAME}.docx"
PDF_ABS="${STAGING}/${TOKEN}_${BASENAME}.pdf"
osascript <<ASEOF
tell application "Microsoft Word"
    open POSIX file "${DOCX_ABS}"
    set theDoc to document "${TOKEN}_${BASENAME}.docx"
    save as theDoc file format format PDF file name "${PDF_ABS}"
    close theDoc saving no
end tell
ASEOF
if [ -s "${PDF_ABS}" ]; then
    PDF_WORDS=$(pdftotext "${PDF_ABS}" - 2>/dev/null | wc -w | tr -d ' ')
    if [ "${PDF_WORDS:-0}" -lt 50 ]; then
        echo "WARNING: PDF has only ${PDF_WORDS:-0} extractable words — export may be blank/garbled. docx is primary, PDF supplementary."
    fi
    cp "${PDF_ABS}" "${OUT_DIR}/${FILE_SLUG}_${BASENAME}.pdf"
    rm -f "${DOCX_ABS}" "${PDF_ABS}"
    echo "pdf rendered: ${OUT_DIR}/${FILE_SLUG}_${BASENAME}.pdf"
else
    echo "WARNING: PDF conversion via Word failed — docx is primary, PDF supplementary."
fi
exit 0
