#!/usr/bin/env python3
"""pdffixtures.py -- build the demo case PDFs and their ground truth.

Why synthesise a case at all: the platform's three fixed cases are not public, so
the Skill has to be demonstrated on inputs whose correct answer is known exactly.
A generated two-column page gives per-character ground truth, which is what makes
the fidelity numbers in characterization.json checkable instead of asserted.

The writer below emits plain PDF 1.4 with a base-14 font and explicit /Widths, so
the layout arithmetic is exact and the extractor sees a realistic font dictionary
rather than a toy one.

    python3 pdffixtures.py --outdir cases
"""

from __future__ import annotations

import json
import os
import sys

PAGE_W, PAGE_H = 612.0, 792.0
CHAR_W = 0.6  # Courier: every glyph is 600/1000 em


# --------------------------------------------------------------------------
# minimal PDF writer
# --------------------------------------------------------------------------

def _esc(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _text_ops(x: float, y: float, size: float, text: str) -> str:
    return (f"BT\n/F1 {size} Tf\n1 0 0 1 {x:.2f} {y:.2f} Tm\n"
            f"({_esc(text)}) Tj\nET\n")


def build_pdf(pages: list[dict], font_name: str = "Courier") -> bytes:
    """pages: [{"ops": [callables/strings], "image": optional (w,h,rgb bytes)}]"""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)  # 1-based

    catalog_num = add(b"")  # placeholder, filled later
    pages_num = add(b"")
    font_num = add(
        (f"<< /Type /Font /Subtype /Type1 /BaseFont /{font_name} "
         f"/Encoding /WinAnsiEncoding /FirstChar 32 /Widths ["
         + " ".join(["600"] * 224) + "] >>").encode("latin-1"))

    page_nums = []
    for page in pages:
        content = "".join(page["ops"]).encode("latin-1")
        res = f"/Font << /F1 {font_num} 0 R >>".encode("latin-1")
        if page.get("image"):
            w, h, rgb = page["image"]
            import zlib
            body = zlib.compress(rgb, 9)
            img_num = add(
                (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                 f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                 f"/Length {len(body)} >>\nstream\n").encode("latin-1") + body + b"\nendstream")
            res = (f"/Font << /F1 {font_num} 0 R >> /XObject << /Im1 {img_num} 0 R >>"
                   ).encode("latin-1")
        content_num = add(
            (f"<< /Length {len(content)} >>\nstream\n").encode("latin-1") + content +
            b"\nendstream")
        page_num = add(
            (f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
             f"/Resources << {res.decode('latin-1')} >> /Contents {content_num} 0 R >>"
             ).encode("latin-1"))
        page_nums.append(page_num)

    objects[catalog_num - 1] = f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin-1")
    kids = " ".join(f"{n} 0 R" for n in page_nums)
    objects[pages_num - 1] = (f"<< /Type /Pages /Kids [{kids}] /Count {len(page_nums)} >>"
                              ).encode("latin-1")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode("latin-1")
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_num} 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode("latin-1")
    return bytes(out)


# --------------------------------------------------------------------------
# content
# --------------------------------------------------------------------------

def wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        candidate = f"{cur} {word}".strip()
        if len(candidate) <= width:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
        while len(word) > width:  # long token: hyphenate like a typesetter
            lines.append(word[:width - 1] + "-")
            word = word[width - 1:]
        cur = word
    if cur:
        lines.append(cur)
    return lines


PAGE1_COL1 = [
    "Text extraction from research papers is usually evaluated on rendered pages, "
    "not on the structure a reader needs. A sentence that begins in the left column "
    "and ends in the right column is a sentence no reader ever wrote, yet column "
    "interleaving produces exactly that artefact.",
    "The failure is easy to miss because the words are all present and the character "
    "count looks healthy. Only a reader notices that the grammar has collapsed.",
    "We treat reading order as a geometric problem: glyph boxes are cut recursively "
    "along whitespace, so a vertical cut yields columns that are emitted left to "
    "right before any line inside them is assembled.",
]

PAGE1_COL2 = [
    "Two properties matter for downstream use. First, every character keeps a page "
    "and a bounding box, so a claim can be traced back to the spot it came from. "
    "Second, the extractor reports what it could not read instead of smoothing it over.",
    "Reproducibility of the layout analysis is what makes the second property useful: "
    "a reviewer can rerun the same page and obtain the same boxes.",
]

PAGE2_COL1 = [
    "The recursive cut is applied to glyph boxes rather than to finished lines. Line "
    "assembly needs a column decision first, and a column decision made per line "
    "oscillates on justified text where the right edge is ragged.",
    "Header and footer removal is decided per document, not per page: a band line is "
    "dropped only when the same normalised text recurs across pages.",
]

