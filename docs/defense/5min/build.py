"""Build the five-minute EFQA defense in editable PPTX and font-embedded PDF.

Run from any directory with: python docs/defense/5min/build.py
All coordinates are in 960 x 540 points; the same drawing instructions make both files.
"""

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Pt
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


HERE = Path(__file__).resolve().parent
FONTS = HERE / "fonts"
for name, filename in (("Montserrat", "Montserrat-Regular.ttf"), ("Montserrat-Semibold", "Montserrat-SemiBold.ttf")):
    pdfmetrics.registerFont(TTFont(name, str(FONTS / filename)))

NAVY = "1C3040"
DEEP = "152B37"
BLUE = "23516A"
TEAL = "246A61"
LIGHT = "E9F1ED"
ICE = "FAF8F3"
MINT = "E3EFEB"
YELLOW = "F3E8D9"
AMBER = "824B29"
RED = "A34430"
WHITE = "FFFFFF"

prs = Presentation()
prs.slide_width = Pt(960)
prs.slide_height = Pt(540)
pdf = canvas.Canvas(str(HERE / "presentation-5min.pdf"), pagesize=(960, 540))


def rgb(hex_color):
    return RGBColor.from_string(hex_color)


def rect(x, y, w, h, fill=WHITE, stroke=None, radius=False):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE, Pt(x), Pt(y), Pt(w), Pt(h))
    if radius:
        shape.adjustments[0] = 0.07
    shape.fill.solid()
    shape.fill.fore_color.rgb = rgb(fill)
    if stroke:
        shape.line.color.rgb = rgb(stroke)
        shape.line.width = Pt(1.2)
    else:
        shape.line.fill.background()
    pdf.setFillColor("#" + fill)
    if stroke:
        pdf.setStrokeColor("#" + stroke)
        pdf.setLineWidth(1.2)
    if radius:
        pdf.roundRect(x, 540 - y - h, w, h, 8, fill=1, stroke=int(bool(stroke)))
    else:
        pdf.rect(x, 540 - y - h, w, h, fill=1, stroke=int(bool(stroke)))


def line(x1, y1, x2, y2, color=BLUE, width=1):
    connector = slide.shapes.add_connector(1, Pt(x1), Pt(y1), Pt(x2), Pt(y2))
    connector.line.color.rgb = rgb(color)
    connector.line.width = Pt(width)
    pdf.setStrokeColor("#" + color)
    pdf.setLineWidth(width)
    pdf.line(x1, 540 - y1, x2, 540 - y2)


def txt(value, x, y, w, size=16, color=NAVY, bold=False, h=None):
    """Draw one explicit line; multiline layout is controlled with separate calls."""
    h = h or size * 1.5
    box = slide.shapes.add_textbox(Pt(x), Pt(y), Pt(w), Pt(h))
    tf = box.text_frame
    tf.clear()
    tf.word_wrap = False
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.text = value
    p.font.name = "Montserrat"
    p.font.size = Pt(size)
    p.font.bold = bold
    p.font.color.rgb = rgb(color)
    pdf.setFillColor("#" + color)
    pdf.setFont("Montserrat-Semibold" if bold else "Montserrat", size)
    pdf.drawString(x, 540 - y - size * 1.04, value)


def lines(items, x, y, w, size=15, gap=23, color=NAVY, bold=False):
    for i, item in enumerate(items):
        txt(item, x, y + gap * i, w, size, color, bold)


def arrow(x1, x2, y, color=BLUE):
    line(x1, y, x2 - 7, y, color)
    line(x2 - 7, y - 4, x2, y, color)
    line(x2 - 7, y + 4, x2, y, color)


def begin(index, section, title, seconds, note, dark=False):
    global slide
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    rect(0, 0, 960, 540, DEEP if dark else ICE)
    ink = WHITE if dark else NAVY
    line(48, 55, 912, 55, WHITE if dark else BLUE, 0.7)
    txt("EFQA", 48, 27, 140, 12, ink, True)
    txt("Контроль качества / космохакатон 2026", 132, 27, 485, 11, ink)
    txt(section, 674, 27, 240, 10, WHITE if dark else TEAL, True)
    if title:
        txt(title, 48, 88, 868, 29, ink, True)
    line(48, 489, 912, 489, WHITE if dark else BLUE, 0.7)
    txt("P8  /  демонстратор на синтетических данных", 48, 501, 710, 10, ink)
    txt(f"{index:02d} — 08", 853, 500, 65, 10, ink, True)
    slide.notes_slide.notes_text_frame.text = f"{seconds} с.\n{note}"


