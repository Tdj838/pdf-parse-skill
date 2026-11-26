# pdf-parse — reading a paper as a structure you can cite

Submission for the product-arena challenge **阅读01｜文献文档解析**
(`product-arena-08-3027d350`).

A PDF is a bag of positioned glyphs. Concatenating its text operators gives you
text where every word is present and the result is still wrong: the two columns
interleave word by word, the running header lands mid-paragraph, and hyphenated
line breaks split words in half. Every downstream task — question answering,
parameter extraction, translation — inherits that damage silently.

This skill rebuilds the page geometrically, then keeps an anchor for every block
so any statement can be checked against the source page.

## What is here

| Path | What it is |
|---|---|
| `SKILL.md` | The skill itself: triggers, contracts, workflow, evidence rules, limits. |
| `scripts/pdfparse.py` | The extractor. Python 3.9+, **standard library only**, ~1.9k lines. |
| `scripts/pdffixtures.py` | Generates the two synthetic cases *and* their ground truth. |
| `scripts/selfcheck.py` | Scores a parse against independent truth; nothing is self-reported. |
| `src/reproduce.py` | Bundle entrypoint: parses, scores, writes `execution/` and `trace/`. |
| `src/build_bundle.py` | Derives `characterization.json` and `arm_manifest.json` from the run. |
| `execution/results/*.json` | Real parse results and the measured checks. |
| `characterization.json` | Every claim turned into a target / metric / score. |
| `trace/trace.jsonl` | ARM v1.1 typed trace emitted by the run itself. |
| `demo/cases/` | Generated inputs + exact ground truth. |
| `demo/papers/` | Two public arXiv preprints used as real-world inputs. |

## Run it

```bash
python3 src/reproduce.py --out execution      # parse + score + log + trace
python3 src/build_bundle.py                   # refresh manifest and characterization
```

Both run in about two seconds and need no network. To use the skill on a paper of
your own:

```bash
python3 scripts/pdfparse.py /path/to/paper.pdf --out paper.json
```

## What the run actually measured

Four inputs, four checks, all passing (`execution/results/selfcheck.json`):

| Case | What it proves | Result |
|---|---|---|
| `twocolumn` (generated, exact truth) | fidelity, order, tables, anchors | character F1 **1.0000**, 11/11 paragraphs in order, table **4×4 exact**, anchor coverage **100 %**, 0 header leaks |
| `scanned` (image-only) | honesty | `status: partial`, page 1 flagged for OCR, **0** fabricated blocks |
| `roberta` (arXiv 1907.11692) | real two-column/ACL-style paper | abstract trigram recall **0.9837**, title recovered, 27 sections, 0 unmapped glyphs |
| `attention` (arXiv 1706.03762) | different producer, 15 pages | abstract trigram recall **0.9695**, title recovered, 0 unmapped glyphs, 1.2 s |

The generated case is the load-bearing one: because the text is written by
`scripts/pdffixtures.py`, the correct answer is known character by character, so
F1 = 1.0 is a measurement rather than an impression. The two real preprints are
scored against metadata published by arXiv, which the extractor never sees.

## How it works, in four decisions

1. **Cut glyph boxes, not lines.** Line assembly needs a column decision first;
   a per-line decision oscillates on justified text. The recursive XY-cut ranks a
   page-wide gutter above narrow seams, so a wide table column gap does not win
   over the real margin between two text columns.
2. **Strip furniture before cutting.** A centred page number sits exactly in the
   column gutter. Removing repeating headers/footers first is what makes the
   gutter detectable at all — this single ordering change took the generated case
   from F1 0.90 to 1.00.
3. **Rejoin tables by row signature.** The cut reads a table column-major; blocks
   sharing the same row baselines are rebuilt row by row and re-inserted after
   the last block above them, which is where a reader expects the table.
4. **Never fill a gap.** Undecodable glyphs become U+FFFD and are counted; pages
   without a text layer produce no text and are listed in `ocr_required_pages`.
   A confidence-free guess is worse than a labelled absence.

## Limits, stated plainly

* No OCR engine is bundled. Scanned pages are reported, then must be OCR'd by a
  tool that exists in the environment — and labelled as OCR-derived.
* Tables come from aligned whitespace. Ruled tables with merged cells are
  recovered only when the seams are visible as gaps; otherwise the block stays
  text and no matrix is claimed.
* Equations are linear text with their printed label, not MathML.
* Reading order is geometric: full-bleed layouts with no whitespace gutter fall
  back to top-to-bottom.
* Handwriting is out of scope.

## Provenance and licensing

The two real inputs are public arXiv preprints, included so the numbers above can
be re-derived offline:

* `demo/papers/roberta.pdf` — arXiv:1907.11692v1, 209 675 bytes
* `demo/papers/attention.pdf` — arXiv:1706.03762v7, 2 215 244 bytes

Their SHA-256 hashes are recorded in `arm_manifest.json`
(`provenance.input_hashes`). They remain the property of their authors and are
used here only as test inputs.

## Public repository

The reviewable copy lives at **https://github.com/Tdj838/pdf-parse-skill**, and
`GITHUB_URL.txt` in the submission root points at it. The repository is public,
needs no login to open, and holds this same tree — the skill, the engine, the
generated inputs, the real parse results and the trace.

Everything still runs offline from a clone:

```bash
git clone https://github.com/Tdj838/pdf-parse-skill
cd pdf-parse-skill && python3 src/reproduce.py --out execution
```
