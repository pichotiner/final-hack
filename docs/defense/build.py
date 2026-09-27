"""Build the offline EFQA defense pack and the presentation."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from xml.sax.saxutils import escape

import markdown
from bs4 import BeautifulSoup
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.xmlchemy import OxmlElement
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

NAVY = "071522"
PANEL = "10283B"
PANEL2 = "12344A"
MINT = "6AE7D9"
ORANGE = "FFB454"
RED = "FF6B6B"
TEXT = "F3F8FA"
MUTED = "A5BCC8"
LINE = "25465A"
BODY = ParagraphStyle("body", fontName="DV", fontSize=9, leading=13, spaceAfter=6, wordWrap="CJK")
H1 = ParagraphStyle("heading", parent=BODY, fontName="DV-Bold", fontSize=17, leading=21, spaceBefore=12)
H2 = ParagraphStyle("sub", parent=H1, fontSize=12, leading=16)
CODE = ParagraphStyle("code", parent=BODY, fontSize=7, leading=10, leftIndent=8, backColor=colors.HexColor("#f0f3f6"))


def text_pdf(c, text, x, top, width, size=18, color=TEXT, bold=False):
    style = ParagraphStyle(
        "draw", fontName="DV-Bold" if bold else "DV", fontSize=size,
        leading=size * 1.22, textColor=colors.HexColor("#" + color), wordWrap="CJK",
    )
    p = Paragraph(escape(str(text)).replace("\n", "<br/>"), style)
    _, height = p.wrap(width, 2000)
    p.drawOn(c, x, top - height)
    return height


def diagram(c, x, y, width):
    labels = ["Источники\nи адаптеры", "API\nи исходники", "Ядро + SQL\nистория и B", "ОТК\nрешение", "Outbox\nERP и ACK"]
    gap = 10
    w = (width - 4 * gap) / 5
    for i, label in enumerate(labels):
        px = x + i * (w + gap)
        c.setFillColor(colors.HexColor("#e3eff7")); c.roundRect(px, y, w, 58, 7, fill=1, stroke=0)
        text_pdf(c, label, px + 7, y + 48, w - 14, size=10, color=NAVY)
        if i < 4:
            c.setStrokeColor(colors.HexColor("#176b95")); c.line(px + w, y + 29, px + w + gap, y + 29)
    text_pdf(c, "Проектный edge: идентификация → качество данных → локальный буфер → центр", x, y - 15, width, size=10, color=NAVY)


class Book(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        if hasattr(flowable, "bookmark"):
            key, title = flowable.bookmark
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title, key, level=0)


class Architecture(Flowable):
    def __init__(self):
        super().__init__(); self.width = 500; self.height = 120

    def draw(self):
        diagram(self.canv, 0, 50, self.width)


def footer(c, doc):
    c.setFont("DV", 8); c.setFillColor(colors.HexColor("#567086"))
    c.drawString(40, 24, "EFQA • теория и архив оснований • срез 27.09.2026")
    c.drawRightString(555, 24, str(doc.page))


def source_flow(path):
    html = markdown.markdown(path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"])
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
            for link in element.find_all("a"):
                link.append(" [" + link.get("href", "") + "]")
            style = H2 if element.name.startswith("h") else BODY
            result.append(Paragraph(escape(element.get_text(" ", strip=True)), style))
    return result


def make_book():
    first = [OUT / "system-explained.md", OUT / "space-quality-research.md", ROOT / "improvements.md"]
    priority = ["architecture/decision-protocol.md", "architecture/engineering-defense.md",
                "architecture/p8-pilot-completion.md", "architecture/early-warning-design.md",
                "architecture/early-warning-demo.md"]
    first += [ROOT / "docs" / p for p in priority if (ROOT / "docs" / p).exists()]
    archive = sorted(p for p in (ROOT / "docs").rglob("*.md") if OUT not in p.parents and p not in first)
    sources = first + archive
    story = [Paragraph("EFQA / Единая книга теории", H1),
             Paragraph("Актуальное объяснение • архитектура • исследования • исторические основания", H2),
             Paragraph("Срез main. Сначала читайте разделы системы и исследования. Архивные планы не являются текущими обещаниями. Полного промышленного внедрения нет.", BODY),
             Architecture(), Paragraph("Навигация", H2)]
    for i, path in enumerate(sources, 1):
        title = path.read_text(encoding="utf-8").splitlines()[0].lstrip("# ")
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
    manifest = {"baseline": "main", "source_count": len(sources), "sources": [
        {"path": str(p.relative_to(ROOT)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sources]}
    (OUT / "sources-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pdf_base(c, index, tag, title, total):
    c.setFillColor(colors.HexColor("#" + NAVY)); c.rect(0, 0, 960, 540, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#" + LINE)); c.rect(42, 525, 876, 2, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#" + MINT)); c.rect(42, 525, 876 * index / total, 2, fill=1, stroke=0)
    text_pdf(c, tag, 48, 505, 850, 10, MINT, True)
    text_pdf(c, title, 48, 470, 850, 27, TEXT, True)


def pdf_footer(c, index, total):
    c.setFont("DV", 8); c.setFillColor(colors.HexColor("#" + MUTED))
    c.drawString(48, 22, "EFQA  /  Evidence-First Quality Architecture")
    c.drawRightString(912, 22, f"{index:02d} / {total:02d}")


def panel(c, x, y, w, h, fill=PANEL, stroke=LINE, radius=12):
    c.setFillColor(colors.HexColor("#" + fill)); c.setStrokeColor(colors.HexColor("#" + stroke))
    c.roundRect(x, y, w, h, radius, fill=1, stroke=1)


def circle(c, x, y, r, fill=MINT):
    c.setFillColor(colors.HexColor("#" + fill)); c.circle(x, y, r, fill=1, stroke=0)


def draw_blindspot(c, slide, data, index, total):
    """Draw this slide once, in points/top-left coordinates, into both formats.

    Explicit line breaks and font metrics guard against overflow. All PowerPoint
    elements remain editable; the PDF is not used as a slide-sized bitmap.
    """
    def block(x, y, width, height, fill, stroke=None, ellipse=False):
        c.saveState()
        c.setFillColor(colors.HexColor("#" + fill))
        if stroke:
            c.setStrokeColor(colors.HexColor("#" + stroke))
        c.setLineWidth(1.2)
        kind = MSO_SHAPE.OVAL if ellipse else MSO_SHAPE.ROUNDED_RECTANGLE
        if ellipse:
            c.ellipse(x, 540 - y - height, x + width, 540 - y, fill=1, stroke=bool(stroke))
        else:
            c.roundRect(x, 540 - y - height, width, height, 12, fill=1, stroke=bool(stroke))
        c.restoreState()
        obj = slide.shapes.add_shape(kind, Pt(x), Pt(y), Pt(width), Pt(height))
        obj.fill.solid()
        obj.fill.fore_color.rgb = RGBColor.from_string(fill)
        if stroke:
            obj.line.color.rgb = RGBColor.from_string(stroke)
            obj.line.width = Pt(1.2)
        else:
            obj.line.fill.background()
        if not ellipse:
            obj.adjustments[0] = 12 / min(width, height)
        obj._element.spPr.append(OxmlElement("a:effectLst"))

    def label(value, x, y, width, height, size, color=TEXT, bold=False, centered=False):
        lines = value.split("\n")
        font = "DV-Bold" if bold else "DV"
        leading = size * 1.25
        if any(pdfmetrics.stringWidth(line, font, size) > width - 2 for line in lines):
            raise ValueError(f"Slide 02 text exceeds width: {value!r}")
        if len(lines) * leading > height:
            raise ValueError(f"Slide 02 text exceeds height: {value!r}")
        c.saveState()
        c.setFont(font, size)
        c.setFillColor(colors.HexColor("#" + color))
        for n, line in enumerate(lines):
            baseline = 540 - y - size - n * leading
            if centered:
                c.drawCentredString(x + width / 2, baseline, line)
            else:
                c.drawString(x, baseline, line)
        c.restoreState()
        obj = slide.shapes.add_textbox(Pt(x), Pt(y), Pt(width), Pt(height))
        tf = obj.text_frame
        tf.word_wrap = False
        tf.auto_size = MSO_AUTO_SIZE.NONE
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        for n, line in enumerate(lines):
            p = tf.paragraphs[0] if n == 0 else tf.add_paragraph()
            p.text = line
            p.font.name = "DejaVu Sans"
            p.font.size = Pt(size)
            p.font.bold = bold
            p.font.color.rgb = RGBColor.from_string(color)
            p.alignment = PP_ALIGN.CENTER if centered else PP_ALIGN.LEFT
            p.line_spacing = Pt(leading)
            p.space_before = p.space_after = Pt(0)

    def link(x1, y1, x2, y2, color=LINE, weight=2):
        c.saveState()
        c.setStrokeColor(colors.HexColor("#" + color))
        c.setLineWidth(weight)
        c.line(x1, 540 - y1, x2, 540 - y2)
        c.restoreState()
        obj = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Pt(x1), Pt(y1), Pt(x2), Pt(y2))
        obj.line.color.rgb = RGBColor.from_string(color)
        obj.line.width = Pt(weight)
        obj._element.spPr.append(OxmlElement("a:effectLst"))

    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = RGBColor.from_string(NAVY)
    c.setFillColor(colors.HexColor("#" + NAVY))
    c.rect(0, 0, 960, 540, fill=1, stroke=0)
    link(48, 16, 912, 16, LINE, 1.5)
    link(48, 16, 48 + 864 * index / total, 16, MINT, 1.5)
    label(data["tag"], 48, 34, 864, 18, 10, MINT, True)
    label(data["title"], 48, 67, 864, 53, 38, TEXT, True)
    label(data["subtitle"], 48, 127, 864, 25, 18, MUTED)

    # One route, but two different levels of knowledge. The amber zone contains
    # possible origins; the coral zone is the known observation, not a cause.
    block(48, 190, 518, 194, "191F29", "695333")
    block(650, 190, 262, 194, "29212B", RED)
    label(data["origin_label"], 70, 208, 208, 23, 16, ORANGE, True)
    label(data["origin_status"], 326, 211, 214, 19, 13, ORANGE, True)
    label(data["detection_label"], 674, 208, 172, 23, 16, RED, True)
    label(data["detection_status"], 854, 211, 48, 19, 12, RED, True)

    centers = [132, 307, 482]
    for left, right in zip(centers, centers[1:]):
        link(left + 26, 278, right - 26, 278, "695333")
    link(579, 278, 637, 278, MUTED)
    link(629, 272, 637, 278, MUTED)
    link(629, 284, 637, 278, MUTED)
    for center, (number, title, detail) in zip(centers, data["origins"]):
        block(center - 25, 253, 50, 50, "292A2C", ORANGE, ellipse=True)
        label(number, center - 25, 266, 50, 23, 16, ORANGE, True, centered=True)
        label(title, center - 74, 320, 148, 27, 18, TEXT, True, centered=True)
        label(detail, center - 74, 349, 148, 22, 16, MUTED, centered=True)

    block(753, 250, 56, 56, RED, ellipse=True)
    label("!", 753, 254, 56, 43, 32, NAVY, True, centered=True)
    label(data["detection_title"], 662, 320, 238, 26, 17, TEXT, True, centered=True)
    label(data["detection_detail"], 662, 349, 238, 23, 16, MUTED, centered=True)

    link(48, 422, 48, 480, MINT, 3)
    label(data["takeaway"], 68, 420, 842, 31, 22, MINT, True)
    label(data["evidence"], 68, 455, 842, 25, 18, TEXT)
    label("EFQA / Evidence-First Quality Architecture", 48, 514, 790, 16, 9, MUTED)
    label(f"{index:02d} / {total:02d}", 864, 514, 48, 16, 9, MUTED)


def make_slides():
    slides = json.loads((OUT / "slides.json").read_text(encoding="utf-8"))
    total = len(slides)
    assert total == 11 and sum(s["seconds"] for s in slides) == 420
    prs = Presentation(); prs.slide_width = Inches(13.333); prs.slide_height = Inches(7.5)
    c = canvas.Canvas(str(OUT / "presentation.pdf"), pagesize=(960, 540))
    c.setTitle("EFQA — доказательный контроль качества")
    notes = ["# Выступление EFQA", "", "11 слайдов, 420 секунд. Визуальная версия: короткие тезисы на слайдах, подробности — в заметках докладчика.", ""]

    def ptext(slide, text, x, y, w, h, size=15, color=TEXT, bold=False, align=None):
        shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = shape.text_frame; tf.clear(); tf.word_wrap = True
        tf.margin_left = Inches(.04); tf.margin_right = Inches(.04); tf.margin_top = Inches(.03); tf.margin_bottom = Inches(.02)
        p = tf.paragraphs[0]; p.text = str(text); p.font.name = "DejaVu Sans"; p.font.size = Pt(size); p.font.bold = bold
        p.font.color.rgb = RGBColor.from_string(color); p.alignment = align or PP_ALIGN.LEFT
        return shape

    def shape(slide, kind, x, y, w, h, fill=PANEL, line=LINE, radius=True):
        sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
                                    Inches(x), Inches(y), Inches(w), Inches(h))
        sh.fill.solid(); sh.fill.fore_color.rgb = RGBColor.from_string(fill)
        sh.line.color.rgb = RGBColor.from_string(line); sh.line.width = Pt(1)
        return sh

    def slide_header(slide, index, s):
        bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
        bg.fill.solid(); bg.fill.fore_color.rgb = RGBColor.from_string(NAVY); bg.line.fill.background()
        bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(.5), Inches(.23), Inches(12.3), Inches(.025))
        bar.fill.solid(); bar.fill.fore_color.rgb = RGBColor.from_string(LINE); bar.line.fill.background()
        progress = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(.5), Inches(.23), Inches(12.3 * index / total), Inches(.025))
        progress.fill.solid(); progress.fill.fore_color.rgb = RGBColor.from_string(MINT); progress.line.fill.background()
        ptext(slide, s["tag"], .65, .38, 12, .25, 9, MINT, True)
        ptext(slide, s["title"], .65, .68, 12, .85, 24, TEXT, True)

    def pcard(slide, x, y, w, h, kicker, title, body, accent=MINT):
        shape(slide, "card", x, y, w, h)
        sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(.07), Inches(h))
        sh.fill.solid(); sh.fill.fore_color.rgb = RGBColor.from_string(accent); sh.line.fill.background()
        ptext(slide, kicker, x + .22, y + .16, w - .35, .24, 8, accent, True)
        ptext(slide, title, x + .22, y + .43, w - .35, .42, 14, TEXT, True)
        ptext(slide, body, x + .22, y + .9, w - .35, h - 1.0, 9.5, MUTED)

    elapsed = 0
    for index, s in enumerate(slides, 1):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        kind = s["kind"]
        if kind != "blindspot":
            slide_header(slide, index, s)
            pdf_base(c, index, s["tag"], s["title"], total)

        if kind == "hero":
            text_pdf(c, s["subtitle"], 50, 320, 690, 24, TEXT, True)
            c.setStrokeColor(colors.HexColor("#" + MINT)); c.setLineWidth(2); c.line(50, 222, 730, 222)
            items = [("01", "TRACE", "история детали"), ("02", "EXPLAIN", "основание B"), ("03", "REVIEW", "поздний сигнал")]
            for n, (num, title, body) in enumerate(items):
                x = 50 + n * 285; panel(c, x, 105, 245, 105, PANEL2)
                text_pdf(c, num, x + 18, 185, 45, 17, MINT, True)
                text_pdf(c, title, x + 70, 182, 155, 15, TEXT, True)
                text_pdf(c, body, x + 70, 153, 155, 10, MUTED)
            ptext(slide, s["subtitle"], .7, 2.5, 9.7, 1.1, 22, TEXT, True)
            for n, item in enumerate(items):
                pcard(slide, .7 + n * 3.8, 4.85, 3.35, 1.3, item[0], item[1], item[2])
        elif kind == "blindspot":
            draw_blindspot(c, slide, s, index, total)
        elif kind == "architecture":
            xs = [48, 270, 492, 714]
            for n, node in enumerate(s["nodes"]):
                x = xs[n]; panel(c, x, 205, 185, 145, PANEL2)
                text_pdf(c, node[0], x + 16, 326, 150, 11, MINT if n != 2 else ORANGE, True)
                text_pdf(c, node[1], x + 16, 294, 150, 15, TEXT, True)
                text_pdf(c, node[2], x + 16, 255, 150, 10, MUTED)
                if n < 3:
                    c.setStrokeColor(colors.HexColor("#" + MINT)); c.setLineWidth(2); c.line(x + 185, 277, x + 215, 277)
            text_pdf(c, s["footer"], 65, 145, 830, 12, ORANGE, True)
            for n, node in enumerate(s["nodes"]):
                pcard(slide, .68 + n * 3.05, 3.1, 2.7, 1.65, node[0], node[1], node[2], ORANGE if n == 2 else MINT)
                if n < len(s["nodes"]) - 1:
                    ptext(slide, "→", 3.4 + n * 3.05, 3.68, .29, .35, 16, MINT, True, PP_ALIGN.CENTER)
        elif kind == "compare":
            for n, comp in enumerate(s["competitors"]):
                x = 50 + n * 295; panel(c, x, 270, 260, 118, PANEL)
                text_pdf(c, comp[0], x + 16, 365, 220, 11, MINT, True)
                text_pdf(c, comp[1], x + 16, 330, 220, 15, TEXT, True)
                text_pdf(c, comp[2], x + 16, 292, 220, 10, RED)
            panel(c, 50, 105, 860, 112, PANEL2, MINT)
            text_pdf(c, "EFQA", 75, 190, 130, 25, MINT, True)
            for n, point in enumerate(s["efqa"]):
                circle(c, 240 + n * 210, 172, 6, ORANGE)
                text_pdf(c, point, 255 + n * 210, 181, 170, 10, TEXT, True)
            for n, comp in enumerate(s["competitors"]):
                pcard(slide, .7 + n * 4.05, 2.65, 3.65, 1.35, comp[0], comp[1], comp[2], RED)
            shape(slide, "card", .7, 5.2, 11.95, 1.0, PANEL2, MINT)
            ptext(slide, "EFQA", .95, 5.43, 1.2, .35, 18, MINT, True)
            ptext(slide, "AI наблюдает  •  B фиксирует  •  ОТК решает  •  replay пересматривает", 2.1, 5.4, 9.9, .35, 14, TEXT, True)
        elif kind == "case_crs7":
            panel(c, 50, 125, 570, 260, PANEL)
            text_pdf(c, s["event"], 75, 350, 515, 14, ORANGE, True)
            for n, fact in enumerate(s["facts"]):
                circle(c, 85, 290 - n * 48, 5, MINT)
                text_pdf(c, fact, 105, 298 - n * 48, 475, 11, TEXT)
            panel(c, 665, 125, 245, 260, PANEL2, ORANGE)
            text_pdf(c, "?", 720, 315, 130, 72, ORANGE, True)
            text_pdf(c, "одна авария\n≠ одна версия", 690, 220, 190, 15, TEXT, True)
            text_pdf(c, s["lesson"], 50, 85, 860, 12, MINT, True)
            shape(slide, "case", .7, 2.5, 7.45, 3.2, PANEL, LINE)
            ptext(slide, "ОТКАЗ / 2015", 1.0, 2.72, 6.8, .3, 10, ORANGE, True)
            ptext(slide, s["event"], 1.0, 3.06, 6.9, .55, 14, TEXT, True)
            for n, fact in enumerate(s["facts"]):
                ptext(slide, "•", 1.0, 3.77 + n * .46, .25, .25, 14, MINT, True)
                ptext(slide, fact, 1.34, 3.73 + n * .46, 6.4, .48, 10.5, TEXT)
            shape(slide, "lesson", 8.7, 2.5, 3.9, 3.2, PANEL2, ORANGE)
            ptext(slide, "?", 9.83, 2.75, 1.3, .96, 50, ORANGE, True, PP_ALIGN.CENTER)
            ptext(slide, "одна авария ≠ одна версия", 9.05, 3.9, 3.2, .75, 15, TEXT, True, PP_ALIGN.CENTER)
            ptext(slide, "EFQA хранит альтернативы, версии анализа и область применимости контроля.", 9.06, 4.82, 3.15, .58, 10, MINT, True)
        elif kind == "case_cert":
            text_pdf(c, s["amount"], 55, 330, 270, 43, ORANGE, True)
            text_pdf(c, s["amount_label"], 60, 250, 240, 11, MUTED)
            c.setStrokeColor(colors.HexColor("#" + RED)); c.setLineWidth(4); c.line(380, 280, 850, 280)
            for n, item in enumerate(s["chain"]):
                x = 380 + n * 117; circle(c, x, 280, 20, RED if n > 1 else MINT)
                text_pdf(c, str(n + 1), x - 10, 286, 20, 10, NAVY, True)
                text_pdf(c, item, x - 50, 225, 100, 9, TEXT, True)
            panel(c, 55, 95, 850, 75, PANEL2, MINT)
            text_pdf(c, "Урок", 75, 148, 70, 11, MINT, True)
            text_pdf(c, s["lesson"], 145, 148, 735, 11, TEXT, True)
            ptext(slide, s["amount"], .8, 2.55, 3.3, .8, 38, ORANGE, True)
            ptext(slide, s["amount_label"], .85, 3.45, 3.3, .5, 11, MUTED)
            for n, item in enumerate(s["chain"]):
                x = 4.7 + n * 1.55
                if n < len(s["chain"]) - 1:
                    sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x + .35), Inches(4.05), Inches(1.0), Inches(.04))
                    sh.fill.solid(); sh.fill.fore_color.rgb = RGBColor.from_string(RED); sh.line.fill.background()
                circ = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x), Inches(3.8), Inches(.5), Inches(.5))
                circ.fill.solid(); circ.fill.fore_color.rgb = RGBColor.from_string(RED if n > 1 else MINT); circ.line.fill.background()
                ptext(slide, str(n + 1), x, 3.91, .5, .2, 9, NAVY, True, PP_ALIGN.CENTER)
                ptext(slide, item, x - .4, 4.45, 1.3, .45, 9, TEXT, True, PP_ALIGN.CENTER)
            shape(slide, "card", .75, 5.65, 11.85, .7, PANEL2, MINT)
            ptext(slide, "Урок  •  документ не заменяет независимое доказательство", 1.0, 5.88, 11.2, .25, 12, TEXT, True)
        elif kind == "case_ru":
            panel(c, 50, 115, 400, 275, PANEL)
            panel(c, 510, 115, 400, 275, PANEL2, ORANGE)
            text_pdf(c, s["left"][0], 75, 350, 350, 16, MINT, True)
            for n, line in enumerate(s["left"][1:]):
                text_pdf(c, "→ " + line, 78, 305 - n * 45, 340, 12, TEXT)
            text_pdf(c, s["right"][0], 535, 350, 350, 16, ORANGE, True)
            for n, line in enumerate(s["right"][1:]):
                text_pdf(c, "→ " + line, 538, 305 - n * 45, 340, 12, TEXT if n < 2 else ORANGE, n == 2)
            text_pdf(c, "?", 355, 175, 70, 42, MINT, True)
            text_pdf(c, "НЕ УСТАНОВЛЕНО", 185, 155, 220, 10, MINT, True)
            text_pdf(c, "63", 805, 175, 90, 40, ORANGE, True)
            text_pdf(c, "ДЕЙСТВИЙ ≠ ДЕФЕКТОВ", 545, 155, 270, 10, ORANGE, True)
            pcard(slide, .75, 2.65, 5.75, 2.65, s["left"][0], "Возможные факторы", "\n".join("→ " + x for x in s["left"][1:]), MINT)
            pcard(slide, 6.85, 2.65, 5.75, 2.65, s["right"][0], "Что важно не перепутать", "\n".join("→ " + x for x in s["right"][1:]), ORANGE)
            ptext(slide, "?", 5.35, 4.7, .55, .55, 30, MINT, True, PP_ALIGN.CENTER)
            ptext(slide, "НЕ УСТАНОВЛЕНО", 4.15, 5.25, 2.95, .25, 8, MINT, True, PP_ALIGN.CENTER)
            ptext(slide, "63", 11.35, 4.68, .9, .6, 28, ORANGE, True, PP_ALIGN.CENTER)
            ptext(slide, "ДЕЙСТВИЙ ≠ ДЕФЕКТОВ", 9.25, 5.25, 3.1, .25, 8, ORANGE, True, PP_ALIGN.CENTER)
        elif kind == "stats":
            text_pdf(c, s["big"], 55, 350, 190, 110, MINT, True)
            text_pdf(c, s["big_label"], 65, 220, 170, 13, MUTED, True)
            for n, bar in enumerate(s["bars"]):
                y = 345 - n * 58; text_pdf(c, bar[0], 285, y + 22, 260, 9.5, TEXT, True)
                c.setFillColor(colors.HexColor("#" + PANEL2)); c.roundRect(555, y, 260, 23, 11, fill=1, stroke=0)
                c.setFillColor(colors.HexColor("#" + (ORANGE if n == 2 else MINT))); c.roundRect(555, y, 260 * bar[1] / 2, 23, 11, fill=1, stroke=0)
                text_pdf(c, bar[2], 835, y + 19, 50, 10, MINT, True)
            panel(c, 285, 85, 600, 55, PANEL2, ORANGE)
            text_pdf(c, s["foot"], 305, 124, 560, 10, ORANGE, True)
            ptext(slide, s["big"], .9, 2.4, 2.5, 1.1, 54, MINT, True)
            ptext(slide, s["big_label"], 1.0, 3.55, 2.4, .45, 12, MUTED, True)
            for n, bar in enumerate(s["bars"]):
                y = 2.65 + n * .62
                ptext(slide, bar[0], 3.4, y, 3.6, .25, 10, TEXT, True)
                shape(slide, "card", 7.0, y + .03, 4.2, .2, PANEL2, PANEL2)
                shape(slide, "card", 7.0, y + .03, 4.2 * bar[1] / 2, .2, ORANGE if n == 2 else MINT, ORANGE if n == 2 else MINT)
                ptext(slide, bar[2], 11.45, y - .02, .8, .3, 11, MINT, True)
            ptext(slide, s["foot"], 3.4, 5.55, 8.6, .6, 11, ORANGE, True)
        elif kind == "prevention":
            for n, layer in enumerate(s["layers"]):
                x = 55 + (n % 3) * 295; y = 250 - (n // 3) * 125
                panel(c, x, y, 255, 95, PANEL2 if n in (4, 5) else PANEL)
                text_pdf(c, layer[0], x + 16, y + 75, 35, 12, MINT if n != 4 else ORANGE, True)
                text_pdf(c, layer[1], x + 60, y + 75, 175, 10, TEXT, True)
                text_pdf(c, layer[2], x + 16, y + 42, 220, 10, MUTED)
            text_pdf(c, "Каждый слой оставляет свидетельство, а не только зелёный статус.", 55, 105, 850, 13, ORANGE, True)
            for n, layer in enumerate(s["layers"]):
                x = .75 + (n % 3) * 4.05; y = 2.45 + (n // 3) * 1.85
                pcard(slide, x, y, 3.65, 1.55, layer[0], layer[1], layer[2], ORANGE if n in (4, 5) else MINT)
            ptext(slide, "Каждый слой оставляет свидетельство, а не только зелёный статус.", .85, 6.38, 11.5, .3, 11, ORANGE, True)
        elif kind == "proof":
            for n, metric in enumerate(s["metrics"]):
                x = 50 + n * 270; panel(c, x, 295, 245, 85, PANEL2)
                text_pdf(c, metric[0], x + 16, 358, 85, 27, MINT, True)
                text_pdf(c, metric[1], x + 105, 354, 120, 12, TEXT, True)
                text_pdf(c, metric[2], x + 16, 320, 215, 8.5, MUTED)
            flow_x = [55, 165, 275, 385, 495]
            for n, item in enumerate(s["flow"]):
                panel(c, flow_x[n], 165, 95, 47, PANEL)
                text_pdf(c, item, flow_x[n] + 7, 195, 81, 8, TEXT, True)
                if n < 4:
                    c.setStrokeColor(colors.HexColor("#" + MINT)); c.line(flow_x[n] + 95, 187, flow_x[n] + 110, 187)
            panel(c, 680, 90, 230, 185, PANEL2, MINT)
            text_pdf(c, "✓", 745, 239, 120, 67, MINT, True)
            text_pdf(c, "ACK принят", 707, 162, 180, 21, TEXT, True)
            text_pdf(c, "результат в ERP-эмуляторе", 705, 130, 180, 9, MUTED)
            for n, metric in enumerate(s["metrics"]):
                pcard(slide, .7 + n * 4.05, 2.35, 3.65, 1.5, metric[0], metric[1], metric[2])
            for n, item in enumerate(s["flow"]):
                ptext(slide, item, .6 + n * 1.53, 5.05, 1.4, .5, 9, TEXT, True, PP_ALIGN.CENTER)
                if n < 4:
                    ptext(slide, "→", 1.92 + n * 1.53, 5.05, .3, .4, 14, MINT, True, PP_ALIGN.CENTER)
            shape(slide, "ack", 9.1, 4.55, 3.2, 1.45, PANEL2, MINT)
            ptext(slide, "✓", 9.25, 4.65, .7, .8, 40, MINT, True)
            ptext(slide, "ACK принят", 10.0, 4.92, 2.2, .45, 19, TEXT, True)
            ptext(slide, "результат в ERP-эмуляторе", 9.3, 5.48, 2.8, .28, 9, MUTED)
        elif kind == "close":
            text_pdf(c, s["lines"][0], 75, 335, 810, 25, MINT, True)
            text_pdf(c, s["lines"][1], 75, 270, 810, 25, ORANGE, True)
            text_pdf(c, s["lines"][2], 75, 205, 810, 25, TEXT, True)
            c.setStrokeColor(colors.HexColor("#" + MINT)); c.setLineWidth(2); c.line(75, 145, 885, 145)
            text_pdf(c, s["last"], 75, 115, 810, 16, MINT, True)
            ptext(slide, s["lines"][0], 1.0, 2.6, 11.3, .5, 22, MINT, True)
            ptext(slide, s["lines"][1], 1.0, 3.35, 11.3, .5, 22, ORANGE, True)
            ptext(slide, s["lines"][2], 1.0, 4.1, 11.3, .5, 22, TEXT, True)
            ptext(slide, s["last"], 1.0, 5.35, 11.3, .45, 15, MINT, True)

        source_notes = {
            "case_crs7": "Источник: NASA Independent Review Team, CRS-7 Public Summary, 2018",
            "case_cert": "Источник: NASA Release 19-036, OCO / Glory, 2019",
            "case_ru": "Источники: Роскосмос / TASS, 2017; FAA, Starship Mishap, 2023",
            "compare": "Источники: 1С:ERP / production; Octave Reliance / QMS. Детали — в space-quality-research.md",
        }
        if kind in source_notes:
            text_pdf(c, source_notes[kind], 48, 53, 860, 8, MUTED)
            ptext(slide, source_notes[kind], .65, 6.77, 11.9, .2, 8, MUTED)
        if kind != "blindspot":
            pdf_footer(c, index, total)
            ptext(slide, f"EFQA  /  Evidence-First Quality Architecture                                      {index:02d}/{total:02d}", .65, 7.12, 12, .18, 8, MUTED)
        slide.notes_slide.notes_text_frame.text = s["notes"]
        end = elapsed + s["seconds"]
        notes += [f"## {index}. {s['title']} — {elapsed // 60}:{elapsed % 60:02d}–{end // 60}:{end % 60:02d}", "", s["notes"], ""]
        elapsed = end
        c.showPage()
    c.save(); prs.save(OUT / "presentation.pptx")
    (OUT / "speaker-notes.md").write_text("\n".join(notes), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slides-only", action="store_true", help="Build the presentation without rebuilding the theory archive")
    args = parser.parse_args()
    if not args.slides_only:
        make_book()
    make_slides()
    print("Built presentation.pdf, presentation.pptx and speaker-notes.md")
