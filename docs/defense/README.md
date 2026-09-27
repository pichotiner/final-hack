# EFQA — светлая презентация Minimal Gradient

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