PAGE2_COL2 = [
    "Tables are detected from aligned whitespace columns. A candidate needs at least "
    "three rows, consistent column starts and numeric content before it is reported.",
    "When the heuristic declines, the block stays ordinary text and the result says "
    "no table was found, which is the honest outcome for a two-column page body.",
]

TABLE_ROWS = [
    ["Model", "Precision", "Recall", "F1"],
    ["baseline", "0.812", "0.774", "0.793"],
    ["column-cut", "0.965", "0.958", "0.961"],
    ["column-cut+anchors", "0.971", "0.960", "0.965"],
]

REFS = [
    "[1] A. Author. Reading order in two-column documents. Journal of Layout, 2019.",
    "[2] B. Writer and C. Editor. Evidence anchors for extracted text. In Proc. DocEng, 2021.",
    "[3] D. Reader. Table structure from whitespace. Technical report, 2022.",
]


def cwidth(size: float) -> float:
    return CHAR_W * size


def centered(text: str, size: float, y: float) -> str:
    x = (PAGE_W - len(text) * cwidth(size)) / 2.0
    return _text_ops(round(x, 2), y, size, text)


def to_image_content(lines: list[tuple[float, float, float, str]]) -> tuple[int, int, bytes]:
    """Rasterise text lines the way a scanned page looks: pixels, no text layer."""
    from PIL import Image, ImageDraw, ImageFont

    scale = 2  # 144 dpi
    img = Image.new("RGB", (int(PAGE_W * scale), int(PAGE_H * scale)), "white")
    draw = ImageDraw.Draw(img)
    font_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf")
    for x, y, size, text in lines:
        try:
            font = ImageFont.truetype(font_path, int(size * scale))
        except OSError:
            font = ImageFont.load_default()
        draw.text((x * scale, (PAGE_H - y) * scale), text, fill="black", font=font)
    return img.width, img.height, img.tobytes()


