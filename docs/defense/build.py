"""Build the offline defense pack from reviewed Markdown and slides.json."""

import hashlib
import json
import os
from pathlib import Path
from xml.sax.saxutils import escape

import markdown
from bs4 import BeautifulSoup
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Flowable, PageBreak, Paragraph, SimpleDocTemplate

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/defense"
FONT = Path(os.environ.get("EFQA_FONT_DIR", "/usr/share/fonts/truetype/dejavu"))
pdfmetrics.registerFont(TTFont("DV", str(FONT / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DV-Bold", str(FONT / "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFontFamily("DV", normal="DV", bold="DV-Bold", italic="DV", boldItalic="DV-Bold")
NAVY = "142B45"
BLUE = "176B95"
BODY = ParagraphStyle("body", fontName="DV", fontSize=9, leading=13, spaceAfter=6, wordWrap="CJK")
H1 = ParagraphStyle("heading", parent=BODY, fontName="DV-Bold", fontSize=17, leading=21, spaceBefore=12)
H2 = ParagraphStyle("sub", parent=H1, fontSize=12, leading=16)
CODE = ParagraphStyle("code", parent=BODY, fontSize=7, leading=10, leftIndent=8, backColor=colors.HexColor("#f0f3f6"))


def text_pdf(c, text, x, top, width, size=18, color=NAVY, bold=False):
    style = ParagraphStyle("draw", fontName="DV-Bold" if bold else "DV", fontSize=size,
                           leading=size * 1.3, textColor=colors.HexColor("#" + color))
    p = Paragraph(escape(text).replace("\n", "<br/>"), style)
    _, height = p.wrap(width, 1000)
    p.drawOn(c, x, top - height)
    return height


def diagram(c, x, y, width):
    labels = ["Источники\nи адаптеры", "API\nи исходники", "Ядро + SQL\nистория и B", "ОТК\nрешение", "Outbox\nERP и ACK"]
    gap = 10
    w = (width - 4 * gap) / 5
    for i, label in enumerate(labels):
        px = x + i * (w + gap)
        c.setFillColor(colors.HexColor("#e3eff7"))
        c.roundRect(px, y, w, 58, 7, fill=1, stroke=0)
        text_pdf(c, label, px + 7, y + 48, w - 14, size=10)
        if i < 4:
            c.setStrokeColor(colors.HexColor("#176b95"))
            c.line(px + w, y + 29, px + w + gap, y + 29)
    text_pdf(c, "Проектный edge: идентификация → качество данных → локальный буфер → центр", x, y - 15, width, size=10)


class Book(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        if hasattr(flowable, "bookmark"):
            key, title = flowable.bookmark
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title, key, level=0)


class Architecture(Flowable):
    def __init__(self):
        super().__init__()
        self.width = 500
        self.height = 120

    def draw(self):
        diagram(self.canv, 0, 50, self.width)


def footer(c, doc):
    c.setFont("DV", 8)
    c.setFillColor(colors.HexColor("#567086"))
    c.drawString(40, 24, "EFQA • теория и архив оснований • срез 26.09.2026")
    c.drawRightString(555, 24, str(doc.page))


def source_flow(path):
    html = markdown.markdown(path.read_text(), extensions=["tables", "fenced_code"])
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for element in soup.children:
        if not getattr(element, "name", None):
            continue
        if element.name == "table":
            for row in element.find_all("tr"):
                text = " | ".join(cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"]))
                result.append(Paragraph(escape(text), BODY))
        elif element.name in {"ol", "ul"}:
            for item in element.find_all("li", recursive=False):
                result.append(Paragraph("• " + escape(item.get_text(" ", strip=True)), BODY))
        elif element.name == "pre":
            result.append(Paragraph(escape(element.get_text()).replace("\n", "<br/>"), CODE))
        else:
            # Keep visible URLs as well as their human-readable source labels.
            for link in element.find_all("a"):
                link.append(" [" + link.get("href", "") + "]")
            style = H2 if element.name.startswith("h") else BODY
            result.append(Paragraph(escape(element.get_text(" ", strip=True)), style))
    return result


def make_book():
    first = [OUT / "system-explained.md", ROOT / "improvements.md"]
    priority = ["architecture/decision-protocol.md", "architecture/p6-acceptance-review.md",
                "architecture/early-warning-design.md", "research/early-warning-findings.md",
                "architecture/early-warning-demo.md"]
    first += [ROOT / "docs" / p for p in priority]
    archive = sorted(p for p in (ROOT / "docs").rglob("*.md") if OUT not in p.parents and p not in first)
    sources = first + archive
    story = [Paragraph("EFQA / Единая книга теории", H1),
             Paragraph("Актуальное объяснение • архитектура • исследования • исторические основания", H2),
             Paragraph("26 сентября 2026. Срез main@53c6f7b. Сначала читайте раздел 1. "
                       "С раздела 8 начинается архив: его старые планы и статусы не являются "
                       "текущими обещаниями. Программные результаты P6 опубликованы ролью №1. "
                       "Раннее предупреждение EW-01 — проект. Полного промышленного внедрения нет.", BODY),
             Architecture(), Paragraph("Навигация", H2)]
    for i, path in enumerate(sources, 1):
        title = path.read_text().splitlines()[0].lstrip("# ")
        story.append(Paragraph(f'<a href="#s{i}">{i}. {escape(title)}</a>', BODY))
    for i, path in enumerate(sources, 1):
        story.append(PageBreak())
        heading = Paragraph(f"Раздел {i}. {'Архив / исходный документ' if i >= 8 else 'Опорный материал'}", H1)
        heading.bookmark = (f"s{i}", f"{i}. {path.stem}")
        story += [heading, Paragraph(escape(str(path.relative_to(ROOT))), CODE)]
        story += source_flow(path)
    Book(str(OUT / "theory.pdf"), pagesize=(595, 842), leftMargin=40, rightMargin=40,
         topMargin=38, bottomMargin=42, title="EFQA — единая книга теории", author="Команда EFQA").build(
             story, onFirstPage=footer, onLaterPages=footer)
    manifest = {"baseline": "53c6f7b", "source_count": len(sources), "sources": [
        {"path": str(p.relative_to(ROOT)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sources]}
    (OUT / "sources-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def make_slides():
    slides = json.loads((OUT / "slides.json").read_text())
    assert sum(s["seconds"] for s in slides) == 360
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    c = canvas.Canvas(str(OUT / "presentation.pdf"), pagesize=(960, 540))
    c.setTitle("EFQA — защита за 6 минут")
    notes = ["# Выступление на 6 минут", "", ("8 слайдов, 360 секунд. Время включает указание на схему и кадр. "
             "Репетируйте с таймером; при быстром чтении используйте остаток на объяснение примера. "
             "Полное видео показывается отдельно, после выступления."), ""]
    elapsed = 0
    for index, s in enumerate(slides, 1):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        def box(text, x, y, w, h, size, bold=False, color=NAVY, slide=slide):
            shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
            tf = shape.text_frame
            tf.word_wrap = True
            for n, line in enumerate(text.split("\n")):
                p = tf.paragraphs[0] if n == 0 else tf.add_paragraph()
                p.text = line
                p.font.name, p.font.size, p.font.bold = "DejaVu Sans", Pt(size), bold
                p.font.color.rgb = RGBColor.from_string(color)
                p.space_after = Pt(10)
            return shape
        c.setFillColor(colors.HexColor("#176b95"))
        c.rect(0, 528, 960, 12, fill=1, stroke=0)
        text_pdf(c, s["tag"], 42, 504, 860, 12, BLUE, True)
        text_pdf(c, s["title"], 42, 466, 865, 27, NAVY, True)
        box(s["tag"], .5, .35, 12.2, .4, 12, color=BLUE)
        box(s["title"], .5, .85, 12.2, 1.1, 27, True)
        if s.get("image"):
            text_pdf(c, "\n\n".join(s["bullets"]), 42, 365, 380, 18)
            box("\n".join(s["bullets"]), .5, 2.15, 5.1, 4.3, 18)
            image = ROOT / s["image"]
            c.drawImage(ImageReader(str(image)), 445, 90, width=475, height=290,
                        preserveAspectRatio=True, anchor="c", mask="auto")
            slide.shapes.add_picture(str(image), Inches(6.2), Inches(2.15), width=Inches(6.4))
        else:
            text_pdf(c, "\n\n".join("• " + t for t in s["bullets"]), 42, 357, 860, 21)
            box("\n".join("• " + t for t in s["bullets"]), .5, 2.3, 12.2, 3.1, 21)
        if s.get("diagram"):
            diagram(c, 42, 92, 865)
            labels = ["Источники\nадаптеры", "API\nисходники", "Ядро + SQL\nистория и B", "ОТК\nрешение", "Outbox\nERP и ACK"]
            for n, label in enumerate(labels):
                shape = slide.shapes.add_shape(5, Inches(.6 + n * 2.5), Inches(5.25), Inches(2.3), Inches(.9))
                shape.fill.solid()
                shape.fill.fore_color.rgb = RGBColor.from_string("E3EFF7")
                box(label, .65 + n * 2.5, 5.3, 2.2, .9, 14)
            box("Проектный edge: идентификация → качество → буфер → центр", .6, 6.3, 12, .5, 13)
        c.setFillColor(colors.HexColor("#567086"))
        c.setFont("DV", 9)
        c.drawString(42, 26, "EFQA • P6: синтетический стенд • EW-01: проект • 26.09.2026")
        c.drawRightString(916, 26, f"{index}/8")
        box(f"EFQA • P6: стенд / EW-01: проект                                      {index}/8", .5, 7, 12.2, .3, 10)
        slide.notes_slide.notes_text_frame.text = s["notes"]
        end = elapsed + s["seconds"]
        notes += [f"## {index}. {s['title']} — {elapsed // 60}:{elapsed % 60:02d}–{end // 60}:{end % 60:02d}", "", s["notes"], ""]
        elapsed = end
        c.showPage()
    c.save()
    prs.save(OUT / "presentation.pptx")
    (OUT / "speaker-notes.md").write_text("\n".join(notes))


if __name__ == "__main__":
    make_book()
    make_slides()
    print("Built theory.pdf, presentation.pdf/.pptx, speaker-notes.md, sources-manifest.json")
