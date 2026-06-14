# SEO Auditor

Автоматизированный инструмент SEO-аудита сайта по URL главной страницы. Работает без участия человека: краулит сайт, проверяет технические параметры, запускает LLM-анализ и генерирует отчёт в MD и HTML.

---

## Структура проекта

```
seo_auditor/
├── main.py           # точка входа, CLI
├── crawler.py        # BFS-краулер (robots.txt, sitemap.xml, ссылки)
├── extractor.py      # парсинг HTML, извлечение мета-данных
├── analyzer.py       # локальные проверки без LLM + plugin loader
├── llm_client.py     # OpenAI-совместимый клиент (логика + качество текста)
├── reporter.py       # генерация MD и HTML отчёта
├── config.yaml       # настройки краулера, анализа, LLM
├── requirements.txt
├── scripts/
│   ├── __init__.py
│   ├── perf_checker.py   # HTTP-метрики (TTFB, размер, gzip)
│   └── keywords.py       # заглушка анализа ключевых слов (плагин)
└── audits/
    └── <slug>_<date>/    # результаты каждого аудита
        ├── report.md
        ├── report.html
        ├── raw_data.json
        ├── aggregated.json
        └── pages/        # HTML страниц (если включено)
```

---

## Что проверяет

**Без LLM (локально):**
- HTTPS, доступность страниц
- Title, description, viewport, robots meta — наличие и длина
- H1–H6: иерархия, количество H1 на страницу, сходство title↔H1
- Canonical: наличие, соответствие URL
- Open Graph (title, description, image)
- Twitter Cards, lang, favicon
- Schema.org (JSON-LD)
- Alt у изображений (отсутствие и пустой alt), размеры img
- Дубли title / description / H1
- Битые ссылки (внутренние и внешние)
- robots.txt / sitemap: покрытие, пробелы
- Внутренняя перелинковка: сироты, слабые страницы
- Mixed content, noopener на target=_blank
- Счётчики аналитики (Яндекс.Метрика, GA, GTM и др.)
- Производительность (TTFB, размер, gzip/br, пороги slow/large)
- Структура URL (глубина, длина, читаемость)
- SEO-балл 0–100, плагин keywords (покрытие мета-тегов)

**С LLM (2 запроса):**
- Логичность структуры: анализ title + description + H1–H3 всех страниц
- Качество текстов: анализ содержимого до 10 страниц

---

## Установка

```bash
pip install -r seo_auditor/requirements.txt
```

**API-ключ LLM** — задать в переменной окружения (рекомендуется):
```bash
export SEO_LLM_API_KEY=sk-...        # Linux/macOS
set SEO_LLM_API_KEY=sk-...           # Windows CMD
$env:SEO_LLM_API_KEY="sk-..."        # Windows PowerShell
```
Или прописать в `config.yaml` → `llm.api_key`.

---

## Запуск

```bash
cd seo_auditor
```

**Базовый запуск:**
```bash
python main.py https://example.com
```

**Без LLM-анализа (быстро, только локальные проверки):**
```bash
python main.py https://example.com --no-llm
```

**Ограничить количество страниц и глубину:**
```bash
python main.py https://example.com --max-pages 10 --max-depth 2
```

**Указать другой конфиг:**
```bash
python main.py https://example.com --config /path/to/config.yaml
```

**Сохранить отчёты в другую папку:**
```bash
python main.py https://example.com --output-dir /path/to/reports
```

**Комбинированный пример:**
```bash
python main.py https://example.com --no-llm --max-pages 50 --max-depth 4 --output-dir ../reports
```

Результат сохраняется в `audits/<slug>_<date>/`:
```
audits/example-com_2026-06-09/report.md
audits/example-com_2026-06-09/report.html
```

---

## Конфигурация (`config.yaml`)

| Параметр | По умолчанию | Описание |
|---|---|---|
| `crawl.max_pages` | 20 | Максимум страниц для анализа |
| `crawl.max_depth` | 3 | Глубина обхода ссылок |
| `crawl.timeout` | 15 | Таймаут HTTP-запроса (сек) |
| `crawl.delay` | 0.5 | Задержка между запросами (сек) |
| `analysis.min_word_count` | 300 | Порог «мало текста» |
| `analysis.min_title_len` | 30 | Минимальная длина title |
| `analysis.max_title_len` | 70 | Максимальная длина title |
| `analysis.min_description_len` | 50 | Минимальная длина description |
| `analysis.max_description_len` | 160 | Максимальная длина description |
| `analysis.plugins` | `[]` | Список путей к плагинам-анализаторам |
| `llm.url` | — | URL OpenAI-совместимого API |
| `llm.model` | — | Название модели |
| `llm.max_pages_text_analysis` | 10 | Страниц для LLM-анализа текста |
| `llm.max_prompt_size_kb` | 256 | Лимит размера промпта |
| `llm.temperature` | 0.2 | Температура генерации |

---

## Плагины

Любой Python-файл с функцией `analyze(raw_data, cfg) -> dict` подключается как плагин:

```yaml
# config.yaml
analysis:
  plugins:
    - scripts/keywords.py
```

Результат плагина попадает в `aggregated["plugins"]["keywords"]` и доступен для дальнейшего использования.

Пример готовой заглушки: `scripts/keywords.py` — частотный анализ слов по корпусу сайта.