def make_cases(outdir: str) -> dict:
    os.makedirs(outdir, exist_ok=True)
    truth: dict = {"case_id": "twocolumn-synthetic", "pages": [], "tables": [], "headers": [],
                   "footers": []}

    col1_x, col2_x = 56.0, 316.0
    body_size, leading = 10.0, 12.0
    per_line = 40

    # ---------------- page 1 ----------------
    ops: list[str] = []
    title = "Column-Aware Text Extraction with Traceable Evidence"
    ops.append(centered(title, 18.0, 726.0))
    ops.append(centered("A. Researcher, B. Reviewer", 12.0, 704.0))
    ops.append(centered("Institute of Document Analysis", 11.0, 690.0))
    ops.append(_text_ops(col1_x, 662.0, 11.0, "Abstract"))
    abstract = ("We present a layout reader that reconstructs two-column reading order "
                "from glyph geometry, keeps a page and bounding box for every character, "
                "and reports unreadable regions instead of guessing them.")
    y = 646.0
    for line in wrap(abstract, 88):
        ops.append(_text_ops(44.0, y, 9.0, line))
        y -= 11.0

    truth["pages"].append({
        "page": 1,
        "parts": [
            {"kind": "title", "text": title, "bbox": [None, 726.0]},
            {"kind": "authors", "text": "A. Researcher, B. Reviewer"},
            {"kind": "abstract", "text": abstract},
        ],
    })

    ops.append(_text_ops(col1_x, 560.0, 11.0, "1  Introduction"))
    ops.append(_text_ops(col2_x, 560.0, 11.0, "2  Method"))

    col1_lines, col2_lines = [], []
    y1 = 540.0
    for para in PAGE1_COL1:
        for line in wrap(para, per_line):
            col1_lines.append(line)
            ops.append(_text_ops(col1_x, y1, body_size, line))
            y1 -= leading
        y1 -= 6.0
    y2 = 540.0
    for para in PAGE1_COL2:
        for line in wrap(para, per_line):
            col2_lines.append(line)
            ops.append(_text_ops(col2_x, y2, body_size, line))
            y2 -= leading
        y2 -= 6.0

    # running header and page number, exactly what a filter must drop
    ops.append(_text_ops(col1_x, 762.0, 8.0, "Preprint - do not distribute"))
    ops.append(centered("1", 9.0, 40.0))

    truth["pages"][0]["parts"].append({"kind": "column", "index": 1, "column": "left",
                                       "lines": col1_lines, "paragraphs": PAGE1_COL1})
    truth["pages"][0]["parts"].append({"kind": "column", "index": 2, "column": "right",
                                       "lines": col2_lines, "paragraphs": PAGE1_COL2})
    truth["headers"].append("Preprint - do not distribute")
    truth["footers"].append("1")

    page1 = {"ops": ops}

    # ---------------- page 2 ----------------
    ops2: list[str] = []
    ops2.append(_text_ops(col1_x, 762.0, 8.0, "Preprint - do not distribute"))
    ops2.append(centered("2", 9.0, 40.0))
    ops2.append(_text_ops(col1_x, 720.0, 11.0, "3  Results"))

    c1_col1, c1_col2 = [], []
    y1 = 700.0
    for para in PAGE2_COL1:
        for line in wrap(para, per_line):
            c1_col1.append(line)
            ops2.append(_text_ops(col1_x, y1, body_size, line))
            y1 -= leading
        y1 -= 6.0
    y2 = 700.0
    for para in PAGE2_COL2:
        for line in wrap(para, per_line):
            c1_col2.append(line)
            ops2.append(_text_ops(col2_x, y2, body_size, line))
            y2 -= leading
        y2 -= 6.0

    # table below both columns, spanning the text width
    ty = 560.0
    ops2.append(_text_ops(col1_x, ty, 9.5, "Table 1: Extraction accuracy on the two-column set."))
    ty -= 16.0
    col_x = [col1_x, 236.0, 356.0, 470.0]
    for row in TABLE_ROWS:
        for cx, cell in zip(col_x, row):
            ops2.append(_text_ops(cx, ty, 9.0, cell))
        ty -= 13.0

    ops2.append(_text_ops(col1_x, 430.0, 11.0, "4  Conclusion"))
    concl = ("Reading order belongs to the page geometry, not to the text string. "
             "Cutting glyph boxes first and assembling lines second keeps columns apart "
             "and keeps every character traceable.")
    yy = 412.0
    for line in wrap(concl, 88):
        ops2.append(_text_ops(44.0, yy, body_size, line))
        yy -= leading

    truth["pages"].append({
        "page": 2,
        "parts": [
            {"kind": "column", "index": 3, "column": "left", "lines": c1_col1,
             "paragraphs": PAGE2_COL1},
            {"kind": "column", "index": 4, "column": "right", "lines": c1_col2,
             "paragraphs": PAGE2_COL2},
            {"kind": "table", "rows": TABLE_ROWS},
            {"kind": "conclusion", "text": concl},
        ],
    })
    truth["tables"].append({"page": 2, "rows": TABLE_ROWS,
                            "caption": "Table 1: Extraction accuracy on the two-column set."})

    page2 = {"ops": ops2}

    # ---------------- page 3 ----------------
    ops3: list[str] = []
    ops3.append(_text_ops(col1_x, 762.0, 8.0, "Preprint - do not distribute"))
    ops3.append(centered("3", 9.0, 40.0))
    ops3.append(_text_ops(col1_x, 730.0, 11.0, "References"))
    ry = 712.0
    for ref in REFS:
        for line in wrap(ref, 92):
            ops3.append(_text_ops(col1_x, ry, 9.0, line))
            ry -= 11.0
        ry -= 4.0
    page3 = {"ops": ops3}
    truth["pages"].append({"page": 3, "parts": [{"kind": "references", "entries": REFS}]})
    truth["references"] = REFS

    # body text in reading order, the thing char-F1 is measured against
    truth["body_text"] = " ".join(
        PAGE1_COL1 + PAGE1_COL2 + PAGE2_COL1 + PAGE2_COL2 + [concl])

    pdf = build_pdf([page1, page2, page3])
    twocol_path = os.path.join(outdir, "twocolumn.pdf")
    with open(twocol_path, "wb") as fh:
        fh.write(pdf)

    # ---------------- scanned variant ----------------
    text_lines = []
    y = 726.0
    text_lines.append((60.0, y, 16, title))
    y -= 26
    text_lines.append((60.0, y, 11, "A. Researcher, B. Reviewer"))
    y -= 24
    for para in PAGE1_COL1[:1]:
        for line in wrap(para, 88):
            text_lines.append((44.0, y, 10, line))
            y -= 12
    y -= 18
    for para in PAGE1_COL1[1:]:
        for line in wrap(para, 88):
            text_lines.append((44.0, y, 10, line))
            y -= 12
    w, h, rgb = to_image_content(text_lines)
    img_ops = ["q\n612 0 0 792 0 0 cm\n/Im1 Do\nQ\n"]
    scanned = build_pdf([{"ops": img_ops, "image": (w, h, rgb)}])
    scanned_path = os.path.join(outdir, "scanned.pdf")
    with open(scanned_path, "wb") as fh:
        fh.write(scanned)

    truth["files"] = {
        "twocolumn": os.path.basename(twocol_path),
        "scanned": os.path.basename(scanned_path),
    }
    truth["scanned_expectation"] = {
        "status": "partial",
        "must_flag_pages": [1],
        "must_not_emit_text": True,
    }
    with open(os.path.join(outdir, "ground_truth.json"), "w", encoding="utf-8") as fh:
        json.dump(truth, fh, ensure_ascii=False, indent=2)
    return truth


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="cases")
    args = ap.parse_args(argv)
    truth = make_cases(args.outdir)
    print(json.dumps({"outdir": args.outdir, "files": truth["files"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
