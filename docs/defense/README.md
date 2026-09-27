# EFQA — материалы для защиты

## Основная защита: 5 минут

- [PowerPoint — 5 минут](5min/presentation-5min.pptx) и [PDF — 5 минут](5min/presentation-5min.pdf): 8 слайдов, Montserrat, маршрут детали и данные P8. Стиль — сдержанная редакционная верстка, технические линейки и таблицы без декоративных градиентов.
- [Превью ключевых слайдов](5min/preview.png).
- [Текст и тайминг](5min/speaker-notes.md) · [источники, методика статистики и ограничения](5min/SOURCES.md).
- Пересборка: `python -m pip install -r docs/defense/5min/requirements.txt`, затем `python docs/defense/5min/build.py`. Montserrat Regular/SemiBold и OFL включены в `5min/fonts/`; в PDF шрифты встроены, для редактируемого PPTX установите при необходимости шрифты из этого каталога.

В публичной выборке четыре **выбранные** потери миссий: 2 с подтверждённым контуром поставки/испытаний и 2 без однозначной атрибуции этапа возникновения. Это не частота брака NASA, SpaceX или Роскосмоса. Подробности и ссылки — в `5min/SOURCES.md`.

## Предыдущая версия: Minimal Gradient, 7 минут

Текущий выбор пользователя — светлый минимализм: белый фон, мягкие розово-оранжевые
градиенты, крупные заголовки и тонкие линии. **11 основных слайдов + страница источников,
ориентировочно 7 минут.**

## Открыть

- **[PowerPoint](presentation-minimal-gradient.pptx)** — редактируемые тексты, схемы и диаграммы.
- **[PDF](presentation-minimal-gradient.pdf)** — экспорт этой же презентации через Microsoft PowerPoint.
- [Превью четырёх слайдов](minimal-gradient-preview.jpg).
- [Текст выступления](speaker-notes-minimal-gradient.md) — также встроен в заметки PPTX.
- `presentation.pptx`, `presentation.pdf`, `speaker-notes.md` — копии текущей светлой версии.
- [Исследование и методика](space-quality-research.md).

### Происхождение оформления

Визуальный ориентир — [Slidesgo / Minimal Gradient Pitch Deck](https://slidesgo.com/theme/minimal-gradient-pitch-deck).
Официальное скачивание потребовало регистрацию. Исходный PPTX Slidesgo **не был
получен**. Эта поставка — самостоятельная адаптация выбранного визуального направления,
а не заполненный оригинальный шаблон Slidesgo. Градиенты созданы программно,
тексты и схемы редактируются. Это также указано в заметках последнего слайда и manifest.

## Проверки

Каждый из 12 слайдов просмотрен отдельным PNG-рендером из Microsoft PowerPoint
в `.local/minimal-gradient-reviewed/`. Обложка и страница источников повторно
просмотрены после правок. Проверены границы текста, наличие русских подписей,
заметок и шести активных ссылок в PDF. Результаты: `minimal-gradient-check.json`.

Числа 289 тестов / 19 браузерных PASS относятся к сохранённому протоколу P8,
а не к новому прогону приложения. Источник статуса системы — основной README
и `docs/architecture/p8-pilot-completion.md`. Диаграмма кейсов имеет знаменатель
5 событий; категории пересекаются и не показывают отраслевую частоту брака.

## Пересборка из корня репозитория — Windows / PowerPoint

```powershell
.\.local\defense-venv\Scripts\python.exe docs/defense/build_minimal_gradient.py
.\docs\defense\export_minimal_gradient.ps1
.\.local\defense-venv\Scripts\python.exe docs/defense/check_minimal_gradient.py --publish
```

Зависимости: `python-pptx`, `Pillow`, `reportlab`, `pymupdf`. Используется Arial
из Windows; экспорт выполняет установленный Microsoft PowerPoint.
Редактируйте `build_minimal_gradient.py`, затем пересобирайте и проверяйте рендер.
PDF отдельно по другой разметке не рисуется.

`build.py`, `slides.json`, `build_from_template.py` и `export_template.ps1` —
предыдущие варианты, не сборщики текущей поставки. `theory.pdf` — ранее собранный
исторический архив. Скринкаст P6: `../demo/walkthrough.webm`.
