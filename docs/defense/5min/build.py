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

NAVY = "102647"
DEEP = "081D38"
BLUE = "215DCF"
TEAL = "007D78"
LIGHT = "EEF5FE"
ICE = "F7FBFF"
MINT = "E1F4EF"
YELLOW = "FFEBC8"
AMBER = "8B4D00"
RED = "A33338"
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


def line(x1, y1, x2, y2, color=BLUE, width=1.6):
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
    rect(0, 0, 960, 540, DEEP if dark else WHITE)
    if not dark:
        rect(0, 0, 10, 540, BLUE)
    ink = WHITE if dark else NAVY
    txt("EFQA  /  КОНТРОЛЬ КАЧЕСТВА", 38, 24, 540, 11, ink, True)
    txt(section, 38, 59, 840, 11, "8BD1FF" if dark else BLUE, True)
    txt(title, 38, 81, 880, 27, ink, True)
    line(38, 496, 922, 496, "387399" if dark else BLUE, 0.9)
    txt("КОСМОХАКАТОН 2026  •  P8: СИНТЕТИЧЕСКИЙ СТЕНД", 38, 506, 750, 9, ink)
    txt(f"{index:02d} / 08", 858, 505, 70, 10, ink, True)
    slide.notes_slide.notes_text_frame.text = f"{seconds} с.\n{note}"


def finish():
    pdf.showPage()


# 01 — the point of the system
begin(1, "ПРОБЛЕМА / 20 С", "От признака — к обоснованному решению", 20,
      "Камера видит признак, но не знает, достаточен ли контроль и что случилось раньше. "
      "EFQA собирает проверяемую историю конкретной детали и отделяет вывод анализатора от решения ОТК.", True)
txt("ДЕТАЛЬ", 48, 175, 180, 18, WHITE, True)
arrow(164, 242, 190, "6ABDF1")
txt("СВИДЕТЕЛЬСТВА", 254, 175, 240, 18, WHITE, True)
arrow(472, 550, 190, "6ABDF1")
txt("ОТК", 565, 175, 120, 18, WHITE, True)
arrow(648, 715, 190, "6ABDF1")
txt("ERP", 730, 175, 100, 18, WHITE, True)
rect(44, 257, 867, 125, "123455", radius=True)
txt("Что проверили?  Где впервые увидели?  Что было известно при выпуске?", 66, 282, 820, 20, WHITE, True)
txt("Если ответ неизвестен — изделие не становится «годным» автоматически.", 66, 328, 817, 17, WHITE)
txt("Закрытый контур  /  заменяемый анализатор  /  человек принимает решение", 48, 431, 840, 15, "8BD1FF")
finish()

# 02 — separate agencies and the event denominator
begin(2, "ОТКРЫТЫЕ РАССЛЕДОВАНИЯ / 45 С", "4 потери миссий ≠ 4 одинаковых брака", 45,
      "В публичной выборке четыре разных неудачных запуска. NASA установило техническую причину двух миссий OCO и Glory: материалы и ложные испытания поставщика Orbital Sciences. "
      "CRS-7 SpaceX: аномалия примерно на 139-й секунде, но альтернативы инициатора сохраняются. Progress MS-04: на 382-й секунде, частицы или сборка насоса — версии. "
      "139 и 382 секунды — моменты проявления в полёте, не моменты появления брака на производстве.")
cards = [
    (38, "NASA  /  OCO + GLORY", "2 отдельные потери", ["2009 и 2011 • Taurus XL", "Материал / испытания поставщика", "Техническая причина установлена NASA"], MINT, TEAL),
    (348, "SPACEX  /  CRS-7", "~139-я секунда", ["Аномалия проявилась в полёте", "Материал / монтаж — альтернативы", "Инициатор не установлен однозначно"], LIGHT, BLUE),
    (658, "РОСКОСМОС  /  МС-04", "382-я секунда", ["Разрушение насосного узла", "Частицы или нарушение сборки", "Публичный вывод: вероятная причина"], YELLOW, AMBER),
]
for x, label, value, details, bg, accent in cards:
    rect(x, 153, 284, 246, bg, radius=True)
    txt(label, x + 18, 170, 255, 12, accent, True)
    txt(value, x + 18, 201, 255, 22, NAVY, True)
    lines(details, x + 18, 255, 261, 12, 36)