def finish():
    pdf.showPage()


# 01 — the point of the system
begin(1, "ЗАДАЧА", "", 20,
      "Камера видит признак, но не знает, достаточен ли контроль и что случилось раньше. "
      "EFQA собирает проверяемую историю конкретной детали и отделяет вывод анализатора от решения ОТК.", True)
txt("Камера видит признак.", 48, 117, 855, 42, WHITE, True)
txt("Мы видим всю историю детали.", 48, 180, 875, 37, WHITE, True)
line(48, 275, 912, 275, WHITE, 0.8)
for x, number, title, detail in [
    (48, "01", "Что проверили", "Метод, область и результат"),
    (349, "02", "Что изменилось", "Операция и обстоятельства"),
    (659, "03", "Почему выпустили", "Основание решения ОТК"),
]:
    txt(number, x, 302, 50, 15, "B6D3D0", True)
    txt(title, x, 339, 251, 19, WHITE, True)
    txt(detail, x, 385, 262, 13, WHITE)
txt("Неизвестный результат контроля не становится «годным» автоматически.", 48, 452, 863, 14, "B6D3D0")
finish()

# 02 — separate agencies and the event denominator
begin(2, "ПУБЛИЧНЫЕ РАЗБОРЫ", "Четыре потери миссий. Разные причины.", 45,
      "В публичной выборке четыре разных неудачных запуска. NASA установило техническую причину двух миссий OCO и Glory на носителе Orbital Sciences: материалы и ложные результаты испытаний поставщика Sapa Profiles. "
      "CRS-7 SpaceX: аномалия примерно на 139-й секунде, но альтернативы инициатора сохраняются. Progress MS-04: на 382-й секунде, частицы или сборка насоса — версии. "
      "139 и 382 секунды — моменты проявления в полёте, не моменты появления брака на производстве.")
cases = [
    (48, "NASA / OCO и Glory", "2", ["Taurus XL / Orbital Sciences", "Материал Sapa Profiles", "и ложные результаты испытаний", "Причина установлена NASA"], TEAL),
    (345, "SpaceX / CRS-7", "139 с", ["Аномалия в полёте", "Материал, повреждение или", "монтаж — альтернативы", "Инициатор точно не установлен"], BLUE),
    (645, "Роскосмос / МС-04", "382 с", ["Разрушение насосного узла", "Частицы или сборка — версии", "Этап возникновения не", "установлен однозначно"], AMBER),
]
for x, label, value, details, accent in cases:
    line(x, 158, x + 263, 158, accent, 2.5)
    txt(label, x, 173, 266, 13, accent, True)
    txt(value, x, 212, 271, 32, NAVY, True)
    lines(details, x, 275, 278, 12, 26)
line(331, 159, 331, 392, BLUE, 0.6)
line(632, 159, 632, 392, BLUE, 0.6)
line(48, 402, 912, 402, BLUE, 0.6)
txt("139 и 382 секунды — время проявления в полёте, не время появления дефекта.", 48, 413, 865, 13, RED, True)
txt("FAA: 63 корректирующих действия по Starship-2023 — не 63 дефекта.", 48, 444, 856, 11, TEAL)
txt("NASA (2019); NASA IRT (2018); ТАСС / Роскосмос (2017); FAA (2023).", 48, 468, 856, 10, BLUE)
finish()

# 03 — meaningful frequency while keeping uncertainty visible
begin(3, "ГРАНИЦА ДАННЫХ", "Где цепочка началась — и что неизвестно", 35,
      "Из четырёх специально отобранных событий у двух подтверждён материальный и испытательный контур до сборки: это OCO и Glory. "
      "У двух других единая первичная производственная стадия публично не доказана. 2 из 4 — показатель только выбранных случаев, не частота брака отрасли. "
      "В P8 уже есть разрезы по типу, линии, участку и сопоставимым операциям; отдельную метрику первого надёжного обнаружения по контрольной точке ещё нужно проверить на пилоте. Для доли нужны одна когорта, период и N оценимо проверенных изделий; причины без оснований остаются unknown.")
