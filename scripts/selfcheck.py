#!/usr/bin/env python3
"""selfcheck.py -- score a pdfparse result against known-correct evidence.

Three checks, each answering one of the four questions the topic asks for:

  fidelity   character-level agreement with text whose exact content is known
             (the generated two-column page), and abstract-token coverage for a
             real arXiv PDF against the publisher's own metadata
  order      paragraphs must appear in the extracted stream in reading order
  honesty    pages without a text layer must be reported, not hallucinated

Nothing here trusts the extractor's own self-report: every number is recomputed
from the emitted JSON and an independent source of truth.
"""

from __future__ import annotations

import json
import os
import re
import sys
from difflib import SequenceMatcher


def norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def compact(text: str) -> str:
    """Drop whitespace so that line-break decisions cannot mask character errors."""
    return re.sub(r"\s+", "", text)


def char_f1(hypothesis: str, reference: str) -> dict:
    h, r = compact(hypothesis), compact(reference)
    if not h or not r:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "aligned_ratio": 0.0}
    sm = SequenceMatcher(None, r, h, autojunk=False)
    matched = sum(block.size for block in sm.get_matching_blocks())
    precision = matched / len(h)
    recall = matched / len(r)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "aligned_ratio": round(sm.ratio(), 4),
        "chars_hypothesis": len(h),
        "chars_reference": len(r),
    }


def body_text(result: dict) -> str:
    """Only running text: tables and captions are scored separately."""
    out = []
    for page in result["document"]["pages"]:
        for block in page["blocks"]:
            if block["type"] in ("paragraph", "heading", "title", "authors"):
                out.append(block["text"])
    return " ".join(out)


def check_twocolumn(result: dict, truth: dict) -> dict:
    # Running text of pages 1-2 only: page 3 is the reference list, which is
    # scored by section detection rather than by character agreement.
    pages = result["document"]["pages"][:2]
    hyp_blocks = [b for p in pages for b in p["blocks"] if b["type"] == "paragraph"]
    extracted = " ".join(b["text"] for b in hyp_blocks)

    parts = [part for page in truth["pages"] for part in page["parts"]]
    abstract = next(p["text"] for p in parts if p["kind"] == "abstract")
    columns = [p for p in parts if p["kind"] == "column"]
    conclusion = [p["text"] for p in parts if p["kind"] == "conclusion"]
    ordered_paragraphs = [para for col in columns for para in col["paragraphs"]]
    reference = " ".join([abstract] + ordered_paragraphs + conclusion)

    fidelity = char_f1(extracted, reference)

    # reading order: the ground-truth paragraphs must appear in sequence
    stream = norm_ws(extracted)
    positions, missing = [], 0
    cursor = 0
    for para in [abstract] + ordered_paragraphs + conclusion:
        probe = norm_ws(para)
        found = -1
        for length in (60, 45, 30):
            found = stream.find(probe[:length], cursor)
            if found >= 0:
                break
        if found < 0:
            missing += 1
            continue
        positions.append(found)
        cursor = found + 1
    ordered = positions == sorted(positions)

    # a running header must not leak; a bare page number counts as a footer leak
    leaked = [h for h in truth["headers"] if len(h) >= 6 and h in stream and h not in reference]
    if re.search(r"(?m)(?:^|\s)\d{1,3}(?:\s|$)", stream) and not re.search(r"\d", reference):
        leaked.append("<bare page number>")

    table_ok, row_acc = None, None
    got = result["document"]["tables"]
    want = truth["tables"]
    if want:
        if not got:
            table_ok, row_acc = False, 0.0
        else:
            expected_rows = want[0]["rows"]
            rows = got[0]["rows"]
            hits = sum(1 for a, b in zip(rows, expected_rows) if a == b)
            row_acc = round(hits / len(expected_rows), 4)
            table_ok = (got[0]["n_cols"] == len(expected_rows[0])
                        and len(rows) == len(expected_rows) and hits == len(expected_rows))

    blocks = [b for p in result["document"]["pages"] for b in p["blocks"]]
    anchored = sum(1 for b in blocks
                   if isinstance(b.get("bbox"), list) and len(b["bbox"]) == 4
                   and b["bbox"][2] >= b["bbox"][0])

    return {
        "case": "twocolumn-synthetic",
        "fidelity": fidelity,
        "reading_order": {
            "paragraphs_found": len(positions),
            "paragraphs_missing": missing,
            "monotonic": ordered,
        },
        "header_footer_leaks": leaked,
        "table": {"exact": table_ok, "row_accuracy": row_acc,
                  "found": len(got), "expected": len(want)},
        "anchors": {"blocks": len(blocks), "anchored": anchored,
                    "coverage": round(anchored / len(blocks), 4) if blocks else 0.0},
        "pass": bool(fidelity["f1"] >= 0.98 and ordered and not leaked and table_ok
                     and anchored == len(blocks)),
    }


def tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", text.lower())


def token_trigram_recall(hypothesis: str, reference: str) -> float:
    ref_tokens = tokens(reference)
    hyp_tokens = tokens(hypothesis)
    ref = set(tuple(ref_tokens[i:i + 3]) for i in range(len(ref_tokens) - 2))
    got = set(tuple(hyp_tokens[i:i + 3]) for i in range(len(hyp_tokens) - 2))
    if not ref:
        return 0.0
    return round(len(ref & got) / len(ref), 4)


def check_real_paper(result: dict, meta: dict) -> dict:
    abstract = meta.get("abstract", "")
    extracted = body_text(result)
    recall = token_trigram_recall(extracted, abstract)

    title_hit = False
    title = meta.get("title", "")
    if title:
        want = compact(title.lower())[:40]
        title_hit = want in compact(extracted.lower())

    sections = result["document"]["sections"]
    numbered = [s for s in sections if re.match(r"^\d", s["heading"])]
    pages_with_text = sum(1 for p in result["document"]["pages"] if p["has_text_layer"])

    return {
        "case": meta.get("case", "real"),
        "abstract_trigram_recall": recall,
        "title_recovered": title_hit,
        "sections_found": len(sections),
        "numbered_sections": len(numbered),
        "pages_with_text_layer": pages_with_text,
        "pages_total": result["input"]["pages"],
        "unknown_glyph_ratio": result["checks"]["unknown_glyph_ratio"],
        "status": result["status"],
        "pass": bool(recall >= 0.95 and title_hit
                     and result["checks"]["unknown_glyph_ratio"] <= 0.01
                     and result["status"] == "success"),
    }


def check_scanned(result: dict, expectation: dict) -> dict:
    flagged = set(result["document"].get("ocr_required_pages", []))
    must = set(expectation.get("must_flag_pages", []))
    blocks = sum(len(p["blocks"]) for p in result["document"]["pages"])
    return {
        "case": "scanned",
        "status": result["status"],
        "flagged_pages": sorted(flagged),
        "must_flag_pages": sorted(must),
        "all_expected_flagged": must.issubset(flagged),
        "emitted_blocks": blocks,
        "no_fabricated_text": blocks == 0,
        "pass": bool(result["status"] == "partial" and must.issubset(flagged) and blocks == 0),
    }


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, help="directory of *.json parse results")
    ap.add_argument("--truth", required=True, help="ground_truth.json")
    ap.add_argument("--meta", help="arxiv metadata json {case: {title, abstract}}")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    def load(name, base=args.results):
        import os
        path = os.path.join(base, name)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    truth = None
    if args.truth and os.path.exists(args.truth):
        with open(args.truth, "r", encoding="utf-8") as fh:
            truth = json.load(fh)
    checks = []
    tc = load("twocolumn.json")
    if tc and truth:
        checks.append(check_twocolumn(tc, truth))
    sc = load("scanned.json")
    if sc and truth:
        checks.append(check_scanned(sc, truth["scanned_expectation"]))

    meta = load(args.meta, ".") or {}
    for case in ("roberta", "attention"):
        res = load("%s.json" % case)
        if res and case in meta:
            m = dict(meta[case])
            m["case"] = case
            checks.append(check_real_paper(res, m))

    summary = {
        "checks": checks,
        "passed": sum(1 for c in checks if c["pass"]),
        "failed": [c["case"] for c in checks if not c["pass"]],
        "all_pass": all(c["pass"] for c in checks) if checks else False,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"passed": summary["passed"], "failed": summary["failed"],
                      "all_pass": summary["all_pass"]}, ensure_ascii=False))
    return 0 if summary["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