txt("139 с / 382 с = полётное проявление, не этап возникновения дефекта", 43, 418, 875, 15, RED, True)
txt("FAA: 63 корректирующих действия по Starship-2023 — это не 63 дефекта.", 43, 446, 864, 11, TEAL, True)
txt("NASA 2019; NASA IRT 2018; Роскосмос / ТАСС 2017; FAA 2023. Ссылки — в приложении.", 43, 471, 867, 10, BLUE)
finish()

# 03 — meaningful frequency while keeping uncertainty visible
begin(3, "СТАТИСТИКА С ОГРАНИЧЕНИЕМ / 35 С", "Где начиналась цепочка: что известно", 35,
      "Из четырёх специально отобранных событий у двух подтверждён материальный и испытательный контур до сборки: это OCO и Glory. "
      "У двух других единая первичная производственная стадия публично не доказана. Таким образом, 2 из 4 — показатель только этой выбранной выборки, а не частота брака отрасли. "
      "Мы различаем этап первого обнаружения, подтверждённое происхождение и неизвестность.")
rect(38, 150, 884, 158, ICE, radius=True)
txt("ЭТАП ВОЗНИКНОВЕНИЯ В ВЫБРАННЫХ 4 СЛУЧАЯХ", 55, 166, 835, 12, BLUE, True)
rect(55, 207, 408, 55, MINT, radius=True)
rect(470, 207, 434, 55, YELLOW, radius=True)
txt("2/4  ПОСТАВКА / ИСПЫТАНИЯ", 69, 218, 382, 16, TEAL, True)
txt("2/4  НЕ УСТАНОВЛЕНО ТОЧНО", 485, 218, 405, 16, AMBER, True)
rect(38, 329, 434, 116, LIGHT, radius=True)
rect(488, 329, 434, 116, LIGHT, radius=True)
txt("ГДЕ НАШЛИ", 56, 345, 355, 14, BLUE, True)
lines(["Пост контроля или стадия полёта.", "Это не доказательство причины."], 56, 371, 400, 14, 26)
txt("ГДЕ ВОЗНИКЛО", 507, 345, 382, 14, BLUE, True)
lines(["Только подтверждённая атрибуция.", "Иначе: версия или неизвестно."], 507, 371, 395, 14, 26)
txt("Выборка намеренно не репрезентативна; мировой рейтинг этапов по ней невозможен.", 40, 462, 890, 11, RED, True)
finish()

# 04 — the user's requested detailed physical + digital route
begin(4, "МАРШРУТ ДЕТАЛИ И ДАННЫХ / 70 С", "Каждая проверка связана с экземпляром и методом", 70,
      "Идём по одной синтетической сборке. ERP даёт задание и версию маршрута; скан фиксирует экземпляр и фактическую установку. "
      "CP-IN проверяет доступную поверхность и назначенный технологом метод материала; после обработки CP-M сопоставляет видимую область, размер и режим станка. "
      "Перед закрытием сборки фиксируем компонент, инструмент и параметры, затем CP-F и нужные невизуальные испытания. "
      "В каждой точке качество, идентичность и применимость метода проверяются: плохой кадр означает повтор, а не годность. "
      "События идут через edge и API в журнал, историю и набор оснований B; контролёр подтверждает дефект отдельно от выпуска. "
      "При доработке новый run и новый цикл обязательных проверок. Outbox отправляет версию решения в ERP и сохраняет ACK. "
      "Физические камеры, edge и ранняя сенсорика — проект для пилота, программный путь P8 работает с эмулятором.")
txt("ФИЗИЧЕСКИЙ МАРШРУТ • ПРОЕКТ КОНТРОЛЬНЫХ ТОЧЕК / СИНТЕТИЧЕСКИЙ ПРИМЕР", 40, 130, 870, 11, BLUE, True)
steps = [
    ("01  ЗАДАНИЕ", ["ERP: ID, маршрут", "состав, ревизия"]),
    ("02  CP-IN", ["скан экземпляра", "вход / материал"]),
    ("03  ОБРАБОТКА", ["run, станок, режим", "время / оператор"]),
    ("04  CP-M", ["осмотр + размер", "после обработки"]),
    ("05  СБОРКА", ["факт установки", "момент / чистота"]),
    ("06  CP-F", ["до закрытия узла", "визуал + доп. метод"]),
    ("07  ОТК", ["проверка полноты", "доработка / выпуск"]),
]
for i, (header, desc) in enumerate(steps):
    x = 39 + i * 128
    rect(x, 170, 116, 106, LIGHT if i % 2 == 0 else MINT, radius=True)
    txt(header, x + 8, 180, 107, 10, TEAL if i % 2 else BLUE, True)
    lines(desc, x + 8, 214, 107, 10, 19)
    if i < 6:
        arrow(x + 116, x + 126, 222)
