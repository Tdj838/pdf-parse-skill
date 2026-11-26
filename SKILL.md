---
name: pdf-parse
description: "Read a scientific PDF into structure you can cite: per-page text in correct two-column reading order, section hierarchy, tables as cell matrices, and a page+bounding-box anchor for every block. Use when a task hands you a paper, preprint, scanned document or PDF report and asks you to answer questions, extract parameters, summarize, translate, verify claims, or just get the text out. Triggers: 'read this PDF', 'parse the paper', 'extract the tables', 'what does this paper say about X', 'quote the section', 'OCR this scan', '解析这篇文献', '读一下这个 PDF', '提取表格', '文献问答'. Handles dual-column papers, running headers/footers, hyphenated line breaks, and pages with no text layer (reported, not guessed)."
---

# PDF → evidence-anchored structure

A PDF is a bag of positioned glyphs, not a document. Naively concatenating text
operators produces text that is all *present* and still wrong: the left and right
columns interleave word by word, running headers land inside paragraphs, and
hyphenated line breaks split words. Everything downstream — answering questions,
extracting a parameter, translating a section — inherits that damage.

This skill reconstructs the page the way a reader sees it, and keeps an anchor
for every block so a claim can be checked against the source page.

## The four guarantees

| Guarantee | Mechanism in `scripts/pdfparse.py` |
|---|---|
| **Complete** — no dropped lines or paragraphs | Glyph-level extraction of every text-showing operator (`Tj`, `TJ`, `'`, `"`), including text inside form XObjects; nothing is sampled or truncated. |
| **Correct order** — columns never interleave | Recursive XY-cut over glyph boxes, with a gutter ranked higher than narrow seams, and running headers/footers stripped *before* the cut (a centred page number sits in the gutter and otherwise fabricates a column edge). |
| **Structured** — hierarchy, tables, formulas | Headings by font size/weight/numbering plus a front-matter rule; tables rejoined from row-aligned fragments into a cell matrix; equations kept as text with their printed label. |
| **Traceable** — every block points home | Each block carries `page` + `bbox`; undecodable glyphs become U+FFFD and are counted instead of substituted; pages with no text layer are listed as needing OCR. |

## When to use / when not

Use it whenever a PDF is the input and the answer must be defensible.

Do **not** use it to: rewrite a PDF, fill forms, extract vector graphics
semantics, or read handwriting (the glyph path has no OCR engine — see
*Scanned pages* below for what to do instead).

## Inputs and outputs

**Input**: one PDF path. Optionally `/input/case.json`, whose `output_contract`
keys are echoed back in `output_contract_fields`.

**Output** (JSON on stdout or `--out FILE`):

```json
{
  "case_id": "demo-case",
  "status": "success | partial | failed",
  "input":    {"path": "...", "pages": 13, "sha256": "...", "bytes": 209675},
  "document": {
    "title": "...",
    "sections":  [{"heading": "3.2 Data", "level": 2, "page": 3, "bbox": [x0,y0,x1,y1]}],
    "tables":    [{"page": 8, "n_rows": 8, "n_cols": 6, "rows": [["Model","MNLI"],["..."], "confidence": 0.98}],
    "figures":   [{"page": 5, "caption": "Figure 1: ...", "kind": "image", "bbox": [...]}],
    "equations": [{"label": "3", "page": 4, "text": "...", "bbox": [...]}],
    "references":[{"page": 12, "raw": "[1] A. Author. ...", "bbox": [...]}],
    "pages": [{
      "page": 1, "width": 612.0, "height": 792.0,
      "has_text_layer": true,
      "blocks": [{"type": "paragraph", "text": "...", "bbox": [56.0, 456.0, 296.0, 540.0],
                  "font_size": 10.0, "line_count": 7}],
      "removed_headers_footers": [{"text": "Preprint", "kind": "header_or_footer", "bbox": [...]}],
      "images": [{"width": 1275, "height": 1650, "bbox": [...]}]
    }],
    "ocr_required_pages": [1]
  },
  "checks": {"unknown_glyph_count": 0, "unknown_glyph_ratio": 0.0,
             "pages_without_text_layer": [], "header_footer_removed": 6,
             "rotated_text_runs": 14, "body_font_size": 10.9, "repairs": []},
  "uncertainty": ["...plain-language statement of what is unknown..."]
}
```

Block `type` is one of `title | authors | heading | paragraph | caption |
equation | table | header | footer`. `bbox` is `[x0, y0, x1, y1]` in PDF points
with the origin at the page's bottom-left — that is the coordinate system the
file itself uses, so a reviewer can check it against any PDF viewer.

## Call it

```bash
# from the directory holding this SKILL.md
python3 scripts/pdfparse.py /input/paper.pdf --out /app/output/paper.json

# case mode: locate the PDF from the case file and echo its output_contract back
python3 scripts/pdfparse.py --case-json /input/case.json --out /app/output/result.json

# first pages only, when the task is scoped to the abstract
python3 scripts/pdfparse.py /input/paper.pdf --max-pages 3 --stdout

# what the extractor itself saw, on stderr
PDFPARSE_DEBUG=1 python3 scripts/pdfparse.py /input/paper.pdf --stdout > /dev/null
```

