---
name: SEO_Auditor
description: Автоматизированный инструмент SEO-аудита сайта по URL главной страницы.
---


# SKILL.md — SEO_Auditor

Файл для развёртывания и использования проекта «SEO Auditor» в среде Claude Code или локально.

Структура проекта уже реализована. Для запуска анализа нужно запустить команды, описанные в п. 5 ниже. 

---

## 1. Что делает проект

Автоматизированный инструмент SEO-аудита сайта по URL главной страницы.

- Краулит сайт (robots.txt, sitemap.xml, внутренние ссылки)
- Проверяет технические параметры (title, description, H1, canonical, OG, Schema.org, битые ссылки, скорость)
- Запускает LLM-анализ структуры и качества текстов
- Формирует отчёты в Markdown, HTML и JSON

---

## 2. Системные требования

| Компонент | Требование |
|---|---|
| Python | 3.11+ |
| ОС | Windows / Linux / macOS |
| Интернет | Обязателен (краулинг + LLM-API) |

---

## 3. Установка

### 3.1. Клонирование

```bash
git clone <URL-репозитория>
cd seo_auditor
```

### 3.2. Установка зависимостей

```bash
pip install -r requirements.txt
```

**Зависимости:**
- `requests` — HTTP-запросы
- `beautifulsoup4` — парсинг HTML
- `pyyaml` — конфигурация
- `jinja2` — шаблоны HTML-отчётов
- `openai` — LLM-клиент

---

## 4. Конфигурация

Основной конфиг — `seo_auditor/config.yaml`.

### 4.1. Настройка LLM (критично)

Параметр `llm.url` должен быть **базовым URL**, без `/chat/completions`:

```yaml
llm:
  url: "https://routerai.ru/api/v1"        # правильно
  # url: "https://routerai.ru/api/v1/chat/completions"  # НЕПРАВИЛЬНО — даст 405
  model: "openai/gpt-5.4-nano"
  api_key: "sk-..."
```

API-ключ можно задать через переменную окружения (приоритетнее конфига):

```bash
# Linux / macOS
export SEO_LLM_API_KEY=sk-...

# Windows CMD
set SEO_LLM_API_KEY=sk-...

# Windows PowerShell
$env:SEO_LLM_API_KEY="sk-..."
```

### 4.2. Лимиты краулера

```yaml
crawl:
  max_pages: 20          # максимум страниц
  max_depth: 3           # глубина обхода
  timeout: 15            # сек
  delay: 0.5             # задержка между запросами
```

---

## 5. Запуск

### 5.1. Базовый запуск (полный аудит с LLM)

```bash
cd seo_auditor
python main.py https://example.com
```

### 5.2. Без LLM (быстро, только локальные проверки)

```bash
python main.py https://example.com --no-llm
```

### 5.3. С кастомными лимитами

```bash
python main.py https://example.com --max-pages 50 --max-depth 4
```

### 5.4. Результат

Отчёты сохраняются в `seo_auditor/audits/<slug>_<date>/`:

```
audits/example-com_2026-06-09/
├── report.md
├── report.html
├── raw_data.json
├── aggregated.json
└── pages/          # HTML-страницы (если включено)
```

---

## 6. Примеры использования

```bash
# Аудит конкурента
python main.py https://zerocoder.ru/ --max-pages 20

# Быстрая проверка без LLM
python main.py https://4info.ru/ --no-llm --max-pages 10

# Тест LLM-подключения
python test_llm.py
```

---

## 7. Структура проекта

```
seo_auditor/
├── main.py              # точка входа, CLI
├── crawler.py           # BFS-краулер
├── extractor.py         # парсинг HTML, извлечение мета-данных
├── analyzer.py          # локальные проверки + plugin loader
├── llm_client.py        # OpenAI-совместимый клиент
├── reporter.py          # генерация MD и HTML
├── config.yaml          # настройки
├── requirements.txt
├── test_llm.py          # диагностика LLM-подключения
├── scripts/
│   ├── perf_checker.py  # HTTP-метрики (TTFB, размер, gzip)
│   └── keywords.py      # заглушка анализа ключевых слов
└── audits/              # результаты аудитов
```

---

## 8. Известные проблемы и решения

| Проблема | Причина | Решение |
|---|---|---|
| `405 Not Allowed` от LLM | `llm.url` содержит `/chat/completions` | Укажите базовый URL: `https://routerai.ru/api/v1` |
| Эмодзи не выводятся в Windows | Кодировка консоли | Запускайте через PowerShell или IDE |
| Большое время аудита | Много битых ссылок или медленный хост | Используйте `--no-llm` и уменьшите `max_pages` |

---

## 9. Расширение

### 9.1. Плагины

Любой Python-файл с функцией `analyze(raw_data, cfg) -> dict` подключается как плагин:

```yaml
# config.yaml
analysis:
  plugins:
    - scripts/keywords.py
```

### 9.2. Настройка HTML-шаблона

Шаблон отчёта: `seo_auditor/` (встроен в `reporter.py`). Можно кастомизировать Jinja2-шаблон.

---

## 10. Проверка работоспособности

Быстрый тест после установки:

```bash
# 1. Проверка LLM
python test_llm.py

# 2. Быстрый аудит без LLM
python main.py https://httpbin.org --no-llm --max-pages 5
```

Если оба шага проходят без ошибок — проект готов к работе.

---

**Связанные файлы:** `README.md`, `INTRO.md`, `CLAUDE.md`
