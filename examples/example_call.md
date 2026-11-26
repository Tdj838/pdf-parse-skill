# Minimal call example

Runnable as written from the directory containing `SKILL.md`. The input is the
generated two-column page in `demo/cases/twocolumn.pdf`; the JSON below is a
**real captured run, abbreviated** (one page, three blocks) so the interface is
visible. It is illustrative output, not a result on the platform's fixed cases.

## 1. Call

```bash
python3 scripts/pdfparse.py demo/cases/twocolumn.pdf --out /tmp/twocolumn.json
```

With a case file, whose `output_contract` keys are echoed back:

```bash
python3 scripts/pdfparse.py /input/paper.pdf --case-json /input/case.json \
        --out /app/output/result.json
```

## 2. Output (captured, abbreviated)

```json
{
  "status": "success",
  "input": {"filename": "twocolumn.pdf", "pages": 3, "sha256": "6dc2c9fb4e104b7f..."},
  "document": {
    "title": "Column-Aware Text Extraction with Traceable Evidence",
    "sections": [
      {"heading": "Abstract", "level": 3, "page": 1, "bbox": [56.0, 662.0, 108.8, 662.0]},
      {"heading": "1 Introduction", "level": 1, "page": 1, "bbox": [56.0, 560.0, 155.0, 560.0]},
      {"heading": "2 Method", "level": 1, "page": 1, "bbox": [316.0, 560.0, 375.4, 560.0]}
    ],
    "tables": [{
      "page": 2, "n_rows": 4, "n_cols": 4, "confidence": 1.0,
      "rows": [["Model", "Precision", "Recall", "F1"],
               ["baseline", "0.812", "0.774", "0.793"],
               ["column-cut", "0.965", "0.958", "0.961"],
               ["column-cut+anchors", "0.971", "0.960", "0.965"]]
    }],
    "pages": [{
      "page": 1,
      "has_text_layer": true,
      "blocks": [
        {"type": "title", "text": "Column-Aware Text Extraction with Traceable Evidence",
         "bbox": [25.2, 726.0, 586.8, 726.0], "font_size": 18.0, "line_count": 1},
        {"type": "authors", "text": "A. Researcher, B. Reviewer",
         "bbox": [212.4, 704.0, 399.6, 704.0], "font_size": 12.0, "line_count": 1},
        {"type": "authors", "text": "Institute of Document Analysis",
         "bbox": [207.0, 690.0, 405.0, 690.0], "font_size": 11.0, "line_count": 1}
      ],
      "removed_headers_footers": [
        {"text": "Preprint - do not distribute", "kind": "header_or_footer",
         "bbox": [56.0, 762.0, 190.4, 762.0]},
        {"text": "1", "kind": "header_or_footer", "bbox": [303.3, 40.0, 308.7, 40.0]}
      ]
    }]
  },
  "checks": {
    "unknown_glyph_count": 0,
    "unknown_glyph_ratio": 0.0,
    "pages_total": 3,
    "pages_without_text_layer": [],
    "header_footer_removed": 6,
    "rotated_text_runs": 0,
    "repairs": [],
    "body_font_size": 10.0
  },
  "uncertainty": ["No figure captions were matched; figure inventory may be incomplete."]
}
```

## 3. How to read it

* Every block carries `page` and `bbox` — `[x0, y0, x1, y1]` in PDF points with the
  origin at the bottom-left, so it can be checked against any viewer.
* The header and page number are **absent from `blocks`** and listed under
  `removed_headers_footers`. That is why they cannot leak into a quotation: the
  page number sits at x≈303 in the middle of the 296→316 column gutter, and
  leaving it in place would have hidden the gutter itself.
* `checks` reports what the decoder could not map; `uncertainty` translates that
  into a sentence a reviewer can act on. Here it says no figure was found — which
  is true of this generated page, not a silent zero.

## 4. Answering from the output

```json
{
  "question": "What does the abstract say the reader reconstructs?",
  "answer": "Two-column reading order from glyph geometry, keeping a page and bounding box for every character, and reporting unreadable regions instead of guessing them.",
  "evidence": [{"page": 1, "bbox": [44.0, 624.0, 492.2, 646.0],
                "quote": "We present a layout reader that reconstructs two-column reading order from glyph geometry, keeps a page and bounding box for every character, and reports unreadable regions instead of guessing them."}],
  "status": "answered",
  "uncertainty": null
}
```

The `quote` must be a substring of the cited block's `text`. If it is not, the
answer is a paraphrase and must be labelled as one, or dropped.
