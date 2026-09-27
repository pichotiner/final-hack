"""Check actual exported files and optionally publish them as the main deck."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from PIL import Image
from pptx import Presentation
import pymupdf

parser = argparse.ArgumentParser()
parser.add_argument("--publish", action="store_true")
args = parser.parse_args()
out = Path(__file__).resolve().parent
root = out.parents[1]
pptx = out / "presentation-minimal-gradient.pptx"
pdf = out / "presentation-minimal-gradient.pdf"
prs = Presentation(pptx)
assert len(prs.slides) == 12
assert all(s.notes_slide.notes_text_frame.text.strip() for s in prs.slides)
with pymupdf.open(pdf) as document:
    assert len(document) == 12
    extracted = " ".join(page.get_text() for page in document)
    for expected in ("EFQA", "NASA", "CRS-7", "ПРОГРЕСС", "289", "Octave"):
        assert expected in extracted, expected
    assert "Lorem ipsum" not in extracted
    for i, page in enumerate(document, 1):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    r = pymupdf.Rect(span["bbox"])
                    assert r.x0 >= -1 and r.y0 >= -1 and r.x1 <= page.rect.width + 1 and r.y1 <= page.rect.height + 1, (i, span["text"])
    assert sum(1 for link in document[-1].get_links() if link.get("uri")) == 6
    previews = []
    for n in (0, 3, 6, 7):
        page = document[n]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(640 / page.rect.width, 640 / page.rect.width), alpha=False)
        previews.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    sheet = Image.new("RGB", (1280, 720), "white")
    for i, image in enumerate(previews):
        sheet.paste(image, (640 * (i % 2), 360 * (i // 2)))
    sheet.save(out / "minimal-gradient-preview.jpg", quality=95)
    pix = document[0].get_pixmap(matrix=pymupdf.Matrix(800 / document[0].rect.width, 800 / document[0].rect.width), alpha=False)
    pix.save(str(out / "minimal-gradient-cover.png"))

report = {"slides": 12, "notes": 12, "pdf_pages": 12, "active_source_links": 6,
          "exporter": "Microsoft PowerPoint", "text_within_pages": True,
          "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
          "pptx_sha256": hashlib.sha256(pptx.read_bytes()).hexdigest()}
(out / "minimal-gradient-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
if args.publish:
    for source, destination in [(pptx, out / "presentation.pptx"), (pdf, out / "presentation.pdf"),
                                (out / "speaker-notes-minimal-gradient.md", out / "speaker-notes.md")]:
        shutil.copyfile(source, destination)
print("PASS: 12 slides, 12 PDF pages, 12 notes, 6 source links, text inside page bounds.")
