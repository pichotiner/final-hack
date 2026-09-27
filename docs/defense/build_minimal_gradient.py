"""Original editable EFQA adaptation of the chosen Minimal Gradient direction.

Slidesgo's original PPTX requires account registration and was not downloaded.
Only the public visual direction is used: white, pink/orange blurred gradients,
large grotesk type, fine rules and editorial compositions. No preview image is
used as a slide background. PDF is exported from this PPTX by PowerPoint.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Pt
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
WORK = ROOT / ".local/minimal-gradient"
REFERENCE = "https://slidesgo.com/theme/minimal-gradient-pitch-deck"
INK, MUTED, LINE, ACCENT = "151515", "50505A", "B5AEB7", "883D62"
WHITE = "FFFFFF"
SOURCES = {
    "NASA / OCO + Glory": "https://www.nasa.gov/news-release/nasa-investigation-uncovers-cause-of-two-science-mission-launch-failures/",
    "NASA IRT / CRS-7": "https://www.nasa.gov/wp-content/uploads/2018/03/public_summary_nasa_irt_spacex_crs-7_final.pdf",
    "Роскосмос / цитата ТАСС": "https://tass.com/science/924519",
    "FAA / Starship": "https://www.faa.gov/newsroom/faa-closes-spacex-starship-mishap-investigation",
    "1С:ERP / производство": "https://v8.1c.ru/erp/production/",
    "Octave Reliance / QMS": "https://www.octave.com/products/asset-performance-management/reliance",
}


def make_backgrounds():
    """Gaussian colour fields fade all the way into white; no hard colour tiles."""
    WORK.mkdir(parents=True, exist_ok=True)
    configurations = {
        "cover": [(-.08, .76, .33, .35, (240, 124, 205), .88), (.44, 1.15, .50, .32, (255, 144, 60), .90)],
        "quiet": [(1.13, -.15, .40, .34, (255, 176, 112), .75), (-.20, 1.2, .44, .38, (223, 184, 239), .66)],
        "side": [(-.18, .40, .37, .57, (255, 180, 125), .72), (-.10, 1.15, .46, .35, (238, 149, 213), .72)],
        "warm": [(1.12, .91, .38, .43, (255, 153, 86), .73), (.65, 1.3, .39, .40, (243, 160, 224), .63)],
    }
    result = {}
    for name, blobs in configurations.items():
        image = Image.new("RGB", (640, 360))
        pixels = []
        for y in range(360):
            for x in range(640):
                rgb = [255., 255., 255.]
                for cx, cy, sx, sy, colour, strength in blobs:
                    a = strength * math.exp(-(((x / 639 - cx) / sx) ** 2 + ((y / 359 - cy) / sy) ** 2) / 2)
                    rgb = [v * (1 - a) + c * a for v, c in zip(rgb, colour)]
                pixels.append(tuple(round(v) for v in rgb))
        image.putdata(pixels)
        dest = WORK / f"gradient-{name}.png"
        image.resize((1920, 1080), Image.Resampling.BICUBIC).save(dest)
        result[name] = dest
    return result


class Deck:
    def __init__(self):
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = Pt(960), Pt(540)
        self.backgrounds = make_backgrounds()
        self.notes = []
        self.text_boxes = []
        for name, file in [("regular", "arial.ttf"), ("bold", "arialbd.ttf")]:
            pdfmetrics.registerFont(TTFont(name, f"C:/Windows/Fonts/{file}"))

    def text(self, s, value, x, y, w, h, size=18, bold=False, color=INK, align="left", url=None):
        # Explicit lines and bounded text. Fail the build rather than shrink silently.
        lines = value.split("\n")
        metric = "bold" if bold else "regular"
        for line in lines:
            if pdfmetrics.stringWidth(line, metric, size) > w - 3:
                raise ValueError(f"Too wide at {size}pt: {line!r}, box {w}")
        if len(lines) * size * 1.2 > h:
            raise ValueError(f"Too tall: {value!r}")
        if min(x, y) < 0 or x + w > 960.1 or y + h > 540.1:
            raise ValueError(f"Outside slide: {value!r}")
        sh = s.shapes.add_textbox(Pt(x), Pt(y), Pt(w), Pt(h))
        sh.name = "EFQA / " + lines[0][:50]
        tf = sh.text_frame
        tf.clear()
        tf.word_wrap = False
        tf.auto_size = MSO_AUTO_SIZE.NONE
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = Pt(0)
        for i, line in enumerate(lines):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = {"left": PP_ALIGN.LEFT, "right": PP_ALIGN.RIGHT, "center": PP_ALIGN.CENTER}[align]
            p.space_before = p.space_after = Pt(0)
            p.line_spacing = Pt(size * 1.2)
            run = p.add_run()
            run.text = line
            run.font.name = "Arial"
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.color.rgb = RGBColor.from_string(color)
            run.font._rPr.set("lang", "ru-RU")
        if url:
            sh.click_action.hyperlink.address = url
        self.text_boxes.append({"slide": len(self.prs.slides), "text": value, "box": [x, y, w, h], "size": size})
        return sh

    def rule(self, s, x1, y1, x2, y2, color=LINE, width=.7):
        sh = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Pt(x1), Pt(y1), Pt(x2), Pt(y2))
        sh.line.color.rgb = RGBColor.from_string(color)
        sh.line.width = Pt(width)
        sh._element.spPr.append(OxmlElement("a:effectLst"))

    def rect(self, s, x, y, w, h, fill, ellipse=False):
        sh = s.shapes.add_shape(MSO_SHAPE.OVAL if ellipse else MSO_SHAPE.RECTANGLE, Pt(x), Pt(y), Pt(w), Pt(h))
        sh.fill.solid()
        sh.fill.fore_color.rgb = RGBColor.from_string(fill)
        sh.line.fill.background()
        sh._element.spPr.append(OxmlElement("a:effectLst"))
        return sh

    def page(self, section, title=None, background="quiet", source=None):
        s = self.prs.slides.add_slide(self.prs.slide_layouts[6])
        s.shapes.add_picture(str(self.backgrounds[background]), 0, 0, width=Pt(960), height=Pt(540))
        self.text(s, "EFQA", 44, 23, 140, 16, 11, True)
        self.text(s, section, 356, 24, 370, 16, 10, color=MUTED)
        self.text(s, "КОСМОХАКАТОН / 2026", 738, 24, 178, 16, 10, align="right")
        self.rule(s, 44, 496, 916, 496, INK)
        self.text(s, source or "ИНТЕЛЛЕКТУАЛЬНЫЙ КОНТРОЛЬ КАЧЕСТВА", 44, 508, 818, 17, 9, color=MUTED)
        self.text(s, f"{len(self.prs.slides):02d}", 876, 506, 40, 20, 12, align="right")
        if title:
            self.text(s, title, 44, 80, 868, 96 if "\n" in title else 57, 36, True)
        return s

    def note(self, s, title, speech, seconds=35, sources=()):
        s.notes_slide.notes_text_frame.text = speech + "\n\n" + "\n".join(sources)
        self.notes.append({"title": title, "speech": speech, "seconds": seconds, "sources": list(sources)})

    def save(self):
        target = OUT / "presentation-minimal-gradient.pptx"
        self.prs.save(target)
        timing, md = 0, ["# EFQA · Minimal Gradient", "", "11 основных слайдов и приложение с источниками. Оригинальная адаптация по выбранному визуальному направлению; исходный PPTX Slidesgo не загружен.", ""]
        for i, n in enumerate(self.notes, 1):
            end = timing + n["seconds"]
            stamp = f"{timing // 60}:{timing % 60:02d}–{end // 60}:{end % 60:02d}" if n["seconds"] else "приложение"
            md += [f"## {i}. {n['title']} · {stamp}", "", n["speech"], "", *n["sources"], ""]
            timing = end
        (OUT / "speaker-notes-minimal-gradient.md").write_text("\n".join(md), encoding="utf-8")
        (WORK / "layout-check.json").write_text(json.dumps(self.text_boxes, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / "minimal-gradient-manifest.json").write_text(json.dumps({
            "visual_reference": REFERENCE, "original_template_downloaded": False,
            "reason": "Official PowerPoint download prompted account registration.",
            "design": "Original editable adaptation, procedural blurred backgrounds, Arial Cyrillic",
            "slide_count": len(self.notes), "seconds": timing, "sources": SOURCES,
            "export": "Microsoft PowerPoint; same PPTX supplies PDF and previews",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Built {len(self.notes)} slides; {timing} seconds; {len(self.text_boxes)} bounded text boxes")


def build():
    d = Deck()
    s = d.page("СИСТЕМА / ПРЕЗЕНТАЦИЯ", background="cover")
    d.text(s, "Проверяемая история детали.\nОбоснованное решение ОТК.", 523, 102, 393, 72, 22)
    d.text(s, "КАЧЕСТВО\nС ПРОВЕРЯЕМОЙ\nИСТОРИЕЙ", 44, 192, 868, 242, 64, True)
    d.text(s, "EFQA / От сигнала до решения", 48, 456, 590, 27, 18)
    d.note(s, "Качество с доказательствами", "EFQA собирает историю качества детали из наблюдений анализатора, производственных операций и решений ОТК. Важно не только найти признак, но и восстановить основания решения. Это синтетический демонстратор; реальный анализатор и интеграции требуют пилота.", 30)

    s = d.page("01 / ПРОБЛЕМА", background="side")
    d.text(s, "ГДЕ НАШЛИ", 44, 98, 852, 74, 55, True)
    d.text(s, "≠", 44, 194, 155, 110, 84, color=ACCENT)
    d.text(s, "ГДЕ ВОЗНИКЛО", 44, 314, 868, 80, 55, True)
    d.text(s, "После сборки нашли дефект.\nЭто ещё не доказательство вины сборщика.", 354, 208, 562, 77, 23)
    d.rule(s, 46, 422, 914, 422)
    d.text(s, "Нужны осмотры до/после, операции и данные оборудования.", 46, 448, 865, 30, 21)
    d.note(s, "Где нашли не равно где возникло", "Признак на финальном контроле мог существовать во входной детали или появиться при обработке. Само время обнаружения не доказывает физическую причину. EFQA помогает связать сопоставимые осмотры и контекст операции, сохраняя неизвестное как неизвестное.", 35)

    s = d.page("02 / РЕШЕНИЕ", "Сигнал. Контекст. Основание.")
    entries = [("01", "СИГНАЛ", "Анализатор\nсообщает признак"), ("02", "КОНТЕКСТ", "Деталь, операция\nи оборудование"), ("03", "ОСНОВАНИЕ B", "Что было известно\nпри решении"), ("04", "РЕШЕНИЕ", "Контролёр\nпринимает решение")]
    for i, (num, title, detail) in enumerate(entries):
        x = 44 + i * 224
        d.text(s, num, x, 196, 181, 62, 44, color=ACCENT)
        d.rule(s, x, 277, x + 194, 277, INK)
        d.text(s, title, x, 301, 212, 29, 18, True)
        d.text(s, detail, x, 345, 211, 62, 18, color=MUTED)
    d.text(s, "Поздние данные меняют текущую картину — старое B сохраняется.", 44, 454, 868, 28, 20)
    d.note(s, "Основания тогда и сейчас", "Исходный сигнал не равен подтверждённому дефекту. B фиксирует основания решения: события, контекст и известные пробелы. Поздний входной осмотр добавляется отдельно, изменяет текущую оценку и требует пересмотра. Старое решение не переписывается. Полный межиздельный охват по фактическому составу ещё требует отдельного эксперимента.", 45)

    s = d.page("03 / СРАВНЕНИЕ", "Что добавляет EFQA", source="Продуктовые страницы: 1С:ERP · Octave Reliance. Неизвестные функции не считаются отсутствующими.")
    for i, (head, body) in enumerate([
        ("1С:ERP", "Заказы, операции\nи серийный учёт"),
        ("Octave Reliance", "QMS: несоответствия,\nCAPA и аудит"),
        ("Машинное зрение", "Признаки дефекта\nна точке контроля"),
    ]):
        x = 44 + i * 299
        d.text(s, head, x, 207, 290, 34, 23, True)
        d.text(s, body, x, 265, 288, 66, 20, color=MUTED)
        d.rule(s, x, 348, x + 274, 348)
    d.text(s, "EFQA", 44, 394, 167, 58, 38, True)
    d.text(s, "Основания «тогда/сейчас», полнота контроля\nи пересмотр после поздних данных — в одном сценарии.", 227, 399, 687, 65, 21)
    d.note(s, "Сильные альтернативы", "1С:ERP поддерживает производственный маршрут и серийный учёт. Octave Reliance, бывшая ETQ, даёт полноценные процессы качества. EFQA демонстрирует специфичную композицию: основания решения, полноту и поздние события. Это проверяемая ценность под кейс, а не доказательство превосходства по цене или всем функциям. Конкретную QMS нужно проверить на том же сценарии.", 40, [SOURCES["1С:ERP / производство"], SOURCES["Octave Reliance / QMS"]])

    s = d.page("04 / NASA · OCO / GLORY", "Цена недостоверного сертификата", "warm", "NASA Release 19-036 · 30.04.2019 · OCO: 2009 / Glory: 2011 · носитель Orbital Sciences")
    d.text(s, "> $700M", 44, 186, 590, 111, 82)
    d.text(s, "потери двух миссий\nпо оценке NASA", 48, 321, 435, 71, 25, color=MUTED)
    d.rule(s, 563, 197, 563, 410)
    d.text(s, "МАТЕРИАЛ\nИ ИСПЫТАНИЯ", 607, 202, 307, 86, 28, True)
    d.text(s, "Некачественные профили.\nИзменённые результаты\nиспытаний и сертификаты.", 607, 315, 307, 90, 21)
    d.text(s, "Профилактика: независимые испытания и прослеживаемость поставки.", 44, 451, 869, 31, 20)
    d.note(s, "OCO и Glory", "NASA в 2019 году связала техническую первопричину двух потерь миссий Taurus XL с материалами Sapa Profiles и изменёнными результатами испытаний профилей узла разделения. Более 700 миллионов долларов — оценка NASA. Эти две миссии не являются независимыми примерами: общий поставочный механизм связывает их. Неизменяемый журнал не доказывает правдивость изначально ложного сертификата.", 45, [SOURCES["NASA / OCO + Glory"]])

    s = d.page("05 / SPACEX · CRS-7", "Один отказ — несколько версий", source="NASA Independent Review Team · CRS-7 Public Summary · 12.03.2018")
    d.text(s, "CRS-7", 44, 205, 414, 114, 82)
    d.text(s, "28.06.2015\nSpaceX / Falcon 9", 48, 340, 397, 70, 24, color=MUTED)
    for i, (label, detail) in enumerate([
        ("ФАКТ", "Разрыв кислородного бака."),
        ("ВЕРСИИ", "Материал, повреждение, монтаж."),
        ("УРОК", "Проверять применимость детали\nк критическим условиям работы."),
    ]):
        y = 194 + i * 88
        d.text(s, label, 493, y, 423, 24, 13, True, ACCENT)
        d.text(s, detail, 493, y + 29, 423, 61, 20)
    d.note(s, "CRS-7", "Отказ крепления COPV — правдоподобная последовательность, но NASA IRT не выделила материал как единственный наиболее вероятный инициатор. Были альтернативы повреждения и монтажа. Также NASA указала на недостаточную проверку промышленной детали для критического криогенного узла. EFQA сохраняет версии анализа и основания; это не доказательство предотвращения такой аварии.", 40, [SOURCES["NASA IRT / CRS-7"]])

    s = d.page("06 / РОСКОСМОС И SPACEX", "Причина ≠ корректирующее действие", source="Роскосмос / цитата ТАСС, 11.01.2017 · FAA, 08.09.2023")
    d.rule(s, 477, 183, 477, 435)
    d.text(s, "ПРОГРЕСС МС-04", 44, 187, 410, 34, 23, True)
    d.text(s, "?", 44, 237, 155, 102, 78, color=ACCENT)
    d.text(s, "Возможны частицы\nили нарушения сборки насоса.\nИнициатор не определён.", 44, 356, 411, 82, 21)
    d.text(s, "STARSHIP / FAA", 522, 187, 392, 34, 23, True)
    d.text(s, "63", 522, 237, 298, 102, 78)
    d.text(s, "корректирующих действия\nпо одному событию.\nЭто не 63 дефекта.", 522, 356, 392, 82, 21)
    d.note(s, "Прогресс и Starship", "По Прогрессу МС-04, потерянному 1 декабря 2016 года, Роскосмос называл возможные посторонние частицы или нарушения сборки насоса, включая зазоры и дисбаланс. FAA по испытательному полёту Starship 20 апреля 2023 указала несколько первопричин и 63 корректирующих действия. Число действий нельзя трактовать как число дефектов или производственных причин.", 40, [SOURCES["Роскосмос / цитата ТАСС"], SOURCES["FAA / Starship"]])

    s = d.page("07 / ОЦЕНКА ЭТАПОВ", "Где расследования ищут риск", source="n = 5 событий, 2009–2023 · категории пересекаются · OCO / Glory связаны общей поставкой")
    d.text(s, "5", 44, 188, 166, 111, 84)
    d.text(s, "событий\nв целевой\nвыборке", 48, 318, 178, 95, 22, color=MUTED)
    rows = [("Материал / сертификаты", "OCO + Glory · вывод NASA", 2),
            ("Изготовление / сборка", "CRS-7 + Прогресс · версии", 2),
            ("Проектирование / проверка", "CRS-7 · применимость детали", 1),
            ("Этап не разложен", "Starship · данных недостаточно", 1)]
    for i, (head, caption, n) in enumerate(rows):
        y = 185 + i * 65
        d.text(s, head, 274, y, 391, 30, 19, True)
        d.text(s, caption, 274, y + 31, 391, 23, 14, color=MUTED)
        d.rect(s, 674, y + 10, 150, 10, "E7E0E4")
        d.rect(s, 674, y + 10, 150 * n / 5, 10, "B76B90")
        d.text(s, f"{n}/5 · {n*20}%", 842, y + 4, 74, 23, 14, True, align="right")
    d.text(s, "Это распределение упоминаний в кейсах, а не частота брака в отрасли.", 274, 458, 640, 21, 16, color=ACCENT)
    d.note(s, "Малая открытая выборка", "Единица учёта — событие: OCO, Glory, CRS-7, Прогресс МС-04 и Starship. Материалы подтверждены для OCO/Glory; сборка и изготовление рассматриваются как альтернативные инициаторы CRS-7 и Прогресса. В CRS-7 также отмечен инженерный фактор. Категории пересекаются, а OCO/Glory не независимы. Полосы имеют общий знаменатель 5, поэтому 40 процентов занимают 40 процентов шкалы. Обобщать эти цифры на всю отрасль нельзя.", 40, list(SOURCES.values())[:4])

    s = d.page("08 / ПРОФИЛАКТИКА", "Контроль на каждом этапе")
    measures = [("01", "ПОСТАВКА", "Партия и независимые\nиспытания материала."),
                ("02", "ПРОЦЕСС", "Режимы, чистота, зазоры\nи проверка инструмента."),
                ("03", "ВЫПУСК", "Полнота контроля,\nактуальное основание B.")]
    for i, (num, head, body) in enumerate(measures):
        x = 44 + i * 299
        d.text(s, num, x, 183, 276, 86, 58, color=ACCENT)
        d.rule(s, x, 288, x + 269, 288, INK)
        d.text(s, head, x, 307, 271, 35, 24, True)
        d.text(s, body, x, 365, 271, 73, 21)
    d.text(s, "Камера видит поверхность. Метод проверки выбирают под физику дефекта.", 44, 458, 870, 23, 18, color=MUTED)
    d.note(s, "Профилактика", "Профилактика не сводится к одной камере. Для материала нужны испытания, для скрытого дефекта — подходящий неразрушающий контроль, для сборки — чистота и защита от неправильной установки. EFQA поддерживает полноту контроля и основания решений. Реальные партии, телеметрия и промышленный анализатор требуют отдельной интеграции.", 40)

    s = d.page("09 / ПРОВЕРКА РЕАЛИЗАЦИИ", "Путь до решения уже проверен", source="Сохранённый протокол P8 · 26–27.09.2026 · синтетический стенд; тесты приложения здесь не повторялись")
    for x, num, label, detail in [(44, "289", "автотестов", "Ядро, БД, контракт и UI"), (343, "19", "браузерных PASS", "Выпуск и восстановление"), (642, "ACK", "результат принят", "Внешний ERP-эмулятор")]:
        d.text(s, num, x, 186, 274, 107, 76)
        d.text(s, label, x, 317, 280, 35, 22, True)
        d.text(s, detail, x, 362, 280, 28, 17, color=MUTED)
    d.rule(s, 44, 426, 916, 426)
    d.text(s, "Задание ERP → события → решение ОТК → подтверждение ERP", 44, 450, 870, 32, 22)
    d.note(s, "Что проверено в P8", "289 — итог опубликованного протокола P8: 171 backend, 77 PostgreSQL, 27 contract/ERP, 14 frontend. Отдельно 19 браузерных PASS. Это историческая проверка конкретного синтетического стенда. ACK означает, что ERP-эмулятор сохранил результат, а не физическую отгрузку. Эффект на предприятии измеряется пилотом одного участка.", 35, ["docs/architecture/p8-pilot-completion.md"])

    s = d.page("10 / ИТОГ", background="cover")
    d.text(s, "У КАЖДОГО\nРЕШЕНИЯ —\nСВОЁ ОСНОВАНИЕ", 44, 119, 868, 230, 55, True)
    d.text(s, "EFQA связывает факты, контекст и решение человека.", 48, 389, 864, 37, 23)
    d.text(s, "СЛЕДУЮЩИЙ ШАГ / ТЕНЕВОЙ ПИЛОТ ОДНОГО УЧАСТКА", 48, 456, 864, 25, 15, True, ACCENT)
    d.note(s, "Итог и пилот", "Решение по качеству должно иметь проверяемое основание. EFQA сохраняет исходные факты, разделяет наблюдение и причину, а поздние данные вызывают пересмотр. Следующий этап — один маршрут, проверенная идентификация и теневой пилот с ОТК. Измеряем полноту данных, время разбора, пропуски и ложные тревоги.", 30)

    s = d.page("ПРИЛОЖЕНИЕ / ИСТОЧНИКИ", "На чём основана презентация", source="Визуальный ориентир: Slidesgo / Minimal Gradient Pitch Deck. Самостоятельная адаптация EFQA.")
    refs = [("NASA · OCO / Glory", "Release 19-036 / 2019", "NASA / OCO + Glory"),
            ("NASA IRT · CRS-7", "Public Summary / 2018", "NASA IRT / CRS-7"),
            ("Роскосмос / ТАСС", "Прогресс МС-04 / 2017", "Роскосмос / цитата ТАСС"),
            ("FAA · Starship", "Mishap Investigation / 2023", "FAA / Starship"),
            ("1С:ERP", "Производство и серийный учёт", "1С:ERP / производство"),
            ("Octave Reliance", "QMS / CAPA / аудит", "Octave Reliance / QMS")]
    for i, (head, detail, key) in enumerate(refs):
        x, y = 44 + (i % 2) * 460, 184 + (i // 2) * 100
        d.text(s, head, x, y, 409, 30, 20, True, url=SOURCES[key])
        d.text(s, detail, x, y + 37, 409, 27, 17, color=MUTED)
        d.rule(s, x, y + 76, x + 399, y + 76)
    d.note(s, "Источники и дизайн", "Полные ссылки активны в заголовках и приведены ниже. Официальный исходник Minimal Gradient Pitch Deck от Slidesgo при скачивании потребовал регистрацию. Поэтому это самостоятельная редактируемая адаптация визуального направления: белый фон, мягкие розово-оранжевые градиенты и крупная типографика. Исходный PPTX Slidesgo не использован. Градиентные фоны сгенерированы для EFQA; текст и схемы редактируются.", 0, [*SOURCES.values(), REFERENCE])
    d.save()


if __name__ == "__main__":
    build()