In case mode the PDF path is taken from the case file itself (the key is not
assumed — `pdf`, `pdf_path`, `input_path`, `path`, `file`, `document` and any
nested `.pdf` value are searched), so a case that names its input differently
still works. The result is written with `case_id` and, when the case declares an
`output_contract`, an `output_contract_fields` block echoing every declared key
(filled where this extractor can answer it, `null` where it cannot — never
invented).

Exit code `0` for `success` and `partial`, `2` for `failed`. Never key a decision
on the exit code alone — read `status`.

Requirements: Python 3.9+, standard library only. No `pdftotext`, no `pdfplumber`,
no OCR binary, no network. That is deliberate: a skill that only works where
poppler happens to be installed fails on the machines that actually need it.

## Answering a question with evidence

The whole point of the anchors is that an answer can be audited. For each
question, emit exactly this shape:

```json
{
  "question": "Which corpora were used for pretraining?",
  "answer": "Five English corpora: BOOKCORPUS, English WIKIPEDIA, CC-NEWS, OPENWEBTEXT, STORIES.",
  "evidence": [{"page": 3, "bbox": [56.0, 640.0, 296.0, 700.0],
                "quote": "We consider five English-language corpora of ..."}],
  "status": "answered | not_reported | conflicting",
  "uncertainty": null
}
```

Rules that make the difference between a usable answer and a confident guess:

1. **Quote verbatim.** The `quote` must be a substring of that block's `text`.
   If you cannot find such a substring, you are paraphrasing from memory — stop.
2. **One anchor per claim.** A claim that spans two columns needs both blocks.
3. **`not_reported` is a valid answer.** If the searched blocks do not contain
   it, say so and name what you did search (e.g. "searched all 13 pages for
   'batch size'; only the learning rate is reported").
4. **`conflicting` when the paper disagrees with itself** — quote both sides and
   both pages. Do not average them.
5. **Never fill a gap from domain priors.** "Section 4 must have used Adam"
   is fabrication unless the text says so.

## Scanned pages: report, then OCR separately

Pages listed in `document.ocr_required_pages` have no text layer. The extractor
returns **no text** for them and sets `status: partial`; that is the honest
result, not a failure to hide.

If an OCR engine is available in the environment, run it on that page and label
the result as OCR-derived — keep the distinction visible:

```bash
# only when an engine exists; the text is then OCR output, not the PDF's own layer
pdftoppm -r 300 -f 3 -l 3 -png /input/paper.pdf /tmp/p3 && tesseract /tmp/p3-03.png stdout
```

Record `"source": "ocr"` and the engine name next to anything produced that way.
Do not merge OCR text into the `blocks` array as if the PDF had said it.

## Failure handling

| Situation | What happens | What you do |
|---|---|---|
| Encrypted PDF (no password) | `PDFDocument` finds no catalog → `status: failed` | Report that the file needs a password; do not attempt a blank-password guess loop. |
| Page has only images | `has_text_layer: false`, page in `ocr_required_pages` | See *Scanned pages*. |
| Font subset without `ToUnicode` | Glyphs become U+FFFD; `unknown_glyph_ratio` rises | Report the ratio. Above 2% the status is already `partial`; quote only the readable spans. |
| Broken xref / linearised file | Objects are recovered by full-file scan; `checks.repairs` records it | Mention the repair if the task is about provenance. |
| Rotated text (side stamps, vertical labels) | Counted in `checks.rotated_text_runs`, excluded from reading order | Not part of the body; add it only if the question is specifically about it. |
| Table heuristic declines | Block stays `paragraph`, no table reported | Say no table was structured rather than inventing a matrix. |

## Limits

* Text-only PDFs. No handwriting, no OCR engine bundled (see above).
* Tables are reconstructed from aligned whitespace; ruled tables with merged
  cells are reported only when the cell seams are visible as gaps. When a
  structured table is required and `confidence < 0.9`, treat the matrix as
  provisional and quote the caption.
* Equations are kept as linear text with their label; superscript/subscript
  positions are not turned into MathML.
* Reading order is geometric. A page whose columns are not separated by a
  whitespace gutter (text on a full-bleed background) is ordered top-to-bottom
  and will say so through a low block count, not by guessing.
* Figures are inventoried by caption and image XObject; the plot's *data* is
  not recovered.

## Minimal example (illustrative, not a submitted test result)

Input: `demo/cases/twocolumn.pdf`, a generated two-column page with a known
answer. Command:

```bash
python3 scripts/pdfparse.py demo/cases/twocolumn.pdf --stdout | python3 -m json.tool | head -40
```

Output, excerpted:

```json
{"case_id": null, "status": "success",
 "document": {"title": "Column-Aware Text Extraction with Traceable Evidence",
   "pages": [{"page": 1, "has_text_layer": true,
     "blocks": [{"type": "title", "text": "Column-Aware Text Extraction with Traceable Evidence", "bbox": [25.2, 726.0, 586.8, 726.0]},
                {"type": "paragraph", "text": "Text extraction from research papers is usually evaluated on rendered pages, not on the structure a reader needs.", "bbox": [56.0, 456.0, 296.0, 540.0]}]}]}}
```

The left column's paragraph ends at `x1 = 296.0` and the right column starts at
`x = 316.0`: that 20 pt gutter is what the cut follows, which is why the sentence
is not spliced into its right-hand neighbour.
