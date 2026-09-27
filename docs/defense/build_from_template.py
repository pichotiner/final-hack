"""Adapt the actual SlidesCarnival Dark Modern Tech Startup PPTX for EFQA.

The template's original freeforms, image-backed lighting and master/theme are
retained. New Russian content is placed into selected original compositions.
Export the result with export_template.ps1, using Microsoft PowerPoint.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import urllib.request

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from PIL import Image, ImageEnhance, ImageOps
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Pt
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as MetricFont

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
WORK = ROOT / ".local" / "template-work"
TEMPLATE = ROOT / ".local" / "dark-modern-tech-startup-original.pptx"
TEMPLATE_URL = "https://www.slidescarnival.com/download/sc01527/tech-startup-brand/pptx"
PAGE_URL = "https://www.slidescarnival.com/template/tech-startup-brand/154090"
TEMPLATE_SHA256 = "6dc07658f10a2895d7fc407dc94015fe8dc7307e3d4018a61ecd5b7b2ec0e1cb"
LIME, WHITE, GRAY, BG = "C1FF72", "F0F0F0", "D4D4D4", "222222"
FONT = "EFQA Oswald"
BODY = "Arial"
SOURCES = {
    "nasa_irt": "https://www.nasa.gov/wp-content/uploads/2018/03/public_summary_nasa_irt_spacex_crs-7_final.pdf",
    "nasa_oco": "https://www.nasa.gov/news-release/nasa-investigation-uncovers-cause-of-two-science-mission-launch-failures/",
    "progress": "https://tass.com/science/924519",
    "faa": "https://www.faa.gov/newsroom/faa-closes-spacex-starship-mishap-investigation",
    "erp": "https://v8.1c.ru/erp/production/",
    "qms": "https://www.octave.com/products/asset-performance-management/reliance",
    "photo": "https://www.nasa.gov/wp-content/uploads/2019/04/2011-1459.jpg",
    "font": "https://raw.githubusercontent.com/google/fonts/main/ofl/oswald/Oswald%5Bwght%5D.ttf",
    "font_license": "https://raw.githubusercontent.com/google/fonts/main/ofl/oswald/OFL.txt",
}


def fetch(url, dest):
    if not dest.exists():
        request = urllib.request.Request(url, headers={"User-Agent": "EFQA-Presentation/1.0"})
        with urllib.request.urlopen(request, timeout=90) as response:
            dest.write_bytes(response.read())
    return dest


def prepare_assets():
    WORK.mkdir(parents=True, exist_ok=True)
    if not TEMPLATE.exists():
        fetch(TEMPLATE_URL, TEMPLATE)
    if hashlib.sha256(TEMPLATE.read_bytes()).hexdigest() != TEMPLATE_SHA256:
        raise ValueError("Template changed. Review its actual layouts before updating this adapter.")
    variable = fetch(SOURCES["font"], WORK / "Oswald-variable.ttf")
    fetch(SOURCES["font_license"], WORK / "Oswald-OFL.txt")
    font_path = WORK / "EFQA-Oswald.ttf"
    if not font_path.exists():
        font = instantiateVariableFont(TTFont(variable), {"wght": 600}, inplace=False)
        # Use a distinct family name so the original embedded Latin subset cannot
        # shadow this full Cyrillic font. The renamed derivative remains OFL.
        names = {1: FONT, 2: "Regular", 3: "EFQA-Oswald-Semibold-v1", 4: FONT, 6: "EFQAOswald", 16: FONT, 17: "Regular"}
        table = font["name"]
        table.names = [n for n in table.names if n.nameID not in names]
        for key, value in names.items():
            table.setName(value, key, 3, 1, 0x409)
            table.setName(value, key, 1, 0, 0)
        font["OS/2"].fsType = 0
        font.save(font_path)
    pdfmetrics.registerFont(MetricFont("headline", str(font_path)))
    pdfmetrics.registerFont(MetricFont("body", "C:/Windows/Fonts/arial.ttf"))
    pdfmetrics.registerFont(MetricFont("body-bold", "C:/Windows/Fonts/arialbd.ttf"))
    original = fetch(SOURCES["photo"], WORK / "glory-nasa-original.jpg")
    image = ImageOps.grayscale(Image.open(original))
    image = ImageEnhance.Contrast(image).enhance(1.08)
    image.save(WORK / "glory-mono.jpg", quality=94)
    # Crop the existing P6 evidence capture; its historic origin is labelled on slide.
    screen = Image.open(ROOT / "docs/demo/a04-ack-basis.png")
    screen.crop((210, 80, 1230, 460)).save(WORK / "basis-ui-crop.png")


def clone_slide(prs, source):
    target = prs.slides.add_slide(source.slide_layout)
    for shape in list(target.shapes):
        shape._element.getparent().remove(shape._element)
    mapping = {}
    for rel in source.part.rels.values():
        if rel.reltype.endswith(("/slideLayout", "/notesSlide")):
            continue
        mapping[rel.rId] = (target.part.rels.get_or_add_ext(rel.reltype, rel.target_ref)
                           if rel.is_external else target.part.rels.get_or_add(rel.reltype, rel.target_part))
    ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    for shape in source.shapes:
        element = deepcopy(shape._element)
        for node in element.iter():
            for key, value in list(node.attrib.items()):
                if key.startswith(ns) and value in mapping:
                    node.set(key, mapping[value])
        target.shapes._spTree.insert_element_before(element, "p:extLst")
    bg = source._element.cSld.find("{http://schemas.openxmlformats.org/presentationml/2006/main}bg")
    if bg is not None:
        target._element.cSld.insert(0, deepcopy(bg))
    return target


def get(slide, shape_id):
    return next(s for s in slide.shapes if s.shape_id == shape_id)


def remove(slide, *ids):
    for shape in list(slide.shapes):
        if shape.shape_id in ids:
            shape._element.getparent().remove(shape._element)


def box(shape, x, y, w, h):
    shape.left, shape.top, shape.width, shape.height = (Pt(v) for v in (x, y, w, h))
    shape.rotation = 0


def text(slide, value, x, y, w, h, size=30, color=WHITE, headline=False,
         bold=False, align="left", target=None, rotation=0, url=None):
    shape = get(slide, target) if target else slide.shapes.add_textbox(Pt(x), Pt(y), Pt(w), Pt(h))
    box(shape, x, y, w, h)
    shape.rotation = rotation
    tf = shape.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = Pt(0)
    tf._txBody.bodyPr.set("rot", "0")
    tf._txBody.bodyPr.set("vert", "horz")
    tf._txBody.bodyPr.set("numCol", "1")
    metric = "headline" if headline else ("body-bold" if bold else "body")
    leading = size * (1.20 if headline else 1.24)
    lines = 0
    for n, line in enumerate(value.split("\n")):
        p = tf.paragraphs[0] if n == 0 else tf.add_paragraph()
        p.alignment = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}[align]
        p.line_spacing = Pt(leading)
        p.space_before = p.space_after = Pt(0)
        run = p.add_run()
        run.text = line
        run.font.name = FONT if headline else BODY
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = RGBColor.from_string(color)
        run.font._rPr.set("lang", "ru-RU")
        if url:
            run.hyperlink.address = url
        current = ""
        for word in line.split():
            if pdfmetrics.stringWidth(word, metric, size) > w:
                raise ValueError(f"Word wider than its box: {word}")
            candidate = f"{current} {word}".strip()
            if pdfmetrics.stringWidth(candidate, metric, size) > w - 8 and current:
                lines += 1
                current = word
            else:
                current = candidate
        lines += 1
    if lines * leading > h + 3:
        raise ValueError(f"Text does not fit ({lines * leading:.0f}>{h}): {value}")
    shape.name = "EFQA / " + value.split("\n")[0][:65]
    return shape


def rect(slide, x, y, w, h, fill, stroke=None, radius=0):
    sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE, Pt(x), Pt(y), Pt(w), Pt(h))
    sh.fill.solid()
    sh.fill.fore_color.rgb = RGBColor.from_string(fill)
    if radius:
        sh.adjustments[0] = min(radius / min(w, h), .49)
    if stroke:
        sh.line.color.rgb = RGBColor.from_string(stroke)
    else:
        sh.line.fill.background()
    sh._element.spPr.append(OxmlElement("a:effectLst"))
    return sh


def line(slide, x1, y1, x2, y2, color=LIME, width=2):
    sh = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Pt(x1), Pt(y1), Pt(x2), Pt(y2))
    sh.line.color.rgb = RGBColor.from_string(color)
    sh.line.width = Pt(width)
    sh._element.spPr.append(OxmlElement("a:effectLst"))
    return sh


def photo(slide, path, x, y, w, h):
    image = ImageOps.fit(Image.open(path).convert("RGB"), (round(w * 1.6), round(h * 1.6)), method=Image.Resampling.LANCZOS)
    dest = WORK / f"photo-{len(list(WORK.glob('photo-*.jpg')))}.jpg"
    image.save(dest, quality=94)
    return slide.shapes.add_picture(str(dest), Pt(x), Pt(y), Pt(w), Pt(h))


def footer(slide, n, source="EFQA · демонстратор P8 · синтетические данные"):
    text(slide, source, 62, 771, 1240, 24, 16, "B8B8B8")
    text(slide, f"{n:02d} / 16", 1310, 771, 90, 24, 16, GRAY, align="right")


def build():
    prepare_assets()
    prs = Presentation(TEMPLATE)
    originals = list(prs.slides)
    # Deliberately use the source compositions, including the template's credits.
    mapping = [1, 5, 6, 9, 19, 20, 18, 9, 17, 21, 13, 14, 2, 1, 19, 23]
    slides = [clone_slide(prs, originals[i - 1]) for i in mapping]
    for item in list(prs.slides._sldIdLst)[:len(originals)]:
        prs.part.drop_rel(item.rId)
        prs.slides._sldIdLst.remove(item)
    all_notes = []

    def notes(i, title, speech, seconds, urls=()):
        all_notes.append({"slide": i, "template_slide": mapping[i - 1], "title": title, "seconds": seconds, "speech": speech, "sources": list(urls)})
        slides[i - 1].notes_slide.notes_text_frame.text = speech + "\n\nИсточники:\n" + "\n".join(urls)

    # 01 — Original title plate and inset ribbon.
    s = slides[0]
    remove(s, 137)
    text(s, "EFQA", 270, 204, 900, 168, 138, LIME, True, align="center")
    text(s, "КАЧЕСТВО, У КОТОРОГО\nЕСТЬ ДОКАЗАТЕЛЬСТВА", 200, 389, 1040, 188, 66, WHITE, True, align="center")
    text(s, "КОСМОХАКАТОН / 2026", 78, 82, 450, 43, 27, GRAY, target=139)
    text(s, "От сигнала до решения ОТК", 790, 688, 560, 43, 28, GRAY, target=138, align="center")
    notes(1, "EFQA: качество с доказательствами", "EFQA связывает сигналы контроля, производственные операции и решения ОТК. Мы показываем не только последний статус детали, но и основания, по которым этот статус появился. Данные демонстратора синтетические, анализатор заменён эмулятором.", 25)

    # 02 — Original quotation layout becomes the product's central thesis.
    s = slides[1]
    remove(s, 180)
    text(s, "ГДЕ НАШЛИ", 260, 182, 920, 104, 78, GRAY, True, align="center")
    text(s, "≠", 560, 294, 320, 106, 84, LIME, bold=True, align="center")
    text(s, "ГДЕ ВОЗНИКЛО", 240, 411, 960, 104, 78, LIME, True, align="center", target=181)
    text(s, "Найдено после сборки — ещё не значит «виноват сборщик».", 270, 574, 900, 77, 31, GRAY, align="center", target=182)
    notes(2, "Обнаружение не равно происхождению", "Признак найден на финальном контроле. Но он мог существовать во входной детали или появиться при обработке. Последовательность событий не устанавливает физическую причину и не доказывает вину оператора. Для вывода нужны сопоставимые осмотры и контекст операций.", 30)

    # 03 — Original asymmetrical three-word composition.
    s = slides[2]
    text(s, "ТРИ УРОВНЯ\nУВЕРЕННОСТИ", 100, 90, 410, 218, 62, LIME, True, target=190)
    text(s, "Анализатор сообщает.\nСистема связывает.\nКонтролёр решает.", 106, 368, 354, 161, 30, GRAY, target=189)
    for sid, value, y, accent in [(191, "СИГНАЛ", 226, GRAY), (192, "ГИПОТЕЗА", 407, LIME), (193, "РЕШЕНИЕ", 588, GRAY)]:
        text(s, value, 660, y, 680, 120, 92, accent, True, target=sid)
    footer(s, 3)
    notes(3, "Сигнал → гипотеза → решение", "Подозрительный след — наблюдение. Возможная связь со станком — гипотеза. Подтверждение дефекта и выпуск изделия — отдельные решения уполномоченного контролёра. Несколько наблюдений одной проблемы не должны умножать число дефектов.", 35)

    # 04 — Original dual circular medallions and angled right-hand composition.
    s = slides[3]
    remove(s, 238, 241, 242)
    text(s, "ТОГДА / СЕЙЧАС", 50, 48, 885, 110, 70, WHITE, True, target=243, align="center")
    for sid, value, x in [(244, "10:12 · РЕШЕНИЕ", 92), (245, "10:20 · НОВЫЙ ФАКТ", 537)]:
        text(s, value, x, 250, 336, 42, 26, LIME, bold=True, target=sid, align="center")
    text(s, "B₁", 155, 369, 170, 117, 92, GRAY, True, align="center")
    text(s, "B₂", 615, 369, 170, 117, 92, LIME, True, align="center")
    text(s, "Основания решения\nсохранены", 65, 616, 364, 88, 30, GRAY, target=246, align="center")
    text(s, "Поздний входной сигнал\nменяет картину", 520, 616, 364, 88, 30, GRAY, target=247, align="center")
    text(s, "ИСТОРИЯ\nНЕ СТИРАЕТСЯ", 989, 249, 375, 168, 56, LIME, True)
    text(s, "Новый факт требует\nпересмотра.\nСтарое решение\nостаётся доступным.", 990, 460, 366, 186, 30, GRAY)
    footer(s, 4, "B — зафиксированный набор оснований · условный пример")
    notes(4, "Основания тогда и сейчас", "Basis B — сохранённый набор событий, версий правил и известных пробелов перед решением. Если позже поступает входной осмотр, он может изменить гипотезу. EFQA сохраняет старое B, показывает текущую картину и необходимость пересмотра. Полный межиздельный impact по составу — отдельный эксперимент, не промышленная функция.", 40)

    # 05 — Use the template's four-column competitive layout, not a new card grid.
    s = slides[4]
    columns = [
        ("1С:ERP", "МАРШРУТ", "Заказы, операции\nи серийный учёт.\nПроизводственный\nконтекст."),
        ("OCTAVE RELIANCE", "QMS", "Несоответствия,\nCAPA и аудит.\nЗрелая система\nуправления качеством."),
        ("VISION STATION", "СИГНАЛ", "Визуальные признаки\nна точке контроля.\nНужна история\nконкретной детали."),
        ("EFQA / P8", "ОСНОВАНИЯ", "B «тогда/сейчас».\nПоздние данные.\nПолнота контроля.\nПересмотр решения."),
    ]
    for i, (head, big, body) in enumerate(columns):
        x = 111 + i * 317
        for sid in ([395, 394, 396], [401, 400, 402], [404, 403, 405], [398, 397, 399])[i]:
            remove(s, sid)
        text(s, head, x, 155, 275, 38, 24, GRAY, bold=True, align="center")
        text(s, big, x - 2, 224, 279, 80, 48 if i == 3 else 58, LIME, True, align="center")
        text(s, body, x - 3, 333, 287, 140, 25, GRAY, align="center")
    text(s, "ГДЕ ПОЛЕЗНА EFQA", 90, 593, 1260, 116, 80, WHITE, True, target=393, align="center")
    text(s, "Преимущество для кейса проверяем одним сценарием; функции чужих систем не считаем отсутствующими.", 99, 721, 1242, 41, 22, GRAY, align="center")
    footer(s, 5, "Источники: 1С:ERP / production · Octave Reliance / QMS")
    notes(5, "Сильные альтернативы", "1С:ERP умеет производственный и серийный учёт. Octave Reliance, ранее ETQ, поддерживает CAPA, аудит и управление несоответствиями. Это сильные альтернативы. Преимущество EFQA для данного кейса — готовый демонстрационный сценарий B, неизвестности и пересмотра. Мы не измеряли стоимость конкурентов и не утверждаем, что они не способны реализовать такой сценарий.", 40, [SOURCES["erp"], SOURCES["qms"]])

    # 06 — Keep the original editorial/photo layout, replace the VR photograph.
    s = slides[5]
    remove(s, 413)
    photo(s, WORK / "glory-mono.jpg", 625, 285, 815, 450)
    text(s, "OCO + GLORY", 200, 75, 1040, 120, 90, WHITE, True, target=415, align="center")
    text(s, "> $700M", 90, 286, 500, 140, 100, LIME, True, target=416)
    text(s, "Потери двух миссий\nпо оценке NASA", 96, 431, 480, 91, 31, GRAY, target=417)
    text(s, "Материал не соответствовал\nтребованиям. Результаты\nиспытаний были изменены.", 96, 558, 477, 137, 28, GRAY, target=418)
    text(s, "Сертификат требует проверяемого происхождения.", 625, 728, 766, 37, 24, LIME)
    footer(s, 6, "NASA Release 19-036 · 30.04.2019 · фото: NASA, подготовка Glory")
    notes(6, "OCO и Glory: доверие к поставке", "Это два запуска Taurus XL компании Orbital Sciences с миссиями NASA, 2009 и 2011 годов. NASA связала техническую первопричину с материалами Sapa Profiles и изменёнными результатами испытаний алюминиевых профилей узла разделения. Более 700 миллионов долларов — оценка потерь NASA. Нужны независимые испытания и прослеживаемость поставки; неизменяемость записи не доказывает правдивость изначально ложного сертификата.", 45, [SOURCES["nasa_oco"], SOURCES["photo"]])

    # 07 — Original 'about technology' split, with a large case identifier.
    s = slides[6]
    remove(s, 381)
    text(s, "CRS-7", 75, 300, 580, 170, 136, LIME, True, align="center")
    text(s, "SPACEX · 28.06.2015", 81, 508, 560, 50, 33, GRAY, align="center")
    text(s, "ОДИН ОТКАЗ.\nНЕСКОЛЬКО ВЕРСИЙ.", 752, 54, 610, 197, 65, WHITE, True, target=379)
    for sid, number, y in [(382, "01", 319), (383, "02", 468), (384, "03", 617)]:
        text(s, number, 757, y, 84, 74, 55, LIME, True, target=sid)
    for sid, value, y in [
        (385, "Факт: разрыв кислородного бака.\nОтказ крепления COPV правдоподобен.", 327),
        (386, "Версии: материал, повреждение,\nмонтаж. Единственный инициатор\nпублично не установлен.", 476),
        (387, "Урок NASA: применимость детали\nк криогенным условиям требует\nдостаточной проверки.", 625),
    ]:
        text(s, value, 873, y, 492, 107, 27, GRAY, target=sid)
    footer(s, 7, "NASA IRT · CRS-7 Public Summary · 12.03.2018 · схематическое изложение")
    notes(7, "CRS-7: альтернативные инициаторы", "NASA IRT признала отказ крепления COPV правдоподобной последовательностью, но не выделила дефект материала как единственный наиболее вероятный инициатор. Рассматривались повреждение и монтаж. Отдельный инженерный вывод касался применения промышленной детали в критическом криогенном узле без достаточной проверки. EFQA сохраняет альтернативные оценки и их основания. Эти выводы не доказывают, что система предотвратила бы аварию.", 45, [SOURCES["nasa_irt"]])

    # 08 — Original two-medallion layout: cause uncertainty vs corrective actions.
    s = slides[7]
    remove(s, 238, 241, 242)
    text(s, "ПРИЧИНА И ДЕЙСТВИЕ", 50, 48, 885, 110, 63, WHITE, True, target=243, align="center")
    text(s, "ПРОГРЕСС МС-04", 64, 245, 357, 43, 28, LIME, bold=True, target=244, align="center")
    text(s, "STARSHIP / FAA", 522, 245, 357, 43, 28, LIME, bold=True, target=245, align="center")
    text(s, "?", 173, 347, 138, 143, 112, LIME, True, align="center")
    text(s, "63", 615, 347, 183, 143, 112, LIME, True, align="center")
    text(s, "Возможны частицы\nили нарушения сборки.\nИнициатор не определён.", 44, 617, 398, 119, 27, GRAY, target=246, align="center")
    text(s, "Корректирующих действия\nпо одному происшествию.\nЭто не 63 дефекта.", 506, 617, 393, 119, 27, GRAY, target=247, align="center")
    text(s, "РАЗНЫЕ\nСУЩНОСТИ", 989, 248, 375, 166, 59, WHITE, True)
    text(s, "Причина.\nМера профилактики.\nПроверка результата.", 984, 462, 375, 168, 31, LIME)
    footer(s, 8, "Роскосмос / ТАСС, 11.01.2017 · FAA, 08.09.2023 · события: 2016 / 2023")
    notes(8, "Прогресс МС-04 и Starship", "По Прогрессу МС-04 сообщение Роскосмоса, процитированное ТАСС, называло возможные частицы или нарушение сборки насоса, в том числе неправильные зазоры и дисбаланс. Для Starship 20 апреля 2023 FAA сообщила 63 корректирующих действия и несколько первопричин. Число действий не превращается в количество дефектов. Полный CAPA и межиздельный охват — направления развития EFQA, а не реализованный промышленный модуль.", 40, [SOURCES["progress"], SOURCES["faa"]])

    # 09 — Template's market/data composition with truthful, editable bars.
    s = slides[8]
    remove(s, 369, 370, 373, 374)
    text(s, "ГДЕ ИСКАТЬ РИСК", 70, 60, 822, 117, 74, WHITE, True, target=368)
    rows = [
        ("Материал / сертификаты", "OCO + Glory · подтверждённый вывод NASA", 2, "40%"),
        ("Изготовление / сборка", "CRS-7 + Прогресс · возможные инициаторы", 2, "40%"),
        ("Проектирование / проверка", "CRS-7 · применимость детали", 1, "20%"),
        ("Этап не разложен", "Starship · публичной детализации нет", 1, "20%"),
    ]
    for i, (name, detail, count, percent) in enumerate(rows):
        y = 281 + i * 108
        text(s, name, 123, y, 616, 38, 28, WHITE, bold=True)
        text(s, detail, 125, y + 37, 622, 30, 19, GRAY)
        rect(s, 125, y + 75, 570, 12, "454545", radius=5)
        rect(s, 125, y + 75, 570 * count / 5, 12, LIME, radius=5)
        text(s, f"{count}/5 · {percent}", 714, y + 43, 154, 43, 27, LIME, headline=True)
    text(s, "5", 1000, 165, 303, 191, 150, LIME, True, align="center")
    text(s, "СОБЫТИЙ\nВ ЦЕЛЕВОЙ ВЫБОРКЕ", 954, 363, 425, 85, 29, WHITE, bold=True, align="center", target=371)
    text(s, "Категории пересекаются.\nOCO и Glory связаны\nобщим поставщиком.\n\nЭто не частота брака\nпо всей отрасли.", 961, 491, 410, 223, 27, GRAY, target=372)
    footer(s, 9, "Единица учёта — событие · n = 5 · 2009–2023 · распределение упоминаний, включая гипотезы")
    notes(9, "Оценка этапов на открытых кейсах", "Это пять выбранных событий, а не независимая репрезентативная выборка. OCO и Glory связаны одним поставочным механизмом. Материалы и сертификаты подтверждены NASA в двух событиях. Изготовление и сборка фигурируют как возможные инициаторы в CRS-7 и Прогрессе, а не как две доказанные одинаковые причины. В CRS-7 также есть фактор проектирования и проверки применимости. Starship не раскладываем по производственным операциям. Доли 40, 40, 20, 20 процентов перекрываются и не являются отраслевой вероятностью брака.", 45, [SOURCES["nasa_oco"], SOURCES["nasa_irt"], SOURCES["progress"], SOURCES["faa"]])

    # 10 — Template's four illustrated actions / big editorial headline.
    s = slides[9]
    text(s, "ПРЕДОТВРАЩАТЬ\nНА КАЖДОМ\nЭТАПЕ", 818, 167, 551, 268, 65, WHITE, True, target=428)
    text(s, "Физические проверки +\nполная история качества", 862, 538, 491, 97, 32, LIME, target=429)
    actions = [
        (430, 167, 203, "ПОСТАВКА", "Независимые испытания.\nСвязь партии и детали."),
        (432, 466, 203, "КОНТРОЛЬ", "Метод под тип дефекта.\nСнимок не заменяет НК."),
        (431, 167, 471, "ПРОЦЕСС", "Режимы станка, чистота,\nзазоры и инструмент."),
        (433, 466, 471, "ВЫПУСК", "Полнота проверок.\nРешение на актуальном B."),
    ]
    for sid, x, y, head, body in actions:
        remove(s, sid)
        text(s, head, x, y, 267, 39, 25, LIME, bold=True, align="center")
        text(s, body, x - 10, y + 48, 292, 106, 25, GRAY, align="center")
    footer(s, 10, "Инженерные меры профилактики · реальные датчики, партии и предупреждения требуют пилота")
    notes(10, "Профилактика по этапам", "Система качества помогает организовать доказательства и не пропустить обязательную проверку. Физически предотвращают брак испытания материала, правильный метод неразрушающего контроля, чистота, защита от ошибочной сборки и контроль процесса. EFQA P8 поддерживает основания и полноту контроля; реальная телеметрия, независимые испытания и партии потребуют интеграции и пилота.", 40)

    # 11 — Keep original large circular product frame; show actual UI evidence.
    s = slides[10]
    remove(s, 314)
    rect(s, 161, 235, 918, 366, "131313", radius=24)
    s.shapes.add_picture(str(WORK / "basis-ui-crop.png"), Pt(181), Pt(254), width=Pt(878))
    text(s, "ОСНОВАНИЕ B", 984, 348, 702, 120, 84, WHITE, True, target=317, rotation=90, align="center")
    text(s, "РЕАЛЬНЫЙ ЭКРАН ДЕМОНСТРАТОРА", 169, 119, 880, 54, 31, LIME, bold=True, target=316, align="center")
    text(s, "Каждое решение открывает свой набор оснований.", 166, 647, 900, 76, 32, GRAY, target=315, align="center")
    footer(s, 11, "Сохранённый кадр P6: docs/demo/a04-ack-basis.png · масштабирование без подмены содержимого")
    notes(11, "Решение связано с основанием", "На слайде реальный сохранённый кадр демонстратора P6 — он явно подписан. В таблице решения confirm, resolve, rework, released связаны со своим основанием. На защите можно открыть исходный скринкаст. Этот кадр не выдаётся за экран промышленной линии и не является новым прогоном P8.", 25)

    # 12 — Original three-color-stripe composition becomes proof metrics.
    s = slides[11]
    remove(s, 332, 333, 334, 335, 336, 337)
    metrics = [("289", "АВТОТЕСТОВ", "171 backend\n77 PostgreSQL\n27 contract / ERP\n14 frontend", "222222"),
               ("19", "БРАУЗЕРНЫХ\nPASS", "Выпуск, ERP ACK\nи восстановление\nпосле потери ответа", WHITE),
               ("ACK", "РЕЗУЛЬТАТ\nСОХРАНЁН", "Повтор доставки\nбез второго\nбизнес-эффекта", "171717")]
    for i, (number, caption, detail, color) in enumerate(metrics):
        x = i * 267.18
        text(s, number, x + 18, 202, 231, 132, 96 if i != 2 else 78, color, True, align="center")
        text(s, caption, x + 18, 377, 231, 96, 28, color, bold=True, align="center")
        text(s, detail, x + 20, 509, 226, 155, 24, color, align="center")
    text(s, "ПРОВЕРЕНО\nНА СТЕНДЕ", 870, 189, 497, 216, 74, WHITE, True, target=338, align="center")
    text(s, "Протокол P8\n26–27 сентября 2026\n\nЭффект на реальном\nпроизводстве измерит пилот.", 890, 463, 450, 231, 29, GRAY, target=339, align="center")
    # The original white/lime columns reach the bottom: footer stays on dark side.
    text(s, "P8 · README / p8-pilot-completion.md", 845, 748, 540, 25, 18, GRAY)
    notes(12, "Программное доказательство P8", "Числа относятся к опубликованному протоколу P8: 289 автоматических тестов, отдельно 19 PASS браузерного runner. P8 также описывает TLS и восстановление резервной копии. Сейчас эти прогоны приложения не повторялись — мы показываем сохранённые результаты, а не новый прогон. Доставка с ACK проверена на внешнем ERP-эмуляторе. Промышленная точность и экономия не заявляются.", 25, ["docs/architecture/p8-pilot-completion.md"])

    # 13 — Original agenda becomes a practical pilot sequence.
    s = slides[12]
    text(s, "ПИЛОТ", 987, 302, 620, 126, 102, WHITE, True, target=147, rotation=90, align="center")
    for sid, num, head, body, y in [(150, "01", "ИДЕНТИФИКАЦИЯ", "Один участок, маршрут и владелец данных", 74),
                                  (148, "02", "ТЕНЕВОЙ РЕЖИМ", "Сверка с ОТК, поздние события и повторы", 330),
                                  (149, "03", "ДОПУСК ОТК", "Полнота контроля и регламент восстановления", 586)]:
        text(s, head, 262, y + 10, 738, 82, 53, WHITE, True, target=sid)
        text(s, body, 264, y + 108, 729, 65, 25, GRAY)
    for sid, number, y in [(151, "01", 87), (152, "02", 343), (153, "03", 599)]:
        text(s, number, 126, y, 109, 86, 60, LIME, True, target=sid)
    notes(13, "Пилот одного участка", "Стартуем с одного маршрута и проверяем идентификацию изделия. В теневом режиме сравниваем с ОТК и специально прогоняем пропуски, повторы, задержки и потерю связи. Полезность измеряем временем поиска оснований, полнотой контроля, пропусками и ложными тревогами. Затем принимаем решение о допуске. Промышленные сроки и размер экономии без обследования неизвестны.", 30)

    # 14 — Reprise of the original title plate as a clean conclusion.
    s = slides[13]
    text(s, "РЕШЕНИЕ ПО КАЧЕСТВУ\nДОЛЖНО ИМЕТЬ\nОСНОВАНИЕ", 205, 243, 1030, 302, 77, WHITE, True, target=137, align="center")
    text(s, "EFQA / ДОКАЗАТЕЛЬНЫЙ КОНТРОЛЬ", 75, 86, 500, 42, 22, LIME, bold=True, target=139)
    text(s, "Факты → контекст → решение", 785, 688, 570, 42, 30, LIME, target=138, align="center")
    notes(14, "Каждое решение имеет основание", "EFQA сохраняет факты, показывает контекст и фиксирует основания решения контролёра. Новые сведения требуют пересмотра, но не переписывают прошлое. Следующий результат — пилот одного производственного участка.", 20)

    # 15 — Appendix in the original four-column composition, with clickable refs.
    s = slides[14]
    for sid in [394, 395, 396, 397, 398, 399, 400, 401, 402, 403, 404, 405]:
        remove(s, sid)
    refs = [("NASA", "OCO / Glory\nRelease 19-036\n30.04.2019", SOURCES["nasa_oco"]),
            ("NASA IRT", "CRS-7\nPublic Summary\n12.03.2018", SOURCES["nasa_irt"]),
            ("РОСКОСМОС", "Прогресс МС-04\nцитата в ТАСС\n11.01.2017", SOURCES["progress"]),
            ("FAA", "Starship\nMishap Investigation\n08.09.2023", SOURCES["faa"])]
    for i, (head, body, url) in enumerate(refs):
        x = 112 + 317 * i
        text(s, head, x - 1, 165, 280, 58, 39, LIME, True, align="center")
        text(s, body, x - 1, 265, 279, 133, 27, GRAY, align="center")
        text(s, "Открыть источник ↗", x - 1, 430, 279, 35, 22, WHITE, align="center", url=url)
    text(s, "ИСТОЧНИКИ И МЕТОДИКА", 88, 587, 1263, 107, 74, WHITE, True, target=393, align="center")
    text(s, "5 событий · категории пересекаются · обзор кейсов не оценивает вероятность брака в отрасли", 115, 710, 1200, 40, 24, GRAY, align="center")
    footer(s, 15, "Сравнение продуктов: v8.1c.ru/erp/production · octave.com / Reliance · ссылки также в заметках")
    notes(15, "Источники", "Приложение для вопросов. Полные ссылки доступны по клику и в заметках. Методика и границы выборки — в space-quality-research.md. Свойства продуктов в сравнении взяты с официальных продуктовых страниц и не заменяют сравнительный пилот.", 0, list(SOURCES.values())[:6])

    # 16 — Keep the template's actual credit slide, including the SlidesCarnival logo.
    s = slides[15]
    text(s, "ДИЗАЙН И МАТЕРИАЛЫ", 108, 57, 1225, 124, 83, WHITE, True, target=490, align="center")
    text(s, "Оригинальный шаблон Dark Modern Tech Startup Brand\nадаптирован для проекта EFQA.", 330, 207, 780, 93, 29, GRAY, target=491, align="center")
    text(s, "Шаблон презентации", 207, 601, 447, 53, 29, GRAY, target=494, align="center", url=PAGE_URL)
    text(s, "NASA / EFQA", 760, 487, 487, 80, 54, LIME, True, target=493, align="center")
    text(s, "Фото Glory / кадр демонстратора", 750, 603, 490, 52, 26, GRAY, target=492, align="center")
    text(s, "Заголовки: Oswald, OFL · русская адаптация: EFQA Oswald · основной текст: Arial", 135, 727, 1170, 36, 24, GRAY, align="center")
    notes(16, "Авторы дизайна", "Оригинальный PowerPoint-шаблон SlidesCarnival / Canva Dark Modern Tech Startup Brand. Сохранена страница авторства. Исходные фотографии VR не использованы в содержательных слайдах. Фото Glory — NASA. Интерфейс — синтетический демонстратор EFQA. Шрифт EFQA Oswald — переименованный статический вариант Oswald по SIL Open Font License; полный текст лицензии рядом с исходниками шрифта.", 0, [PAGE_URL, SOURCES["photo"], SOURCES["font_license"]])

    # No claim of new application test runs: this build only makes presentation assets.
    draft = WORK / "EFQA-template-draft.pptx"
    prs.save(draft)
    elapsed = 0
    lines = ["# EFQA — выступление по шаблону Dark Modern Tech Startup", "", "14 основных слайдов + 2 приложения. Тайминг ориентировочный; подробности остаются в заметках.", ""]
    for item in all_notes:
        end = elapsed + item["seconds"]
        stamp = f"{elapsed // 60}:{elapsed % 60:02d}–{end // 60}:{end % 60:02d}" if item["seconds"] else "приложение"
        lines += [f"## {item['slide']}. {item['title']} — {stamp}", "", item["speech"], ""]
        lines += [f"- {url}" for url in item["sources"]] + [""]
        elapsed = end
    (OUT / "speaker-notes.md").write_text("\n".join(lines), encoding="utf-8")
    manifest = {"template_page": PAGE_URL, "download": TEMPLATE_URL, "sha256": TEMPLATE_SHA256,
                "output_slides": len(slides), "main_slides": 14, "main_seconds": elapsed,
                "render_engine": "Microsoft PowerPoint (export_template.ps1)", "slide_mapping": all_notes,
                "sources": SOURCES, "font_family": FONT}
    (OUT / "template-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Built {len(slides)} editable slides: {draft}")
    print(f"Main talk: {elapsed // 60}:{elapsed % 60:02d}; font: {WORK / 'EFQA-Oswald.ttf'}")


if __name__ == "__main__":
    build()