txt("На каждой CP: item + run + область + метод + качество; пропуск / плохой кадр → повтор", 40, 281, 880, 11, RED, True)
txt("Ветка: признак → ОТК → доработка → новый run / цикл → повторные CP → устранение → gate", 40, 300, 880, 11, TEAL, True)
line(40, 319, 919, 319, BLUE, 1)
txt("ЦИФРОВОЙ ПУТЬ • P8 РЕАЛИЗОВАН СО СТРУКТУРИРОВАННЫМИ СИГНАЛАМИ", 40, 329, 880, 11, BLUE, True)
digital = [
    ("EDGE / ПРИЁМ", "ID, очередь¹", "схема / дубль"),
    ("RAW + КОНТЕКСТ", "2 времени", "изделие / run"),
    ("ОЦЕНКА + B", "контроль / версии", "неизвестность"),
    ("РЕШЕНИЕ ОТК", "confirm → rework", "новый цикл → release"),
    ("OUTBOX → ERP", "версия результата", "ACK / retry"),
]
for i, (header, a, b) in enumerate(digital):
    x = 40 + i * 178
    rect(x, 356, 164, 88, DEEP if i == 3 else ICE, radius=True)
    ink = WHITE if i == 3 else NAVY
    txt(header, x + 10, 366, 153, 10, "8BD1FF" if i == 3 else BLUE, True)
    lines([a, b], x + 10, 393, 148, 10, 19, ink)
    if i < 4:
        arrow(x + 164, x + 175, 400)
txt("¹ Локальная устойчивая очередь edge спроектирована; промышленный edge не проверен.", 42, 462, 872, 10, TEAL)
finish()

# 05 — causal separation and architecture
begin(5, "АРХИТЕКТУРА / 40 С", "Почему это больше, чем камера + таблица", 40,
      "Сигнал анализатора — только наблюдение. API проверяет права, формат и повторы; PostgreSQL в одной транзакции хранит raw, историю, основания решения и outbox. "
      "Граф означает типизированные связи в SQL между деталью, операцией, осмотром и решением, не отдельную графовую базу. "
      "Решение контролёра неизменяемо и ссылается на B. Плохой контроль и неполная привязка блокируют выпуск. "
      "Сильная альтернатива — QMS с полноценным аудитом; проверяем на том же late-event сценарии, а при меньшей цене выбираем её.")
top = [("ERP / MES / PLM", "владельцы задания, run и состава"), ("АНАЛИЗАТОР", "наблюдение + версия, не вердикт"), ("ОТК", "авторизованная команда + B")]
for i, (a, b) in enumerate(top):
    x = 40 + 302 * i
    rect(x, 150, 283, 81, LIGHT, radius=True)
    txt(a, x + 15, 164, 253, 15, BLUE, True)
    txt(b, x + 15, 197, 260, 11)
    line(x + 140, 231, x + 140, 267, BLUE)
rect(40, 266, 884, 127, DEEP, radius=True)
txt("ON-PREMISE ЯДРО + POSTGRESQL", 58, 278, 820, 15, WHITE, True)
txt("Права → схема → идемпотентность → raw → связи → полнота → B → решение → outbox", 58, 314, 831, 15, WHITE)
txt("Вход: событие + occurred_at  /  сервер: received_at  /  дубль: тот же эффект", 58, 353, 824, 12, "8BD1FF")
rect(40, 410, 884, 60, MINT, radius=True)
txt("QMS С АУДИТОМ — СИЛЬНАЯ АЛЬТЕРНАТИВА. ТЕСТ: B «ТОГДА», ПОЗДНИЙ ФАКТ, UNKNOWN, ACK.", 55, 424, 852, 12, TEAL, True)
finish()

# 06 — traceable example and late evidence
begin(6, "РАЗБОР ОДНОЙ ДЕТАЛИ / 40 С", "Поздний сигнал меняет «сейчас», не «тогда»", 40,
      "Синтетическая сборка assembly-001: вход и контроль после обработки не показали признаков, после сборки найдена риска и одновременно известно отклонение станка. "
      "Контролёр подтвердил один дефект и назначил доработку; причину оставил неизвестной. "
      "Позже пришёл осмотр с временем до сборки, который указывает на ту же риску. Старое решение D1 по B1 остаётся в истории, B2 требует пересмотра. "
      "Два наблюдения одной риски и дубли доставки не размножают дефекты. Наличие станочного отклонения не делает оператора виновным.")
