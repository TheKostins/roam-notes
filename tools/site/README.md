# tools/site -- статические файлы HTML-экспорта

- `template.html` -- каркас страницы; `{{…}}` заполняет `roam_html.py`
  (`title`, `site_title`, `site_author`, `root`, `id`, `article`, `aside`,
  `explorer`, `mathjax_macros`, `description`, `canonical`).
  Блок `aside` (оглавление, локальный граф, backlinks) вставляется дважды:
  в правую колонку (desktop) и под статью (планшет/телефон); CSS показывает
  один из них.
- `site.css` -- gruvbox-токены (светлая/тёмная), шрифтовые пары
  (`data-pair` на `<html>`, сайт использует `stix`), раскладка:
  3 колонки ≥1200px, 2 колонки 768–1199px, одна колонка + шторка <768px.
- `site.js` -- тема, шторка, поиск (`search.json`, загружается при фокусе,
  оценка: заголовок > теги > текст), подсветка текущей заметки в проводнике,
  граф на canvas без библиотек (`graph.json`): локальный (соседи до глубины 2,
  не больше 20 узлов) и глобальный (`graph/`, колёсико/перетаскивание/пинч,
  `?id=` подсвечивает узел).  В макете данные графа -- `window.MOCK_GRAPH`.
- `mockup.html` -- визуальный макет одной заметки на тех же css/js
  (переключатель шрифтовых пар остаётся только здесь).  Смотреть:
  `python3 -m http.server 8787 -d tools/site` (есть в `.claude/launch.json`).
- Сам сайт: `python3 tools/roam.py site serve` (порт 8788) или конфигурация
  `site` в `.claude/launch.json` после `site export`.

Шрифты: STIX Two Text (текст и заголовки), IBM Plex Sans (интерфейс),
IBM Plex Mono (код), формулы -- MathJax 4 с шрифтом `mathjax-stix2`.
