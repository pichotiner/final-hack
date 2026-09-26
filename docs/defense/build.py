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
            # Keep visible URLs as well as their human-readable source labels.
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
             Paragraph("26 сентября 2026. Срез main@53c6f7b. Сначала читайте раздел 1. "
                       "С раздела 8 начинается архив: его старые планы и статусы не являются "
                       "текущими обещаниями. Программные результаты P6 опубликованы ролью №1. "
                       "Раннее предупреждение EW-01 — проект. Полного промышленного внедрения нет.", BODY),
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
    manifest = {"baseline": "53c6f7b", "source_count": len(sources), "sources": [
        {"path": str(p.relative_to(ROOT)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sources]}
    (OUT / "sources-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_slides():
    slides = json.loads((OUT / "slides.json").read_text(encoding="utf-8"))
    assert sum(s["seconds"] for s in slides) == 495
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    c = canvas.Canvas(str(OUT / "presentation.pdf"), pagesize=(960, 540))
    c.setTitle("EFQA — доказательный контроль качества")
    notes = ["# Выступление на 8 минут", "", ("12 слайдов, 495 секунд. Расширенная версия включает сравнение, "
             "открытые расследования NASA/SpaceX/Роскосмоса и малую оценочную статистику. "
             "Все отраслевые числа сопровождаются знаменателем и ограничениями."), ""]
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
        layout = s.get("layout", "bullets")
        dark = layout == "hero"
        c.setFillColor(colors.HexColor("#0E2238" if dark else "#F7FAFC"))
        c.rect(0, 0, 960, 540, fill=1, stroke=0)
        if not dark:
            c.setFillColor(colors.HexColor("#176B95"))
            c.rect(0, 528, 960, 12, fill=1, stroke=0)
        title_color = "FFFFFF" if dark else NAVY
        tag_color = "7DE0DD" if dark else BLUE
        text_pdf(c, s["tag"], 42, 504, 865, 12, tag_color, True)
        text_pdf(c, s["title"], 42, 469, 865, 25, title_color, True)

        def pdf_card(x, y, w, h, kicker, title, body, accent=BLUE, dark_card=False):
            c.setFillColor(colors.HexColor("#173A55" if dark_card else "#FFFFFF"))
            c.setStrokeColor(colors.HexColor("#2A607D" if dark_card else "#D9E5EC"))
            c.roundRect(x, y, w, h, 10, fill=1, stroke=1)
            c.setFillColor(colors.HexColor("#7DE0DD" if dark_card else "#176B95"))
            c.roundRect(x, y + h - 7, w, 7, 3, fill=1, stroke=0)
            text_pdf(c, kicker, x + 14, y + h - 19, w - 28, 11, "7DE0DD" if dark_card else BLUE, True)
            text_pdf(c, title, x + 14, y + h - 44, w - 28, 15, "FFFFFF" if dark_card else NAVY, True)
            text_pdf(c, body, x + 14, y + h - 77, w - 28, 10.5, "D9EDF2" if dark_card else "365268")

        def pdf_footer():
            c.setFillColor(colors.HexColor("#6B8292" if not dark else "#9CC5D2"))
            c.setFont("DV", 8)
            c.drawString(42, 24, "EFQA • синтетический демонстратор • отраслевые кейсы — открытые источники")
            c.drawRightString(916, 24, f"{index}/12")

        if layout == "hero":
            text_pdf(c, s["subtitle"], 42, 340, 700, 23, "EAF7F7", True)
            x = 42
            for chip in s["chips"]:
                width = 28 + len(chip) * 7.2
                c.setFillColor(colors.HexColor("#1B4D68"))
                c.setStrokeColor(colors.HexColor("#4FB8B9"))
                c.roundRect(x, 115, width, 30, 15, fill=1, stroke=1)
                text_pdf(c, chip, x + 14, 135, width - 28, 10, "B9F3F0", True)
                x += width + 12
            text_pdf(c, "Evidence-First Quality Architecture", 42, 78, 700, 13, "7DE0DD", True)
            box(s["tag"], .5, .35, 12.2, .4, 12, color="7DE0DD")
            box(s["title"], .5, .85, 12.2, 1.25, 25, True, color="FFFFFF")
            box(s["subtitle"], .5, 2.55, 10.2, 1.5, 20, True, color="EAF7F7")
        elif layout == "cards":
            cards = s["cards"]
            for n, card in enumerate(cards):
                x = 42 + (n % 2) * 443
                y = 255 - (n // 2) * 145
                pdf_card(x, y, 410, 118, card[0], card[1], card[2])
                sx = .6 + (n % 2) * 6.1
                sy = 2.55 + (n // 2) * 2.0
                shape = slide.shapes.add_shape(5, Inches(sx), Inches(sy), Inches(5.7), Inches(1.55))
                shape.fill.solid(); shape.fill.fore_color.rgb = RGBColor.from_string("FFFFFF")
                shape.line.color.rgb = RGBColor.from_string("D9E5EC")
                box(f"{card[0]}  {card[1]}\n{card[2]}", sx + .15, sy + .15, 5.4, 1.2, 13, color=NAVY)
        elif layout == "pipeline":
            stages = s["stages"]
            for n, stage in enumerate(stages):
                x = 42 + n * 181
                pdf_card(x, 225, 155, 128, stage[0], stage[1], stage[2])
                if n < len(stages) - 1:
                    c.setStrokeColor(colors.HexColor("#36A4A6")); c.setLineWidth(2)
                    c.line(x + 155, 289, x + 176, 289)
                sh = slide.shapes.add_shape(5, Inches(.55 + n * 2.5), Inches(3.05), Inches(2.15), Inches(1.25))
                sh.fill.solid(); sh.fill.fore_color.rgb = RGBColor.from_string("EAF4F8")
                box(f"{stage[0]}\n{stage[1]}\n{stage[2]}", .65 + n * 2.5, 3.15, 1.95, 1.0, 11, color=NAVY)
            c.setFillColor(colors.HexColor("#E1F6F4")); c.roundRect(85, 105, 790, 55, 10, fill=1, stroke=0)
            text_pdf(c, s["callout"], 105, 142, 750, 12, NAVY, True)
            box(s["callout"], .9, 5.7, 11.6, .55, 12, True, color=NAVY)
        elif layout == "compare":
            x0, y0, widths = 42, 125, [145, 205, 235, 290]
            x = x0
            for col, w in zip(s["columns"], widths):
                c.setFillColor(colors.HexColor("#176B95")); c.rect(x, 375, w, 34, fill=1, stroke=0)
                text_pdf(c, col, x + 8, 398, w - 16, 10, "FFFFFF", True)
                x += w
            for r, row in enumerate(s["rows"]):
                y = 335 - r * 51
                x = x0
                fill = "EAF4F8" if r % 2 == 0 else "FFFFFF"
                for cell, w in zip(row, widths):
                    c.setFillColor(colors.HexColor("#" + fill)); c.rect(x, y, w, 47, fill=1, stroke=0)
                    text_pdf(c, cell, x + 7, y + 38, w - 14, 8.5 if len(cell) > 55 else 9.5, NAVY if r == 3 else "365268", r == 3)
                    x += w
            text_pdf(c, s["footnote"], 42, 105, 865, 9, "567086")
            for r, row in enumerate(s["rows"]):
                sx = .6; sy = 2.55 + r * .55
                box(" | ".join(row), sx, sy, 12.1, .5, 9, color=NAVY)
        elif layout == "metrics":
            for n, metric in enumerate(s["metrics"]):
                x = 42 + (n % 2) * 265; y = 320 - (n // 2) * 105
                pdf_card(x, y, 240, 80, metric[0], metric[1], metric[2])
                sx = .55 + (n % 2) * 3.55; sy = 2.55 + (n // 2) * 1.35
                sh = slide.shapes.add_shape(5, Inches(sx), Inches(sy), Inches(3.25), Inches(.88))
                sh.fill.solid(); sh.fill.fore_color.rgb = RGBColor.from_string("FFFFFF")
                box(f"{metric[0]}  {metric[1]}\n{metric[2]}", sx + .1, sy + .1, 3.05, .65, 10, color=NAVY)
            image = ROOT / s["image"]
            c.setFillColor(colors.HexColor("#FFFFFF")); c.roundRect(590, 105, 317, 250, 10, fill=1, stroke=0)
            c.drawImage(ImageReader(str(image)), 606, 125, width=285, height=210, preserveAspectRatio=True, anchor="c", mask="auto")
            slide.shapes.add_picture(str(image), Inches(8.35), Inches(2.2), width=Inches(4.3))
        elif layout == "cases":
            for n, case in enumerate(s["cases"]):
                x = 42 + n * 443
                pdf_card(x, 122, 410, 245, case[0], case[1], case[2] + "\n\n→ " + case[3], accent="36A4A6")
                sh = slide.shapes.add_shape(5, Inches(.6 + n * 6.15), Inches(2.3), Inches(5.7), Inches(3.2))
                sh.fill.solid(); sh.fill.fore_color.rgb = RGBColor.from_string("FFFFFF")
                box(f"{case[0]}\n{case[1]}\n\n{case[2]}\n\n→ {case[3]}", .75 + n * 6.15, 2.48, 5.4, 2.75, 11, color=NAVY)
        elif layout == "bars":
            max_value = max(row[1] for row in s["bars"])
            for n, row in enumerate(s["bars"]):
                y = 335 - n * 54
                text_pdf(c, row[0], 42, y + 27, 280, 10, NAVY, True)
                c.setFillColor(colors.HexColor("#DDEBF1")); c.roundRect(325, y, 410, 25, 12, fill=1, stroke=0)
                c.setFillColor(colors.HexColor("#1D8D9B")); c.roundRect(325, y, 410 * row[1] / max_value, 25, 12, fill=1, stroke=0)
                text_pdf(c, f"{row[1]} / 5", 750, y + 22, 70, 12, NAVY, True)
                text_pdf(c, row[2], 825, y + 21, 80, 8, "567086")
            c.setFillColor(colors.HexColor("#FFF4DC")); c.roundRect(42, 95, 865, 56, 10, fill=1, stroke=0)
            text_pdf(c, s["statnote"], 60, 132, 830, 10, "805A14", True)
            box(s["statnote"], .8, 5.65, 11.7, .65, 11, True, color="805A14")
        elif layout == "prevention":
            widths = [130, 250, 485]; x0 = 42
            for i, head in enumerate(["Этап", "Событие риска", "Профилактика"]):
                x = x0 + sum(widths[:i]); c.setFillColor(colors.HexColor("#176B95")); c.rect(x, 390, widths[i], 30, fill=1, stroke=0)
                text_pdf(c, head, x + 8, 410, widths[i] - 16, 10, "FFFFFF", True)
            for r, row in enumerate(s["rows"]):
                y = 350 - r * 42; x = x0; fill = "FFFFFF" if r % 2 else "EAF4F8"
                for cell, w in zip(row, widths):
                    c.setFillColor(colors.HexColor("#" + fill)); c.rect(x, y, w, 38, fill=1, stroke=0)
                    text_pdf(c, cell, x + 8, y + 29, w - 16, 8.5 if len(cell) > 50 else 9.5, NAVY, r == 0)
                    x += w
            for r, row in enumerate(s["rows"]):
                box(" | ".join(row), .6, 2.2 + r * .48, 12.1, .4, 9, color=NAVY)
        elif layout == "roadmap":
            for n, step in enumerate(s["steps"]):
                x = 42 + n * 220
                c.setFillColor(colors.HexColor("#176B95")); c.circle(x + 22, 315, 21, fill=1, stroke=0)
                text_pdf(c, step[0], x + 12, 322, 20, 14, "FFFFFF", True)
                pdf_card(x, 175, 190, 105, step[0], step[1], step[2])
                if n < 3:
                    c.setStrokeColor(colors.HexColor("#36A4A6")); c.setLineWidth(2); c.line(x + 45, 315, x + 220, 315)
                sh = slide.shapes.add_shape(5, Inches(.6 + n * 3.05), Inches(2.9), Inches(2.65), Inches(1.55))
                sh.fill.solid(); sh.fill.fore_color.rgb = RGBColor.from_string("FFFFFF")
                box(f"{step[0]}  {step[1]}\n{step[2]}", .72 + n * 3.05, 3.1, 2.4, 1.1, 11, color=NAVY)
            text_pdf(c, s["callout"], 42, 130, 865, 11, BLUE, True)
            box(s["callout"], .8, 5.6, 11.8, .65, 11, True, color=BLUE)
        elif layout == "closing":
            for n, bullet in enumerate(s["bullets"]):
                y = 345 - n * 62
                c.setFillColor(colors.HexColor("#1D8D9B")); c.circle(57, y - 5, 7, fill=1, stroke=0)
                text_pdf(c, bullet, 78, y + 5, 820, 13 if n < 2 else 10, NAVY, n < 2)
            box("\n".join("• " + b for b in s["bullets"]), .6, 2.35, 12, 3.2, 15, color=NAVY)
        else:
            text_pdf(c, "\n\n".join("• " + t for t in s.get("bullets", [])), 42, 357, 860, 18)
            box("\n".join("• " + t for t in s.get("bullets", [])), .6, 2.3, 12.1, 3.1, 18)

        pdf_footer()
        box(f"EFQA • синтетический стенд / источники отраслевых кейсов                                      {index}/12", .5, 7, 12.2, .3, 9, color="6B8292")
        slide.notes_slide.notes_text_frame.text = s["notes"]
        end = elapsed + s["seconds"]
        notes += [f"## {index}. {s['title']} — {elapsed // 60}:{elapsed % 60:02d}–{end // 60}:{end % 60:02d}", "", s["notes"], ""]
        elapsed = end
        c.showPage()
    c.save()
    prs.save(OUT / "presentation.pptx")
    (OUT / "speaker-notes.md").write_text("\n".join(notes), encoding="utf-8")


if __name__ == "__main__":
    make_book()
    make_slides()
    print("Built theory.pdf, presentation.pdf/.pptx, speaker-notes.md, sources-manifest.json")