rect(39, 150, 422, 273, LIGHT, radius=True)
txt("ТОГДА  /  B1", 56, 167, 365, 16, BLUE, True)
lines(["10:02  Вход: признаков нет", "10:06  После обработки: чисто", "10:08  Отклонение станка", "10:10  Финал: след на вставке", "10:12  ОТК: 1 дефект; причина unknown", "10:13  Назначена доработка"], 56, 209, 380, 13, 32)
arrow(464, 501, 283)
rect(505, 150, 417, 273, MINT, radius=True)
txt("СЕЙЧАС  /  B2", 522, 167, 376, 16, TEAL, True)
lines(["10:20  Дошёл поздний сигнал", "occurred_at = 10:02:30", "След виден ДО сборки", "Гипотеза происхождения изменена", "D1 / B1 сохранены", "review_required → новое решение"], 522, 209, 374, 13, 32)
txt("Не выводим причину из порядка доставки; повтор не удваивает KPI и результат ERP.", 43, 449, 875, 13, RED, True)
finish()

# 07 — what prevents what; realized vs planned
begin(7, "ЭФФЕКТ И ГОТОВНОСТЬ / 35 С", "Останавливаем распространение, проверяем риск раньше", 35,
      "Программно в P8 проверены полный путь через ERP-эмулятор, B, контроль полноты, доработка, повторный контроль, доставка и ACK: 289 тестов и 19 браузерных PASS из сохранённого протокола. "
      "Пилотный физический контур иной: входные испытания и применимость метода назначает технолог, edge связывает ID и буферизует при обрыве, анализатор отдаёт признаки, а раннее предупреждение по режиму запускает адресную проверку. "
      "Вибрация — риск, а не брак; предупреждение не доказывает предотвращённую аварию. Проектируем теневой пилот и измеряем пропуски, ложные тревоги и время до проверки.")
rows = [
    ("ПОСТАВКА / МАТЕРИАЛ", "происхождение + требуемое испытание", "проект пилота"),
    ("ОБРАБОТКА / СБОРКА", "run + режим + контроль до закрытия", "частично P8 / сенсоры проект"),
    ("ПРОПУСК / ПЛОХОЙ КАДР", "явный пробел → повтор; gate выпуска", "проверено P8"),
    ("ПОЗДНИЙ ФАКТ / ДУБЛЬ", "B тогда/сейчас + review + стабильный ID", "проверено P8"),
]
for i, (risk, mechanism, status) in enumerate(rows):
    y = 151 + 69 * i
    rect(40, y, 883, 60, ICE if i % 2 == 0 else LIGHT, radius=True)
    txt(risk, 53, y + 9, 296, 12, BLUE, True)
    txt(mechanism, 53, y + 33, 617, 13)
    txt(status, 677, y + 23, 235, 10, TEAL if "P8" in status else AMBER, True)
txt("Пилот: 1 участок → теневой режим → экспертная сверка → допуск ОТК и технолога", 43, 445, 876, 13, NAVY, True)
finish()

# 08 — decisive close and main evidence anchors
begin(8, "ИТОГ / 15 С", "Доказательное качество — это цепочка решений", 15,
      "Результат: мы показываем не просто зелёную лампу, а что проверено, каким методом и на каком основании подписан выпуск. "
      "Следующий шаг роли один — измерить цену replay и полноту зависимостей; роли два — совместно с технологом подтвердить ID, точки и методы для одного теневого участка. "
      "Публикации расследований и методика выборки — в notes и приложении; P8 доказан только на синтетическом стенде.", True)
txt("01  Видим признаки и предел метода", 48, 168, 870, 22, WHITE, True)
txt("02  Сохраняем факты, B и человеческое решение", 48, 218, 870, 22, WHITE, True)
txt("03  Не выпускаем изделие при неизвестном контроле", 48, 268, 875, 22, WHITE, True)
rect(45, 363, 867, 80, "123455", radius=True)
txt("Следующий шаг: один участок → теневой пилот → измеренный эффект.", 62, 382, 822, 18, WHITE, True)
txt("Источники: NASA IRT (2018); NASA (2019); Роскосмос / ТАСС (2017); docs/defense/5min/SOURCES.md", 48, 458, 878, 10, "8BD1FF")
finish()

pdf.save()
prs.save(str(HERE / "presentation-5min.pptx"))
print("Built presentation-5min.pptx and presentation-5min.pdf (8 slides, 300 seconds)")