txt("Из четырёх выбранных случаев:", 48, 155, 853, 15, BLUE)
txt("2", 48, 191, 160, 75, TEAL, True)
txt("2", 498, 191, 160, 75, AMBER, True)
line(469, 185, 469, 309, BLUE, 0.7)
txt("поставка / испытания", 48, 274, 402, 17, NAVY, True)
txt("этап не доказан точно", 498, 274, 419, 17, NAVY, True)
txt("OCO + Glory: вывод NASA", 48, 304, 395, 12, TEAL)
txt("CRS-7 + МС-04: несколько версий", 498, 304, 411, 12, AMBER)
line(48, 352, 912, 352, BLUE, 0.7)
txt("Где впервые увидели?", 48, 368, 410, 16, NAVY, True)
txt("Где возникло?", 496, 368, 410, 16, NAVY, True)
txt("Контрольная точка + оценимый осмотр", 48, 400, 418, 12)
txt("Только решение с основанием; иначе unknown", 496, 400, 417, 12)
txt("Доля на участке: n дефектных изделий / N оценимо проверенных той же когорты.", 48, 450, 862, 12, RED, True)
finish()

# 04 — the user's requested detailed physical + digital route
begin(4, "МАРШРУТ ДЕТАЛИ", "От заготовки до подтверждения ERP", 70,
      "Идём по одной синтетической сборке. ERP даёт задание и версию маршрута; скан фиксирует экземпляр и фактическую установку. "
      "CP-IN проверяет доступную поверхность и назначенный технологом метод материала; после обработки CP-M сопоставляет видимую область, размер и режим станка. "
      "Перед закрытием сборки фиксируем компонент, инструмент и параметры, затем CP-F и нужные невизуальные испытания. "
      "В каждой точке качество, идентичность и применимость метода проверяются: плохой кадр означает повтор, а не годность. "
      "События идут через edge и API в журнал, историю и набор оснований B; контролёр подтверждает дефект отдельно от выпуска. "
      "При доработке новый run и новый цикл обязательных проверок. Outbox отправляет версию решения в ERP и сохраняет ACK. "
      "Физические камеры, edge и ранняя сенсорика — проект для пилота, программный путь P8 работает с эмулятором.")
txt("01   ФИЗИЧЕСКИЙ МАРШРУТ  /  ПРЕДЛОЖЕННЫЕ КОНТРОЛЬНЫЕ ТОЧКИ", 48, 137, 864, 11, TEAL, True)
steps = [
    ("01  ЗАДАНИЕ", ["ERP: ID, маршрут", "PLM: состав@rev"]),
    ("02  CP-IN", ["скан экземпляра", "вход / материал"]),
    ("03  ОБРАБОТКА", ["run, станок, режим", "время / оператор"]),
    ("04  CP-M", ["осмотр + размер", "после обработки"]),
    ("05  СБОРКА", ["MES: факт установки", "момент / чистота"]),
    ("06  CP-F", ["до закрытия узла", "визуал + доп. метод"]),
    ("07  ОТК", ["проверка полноты", "доработка / выпуск"]),
]
for i, (header, desc) in enumerate(steps):
    x = 48 + i * 124
    line(x, 172, x + 110, 172, TEAL if i in (1, 3, 5) else BLUE, 2)
    txt(header, x, 186, 110, 10, TEAL if i in (1, 3, 5) else BLUE, True)
    lines(desc, x, 216, 112, 10, 19)
    if i < 6:
        arrow(x + 111, x + 121, 173)
line(48, 271, 912, 271, BLUE, 0.6)
txt("На каждой CP: экземпляр · операция · область · метод · качество. Пропуск / плохой кадр → повтор.", 48, 280, 864, 11, RED, True)
txt("При дефекте: ОТК → доработка → новый run и цикл → повторные CP → решение.", 48, 299, 864, 11, TEAL, True)
txt("02   ЦИФРОВОЙ МАРШРУТ  /  P8, СТРУКТУРИРОВАННЫЕ СИГНАЛЫ", 48, 329, 864, 11, TEAL, True)
digital = [
    ("ИСТОЧНИК / API", "ID, схема¹", "дубль без эффекта"),
    ("RAW + КОНТЕКСТ", "2 времени", "изделие / run"),
    ("ОЦЕНКА + B", "контроль / версии", "неизвестность"),
    ("РЕШЕНИЕ ОТК", "confirm → rework", "новый цикл → release"),
    ("OUTBOX → ERP", "версия результата", "ACK / retry"),
]
for i, (header, a, b) in enumerate(digital):
    x = 48 + i * 175
    line(x, 362, x, 433, BLUE, 0.6)
    txt(header, x + 9, 370, 156, 10, TEAL if i == 3 else BLUE, True)
    lines([a, b], x + 9, 399, 152, 10, 19)
    if i < 4:
        arrow(x + 155, x + 169, 374, BLUE)
txt("¹ Физические камеры и edge-очередь — проект; P8 принимает структурированный сигнал эмулятора.", 48, 457, 864, 10, TEAL)
finish()

# 05 — causal separation and architecture
begin(5, "АРХИТЕКТУРА", "Как устроена проверяемая история", 40,
      "Сигнал анализатора — только наблюдение. API проверяет права, формат и повторы; PostgreSQL в одной транзакции хранит raw, историю, основания решения и outbox. "
      "Граф означает типизированные связи в SQL между деталью, операцией, осмотром и решением, не отдельную графовую базу. "
      "Решение контролёра неизменяемо и ссылается на B. Плохой контроль и неполная привязка блокируют выпуск. Outbox переживает потерю связи с ERP, ACK означает запись/применение результата, не физическую отгрузку. "
      "Сильная альтернатива — QMS с полноценным аудитом; сравниваем тот же сценарий и стоимость поддержки.")
txt("На участке", 48, 154, 300, 19, NAVY, True)
txt("В ядре / PostgreSQL", 441, 154, 463, 19, NAVY, True)
line(48, 188, 912, 188, BLUE, 0.8)
for i, (a, b) in enumerate([
    ("ERP, MES, PLM", "задание · фактический run · проектный состав"),
    ("Анализатор", "признак и версия, но не вердикт"),
    ("Контролёр", "решение по закреплённому B"),
]):
    y = 208 + i * 66
    txt(a, 48, y, 343, 14, BLUE, True)
    txt(b, 48, y + 24, 350, 11)
    line(48, y + 57, 387, y + 57, BLUE, 0.4)
line(412, 190, 412, 409, BLUE, 0.8)
txt("Приём и права", 441, 208, 180, 14, BLUE, True)
txt("схема, идентификатор события, повтор", 640, 210, 272, 11)
line(441, 245, 912, 245, BLUE, 0.4)
txt("Исходники и связи", 441, 261, 211, 14, BLUE, True)
txt("raw AES-GCM · 2 времени · связи", 658, 264, 258, 11)
line(441, 298, 912, 298, BLUE, 0.4)
txt("Оценка и решение", 441, 315, 229, 14, BLUE, True)
txt("полнота → B → ОТК → outbox → ACK", 651, 317, 266, 11)
line(441, 352, 912, 352, BLUE, 0.4)
txt("Пробелы видны; поздний факт создаёт новую оценку.", 441, 373, 469, 12, TEAL)
txt("В стенде — ERP-эмулятор; 1С, Галактика, MES и КОМПАС — целевые адаптеры.", 48, 405, 864, 11, TEAL)
line(48, 430, 912, 430, BLUE, 0.8)
txt("Альтернатива: QMS с аудитом. Тестируем B «тогда», поздний факт и ACK.", 48, 447, 857, 14, NAVY, True)
finish()

# 06 — traceable example and late evidence
begin(6, "ОДНА ДЕТАЛЬ", "Поздний факт меняет «сейчас», не «тогда»", 40,
      "Синтетическая сборка assembly-001: вход и контроль после обработки не показали признаков, после сборки найдена риска и одновременно известно отклонение станка. "
      "Контролёр подтвердил один дефект и назначил доработку; причину оставил неизвестной. "
      "Позже пришёл осмотр с временем до сборки, который указывает на ту же риску и противоречит отрицательному входному осмотру. Старое решение D1 по B1 остаётся в истории, B2 показывает конфликт и требует пересмотра. "
      "Два наблюдения одной риски и дубли доставки не размножают дефекты. Наличие станочного отклонения не делает оператора виновным.")
txt("Синтетический пример  /  assembly-001", 48, 150, 812, 12, TEAL)
line(48, 188, 912, 188, BLUE, 0.8)
txt("Тогда — B1", 48, 204, 366, 22, NAVY, True)
txt("Сейчас — B2", 510, 204, 369, 22, NAVY, True)
line(482, 188, 482, 427, BLUE, 0.7)
lines(["10:02  Вход: признаков нет", "10:06  После обработки: чисто", "10:08  Отклонение станка", "10:10  След на вставке", "10:12  ОТК: 1 дефект, причина неизвестна", "10:13  Направление на доработку"], 48, 254, 422, 13, 30)
lines(["10:20  Получен поздний сигнал", "Время события — 10:02:30", "Осмотр до сборки сообщил о следе", "Входные осмотры противоречат", "Решение D1 и B1 сохранены", "Нужен пересмотр на новом B2"], 510, 254, 407, 13, 30)
line(48, 441, 912, 441, RED, 1)
txt("Порядок обнаружения ≠ причина. Повтор не удваивает дефекты и KPI.", 48, 455, 856, 12, RED, True)
finish()

# 07 — what prevents what; realized vs planned
begin(7, "ПОЛЬЗА И ГОТОВНОСТЬ", "Где вмешаться до следующей операции", 35,
      "Программно в P8 проверены полный путь через ERP-эмулятор, B, контроль полноты, доработка, повторный контроль, доставка и ACK: 289 тестов и 19 браузерных PASS из сохранённого протокола. "
      "В демонстрации: confirm → rework → новый run/цикл → повторный контроль → resolve → release → ERP ACK. Физический пилот иной: входные испытания и применимость метода назначает технолог, edge связывает ID и буферизует при обрыве, раннее предупреждение по режиму предлагает адресную проверку. "
      "Вибрация — риск, а не брак; предупреждение не доказывает предотвращённую аварию. Проектируем теневой пилот и измеряем пропуски, ложные тревоги и время до проверки.")
rows = [
    ("Поставка / материал", "Происхождение и нужное испытание", "Пилот"),
    ("Обработка / сборка", "Отклонение режима → адресная проверка", "EW-01: проект"),
    ("Пропуск / плохой кадр", "Пробел → повтор, gate выпуска", "Проверено P8"),
    ("Поздний факт / повтор", "B тогда/сейчас, review, стабильный ID", "Проверено P8"),
]
txt("Ситуация", 48, 159, 261, 11, TEAL, True)
txt("Реакция", 316, 159, 435, 11, TEAL, True)
txt("Статус", 777, 159, 132, 11, TEAL, True)
line(48, 185, 912, 185, BLUE, 1)
for i, (risk, mechanism, status) in enumerate(rows):
    y = 201 + 57 * i
    txt(risk, 48, y, 260, 13, NAVY, True)
    txt(mechanism, 316, y, 442, 12)
    txt(status, 777, y, 135, 11, TEAL if "P8" in status else AMBER, True)
    line(48, y + 40, 912, y + 40, BLUE, 0.5)
txt("Стенд: доработка → новый контроль → устранение → выпуск → ERP ACK.", 48, 442, 862, 12, NAVY, True)
txt("P8: 289 тестов / 19 браузерных PASS. Дальше — теневой пилот одного участка.", 48, 465, 856, 11, TEAL)
finish()

# 08 — decisive close and main evidence anchors
begin(8, "ВЫВОД", "", 15,
      "Результат: мы показываем не просто зелёную лампу, а что проверено, каким методом и на каком основании подписан выпуск. "
      "Следующий шаг роли один — измерить цену replay и полноту зависимостей; роли два — совместно с технологом подтвердить ID, точки и методы для одного теневого участка. "
      "Публикации расследований и методика выборки — в notes и приложении; P8 доказан только на синтетическом стенде.", True)
txt("Выпуск — не зелёная лампа.", 48, 127, 864, 40, WHITE, True)
txt("Это решение с основаниями.", 48, 191, 864, 40, WHITE, True)
line(48, 288, 912, 288, WHITE, 0.8)
txt("Проверить метод. Показать пробелы. Сохранить, что знал контролёр.", 48, 321, 866, 16, WHITE)
txt("Дальше — теневой пилот на одном участке с измеренным результатом.", 48, 397, 866, 17, "B6D3D0", True)
txt("Источники: NASA IRT (2018); NASA (2019); Роскосмос / ТАСС (2017); docs/defense/5min/SOURCES.md", 48, 461, 864, 10, "B6D3D0")
finish()

pdf.save()
prs.save(str(HERE / "presentation-5min.pptx"))
print("Built presentation-5min.pptx and presentation-5min.pdf (8 slides, 300 seconds)")
