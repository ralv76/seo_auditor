"""
Analyzer — локальные проверки без LLM.
Вход: raw_data (список page-объектов от extractor).
Выход: aggregated.json — сводные метрики + список проблем по каждой странице.
"""

import json
import time
import importlib.util
import requests
from pathlib import Path
from urllib.parse import urlparse


ISSUE_CATEGORIES = {
    "NO_HTTPS": "indexability",
    "UNREACHABLE": "indexability",
    "HTTP_404": "indexability",
    "HTTP_5XX": "indexability",
    "REDIRECT": "indexability",
    "NOINDEX_HEADER": "indexability",
    "NOINDEX_META": "indexability",
    "CANONICAL_MISMATCH": "indexability",
    "CANONICAL_EXTERNAL": "indexability",
    "CANONICAL_RELATIVE": "indexability",
    "SITEMAP_404": "indexability",
    "PAGE_NOT_IN_SITEMAP": "indexability",
    "NO_TITLE": "serp",
    "TITLE_SHORT": "serp",
    "TITLE_LONG": "serp",
    "TITLE_NO_BRAND": "serp",
    "TITLE_DUPLICATE_PREFIX": "serp",
    "NO_DESCRIPTION": "serp",
    "DESC_SHORT": "serp",
    "DESC_LONG": "serp",
    "DESC_NO_CTA": "serp",
    "TITLE_H1_MISMATCH": "serp",
    "NO_H1": "content",
    "MULTIPLE_H1": "content",
    "HEADING_ISSUE": "content",
    "LOW_WORD_COUNT": "content",
    "LONG_PARAGRAPHS": "content",
    "NO_LISTS_OR_TABLES": "content",
    "NO_FAQ_BLOCK": "content",
    "TEMPLATE_TEXT_REPETITION": "content",
    "NO_CLEAR_CTA": "conversion",
    "NO_PHONE_OR_MESSENGER": "conversion",
    "NO_PRICE_OR_TERMS": "commercial",
    "NO_PROOF_CASES": "commercial",
    "NO_GUARANTEE_SIGNAL": "commercial",
    "NO_CONTACT_TRUST": "trust",
    "NO_SCHEMA": "schema",
    "SCHEMA_JSONLD_INVALID": "schema",
    "NO_ORGANIZATION_SCHEMA": "schema",
    "NO_BREADCRUMB_SCHEMA": "schema",
    "SERVICE_PAGE_NO_SERVICE_SCHEMA": "schema",
    "ARTICLE_PAGE_NO_ARTICLE_SCHEMA": "schema",
    "IMAGES_NO_ALT": "media",
    "IMAGES_EMPTY_ALT": "media",
    "IMAGES_NO_DIMENSIONS": "media",
    "GENERIC_ANCHORS": "linking",
    "EMPTY_ANCHORS": "linking",
    "ORPHAN_PAGE": "linking",
    "WEAK_INTERNAL_LINKS": "linking",
    "SLOW_TTFB": "performance",
    "LARGE_PAGE": "performance",
}


def _category_for(code: str) -> str:
    return ISSUE_CATEGORIES.get(code, "technical")


def _has_any_schema(page: dict, expected: set[str]) -> bool:
    types = {str(t).lower() for t in page.get("schema_types", [])}
    return bool(types & {t.lower() for t in expected})


def _is_service_page(page: dict) -> bool:
    text = " ".join([
        page.get("title", ""),
        page.get("description", ""),
        " ".join(page.get("headings", {}).get("h1", [])),
    ]).lower()
    markers = ("под ключ", "стоимость", "цена", "заказать", "услуг", "строительство", "проект")
    return any(m in text for m in markers)


def _is_article_page(page: dict) -> bool:
    path = urlparse(page.get("url", "")).path.lower()
    text = (page.get("title", "") + " " + page.get("description", "")).lower()
    markers = ("как ", "зачем", "почему", "советы", "руководство", "ошибки", "сравнение")
    return "blog" in path or any(m in text for m in markers)


def _site_brand(raw_data: list) -> str:
    host = urlparse(raw_data[0]["url"]).netloc if raw_data else ""
    parts = host.replace("www.", "").split(".")
    return parts[0] if parts else ""


# ------------------------------------------------------------------
# Проверка битых ссылок
# ------------------------------------------------------------------

def check_broken_links(raw_data: list, cfg: dict) -> dict:
    """
    Проверяем уникальные внешние + некраулленные внутренние ссылки (с лимитом).
    Возвращает {url: status_code} только для проблемных (не 200/301/302).
    """
    analysis_cfg = cfg.get("analysis", {})
    timeout = analysis_cfg.get("broken_link_timeout", 5)
    max_check = analysis_cfg.get("max_broken_links_check", 200)
    delay = cfg["crawl"].get("delay", 0.5)
    ua = cfg["crawl"].get("user_agent", "SEO-Auditor/1.0")
    session = requests.Session()
    session.headers.update({"User-Agent": ua})

    # собираем все уникальные ссылки
    all_links = set()
    crawled_urls = {p["url"] for p in raw_data}

    for page in raw_data:
        for link in page.get("links_internal", []):
            if link not in crawled_urls:
                all_links.add(link)
        for link in page.get("links_external", []):
            all_links.add(link)

    links_list = sorted(all_links)
    total = len(links_list)
    if total > max_check:
        print(f"  [broken links] Лимит {max_check} из {total} ссылок (остальные пропущены)")
        links_list = links_list[:max_check]
    else:
        print(f"  [broken links] Проверяем {total} некраулленных ссылок...")

    broken = {}
    checked = {}

    for n, link in enumerate(links_list, 1):
        if n % 25 == 0:
            print(f"  [broken links] {n}/{len(links_list)}...", flush=True)
        if link in checked:
            continue
        try:
            r = session.head(link, timeout=timeout, allow_redirects=True)
            status = r.status_code
        except requests.exceptions.Timeout:
            status = 0
            checked[link] = "timeout"
        except Exception as e:
            status = 0
            checked[link] = str(e)[:80]

        checked[link] = status
        if status not in (200, 201, 301, 302, 303, 307, 308):
            broken[link] = status
        time.sleep(delay * 0.5)

    return broken


# ------------------------------------------------------------------
# Проверки по странице
# ------------------------------------------------------------------

def check_page(page: dict, cfg: dict) -> list:
    """Возвращает список проблем [{severity, code, message}]."""
    a = cfg["analysis"]
    issues = []

    def issue(severity, code, msg):
        issues.append({"severity": severity, "code": code, "message": msg})

    url = page["url"]

    # HTTPS
    if not page.get("https"):
        issue("critical", "NO_HTTPS", "Страница не на HTTPS")

    # Статус
    status = page.get("status", 0)
    if status == 0:
        issue("critical", "UNREACHABLE", f"Страница недоступна: {page.get('error', '')}")
        return issues
    if status == 404:
        issue("critical", "HTTP_404", "Страница возвращает 404")
    elif status >= 500:
        issue("critical", "HTTP_5XX", f"Серверная ошибка: HTTP {status}")
    elif status in (301, 302):
        issue("warning", "REDIRECT", f"Редирект на {page.get('final_url')}")

    # Title
    title = page.get("title", "")
    if not title:
        issue("critical", "NO_TITLE", "Отсутствует <title>")
    elif len(title) < a["min_title_len"]:
        issue("warning", "TITLE_SHORT", f"Title слишком короткий: {len(title)} символов (мин. {a['min_title_len']})")
    elif len(title) > a["max_title_len"]:
        issue("warning", "TITLE_LONG", f"Title слишком длинный: {len(title)} символов (макс. {a['max_title_len']})")
    if title and a.get("brand_required_in_title", False):
        brand = a.get("brand_name", "").strip().lower()
        if brand and brand not in title.lower():
            issue("info", "TITLE_NO_BRAND", f"Title не содержит бренд: {a.get('brand_name')}")

    # Description
    desc = page.get("description", "")
    if not desc:
        issue("critical", "NO_DESCRIPTION", "Отсутствует meta description")
    elif len(desc) < a["min_description_len"]:
        issue("warning", "DESC_SHORT", f"Description слишком короткий: {len(desc)} символов (мин. {a['min_description_len']})")
    elif len(desc) > a["max_description_len"]:
        issue("warning", "DESC_LONG", f"Description слишком длинный: {len(desc)} символов (макс. {a['max_description_len']})")
    if desc:
        cta_words = a.get("description_cta_words", ["заказать", "узнать", "рассчитать", "получить", "позвон"])
        if not any(w.lower() in desc.lower() for w in cta_words):
            issue("info", "DESC_NO_CTA", "Description не содержит явного действия/CTA")

    # Keywords
    if not page.get("keywords"):
        issue("info", "NO_KEYWORDS", "Отсутствуют meta keywords")

    # Viewport
    if not page.get("viewport"):
        issue("warning", "NO_VIEWPORT", "Отсутствует meta viewport (проблема с мобильной версией)")
    elif not page.get("viewport_valid"):
        issue("warning", "VIEWPORT_INVALID",
              "Viewport не содержит width=device-width")

    # HTML lang
    if not page.get("html_lang"):
        issue("warning", "NO_HTML_LANG", "Отсутствует атрибут lang на <html>")

    # Favicon
    if not page.get("has_favicon"):
        issue("info", "NO_FAVICON", "Отсутствует favicon")

    # Twitter Cards
    tw = page.get("twitter", {})
    if not tw.get("twitter:card") or not tw.get("twitter:title"):
        issue("info", "TWITTER_INCOMPLETE", "Неполные Twitter Cards (нет card или title)")

    # Canonical
    if not page.get("canonical"):
        issue("info", "NO_CANONICAL", "Отсутствует canonical ссылка")
    elif not page.get("canonical_matches"):
        issue("warning", "CANONICAL_MISMATCH",
              f"Canonical не совпадает с URL страницы: {page.get('canonical')}")
    if page.get("canonical") and not page.get("canonical_absolute"):
        issue("info", "CANONICAL_RELATIVE", "Canonical задан относительным URL")
    if page.get("canonical_external"):
        issue("critical", "CANONICAL_EXTERNAL", f"Canonical указывает на другой домен: {page.get('canonical')}")

    # H1
    h1_count = page.get("h1_count", 0)
    if h1_count == 0:
        issue("critical", "NO_H1", "Отсутствует H1")
    elif h1_count > a["max_h1_per_page"]:
        issue("warning", "MULTIPLE_H1", f"Несколько H1 на странице: {h1_count}")

    # Проблемы иерархии заголовков
    for hi in page.get("heading_issues", []):
        issue("info", "HEADING_ISSUE", hi)

    # Open Graph
    og = page.get("og", {})
    if not og.get("og:title"):
        issue("info", "NO_OG_TITLE", "Отсутствует og:title")
    if not og.get("og:description"):
        issue("info", "NO_OG_DESCRIPTION", "Отсутствует og:description")
    if not og.get("og:image"):
        issue("info", "NO_OG_IMAGE", "Отсутствует og:image")

    # Schema.org
    if not page.get("schema_types"):
        issue("info", "NO_SCHEMA", "Отсутствуют Schema.org разметки")
    if page.get("schema_jsonld_errors", 0) > 0:
        issue("warning", "SCHEMA_JSONLD_INVALID", f"Ошибок JSON-LD: {page.get('schema_jsonld_errors')}")
    if _is_service_page(page) and not _has_any_schema(page, {"Service", "Product", "Offer", "LocalBusiness"}):
        issue("info", "SERVICE_PAGE_NO_SERVICE_SCHEMA", "Коммерческая страница без Service/Product/Offer schema")
    if _is_article_page(page) and not _has_any_schema(page, {"Article", "BlogPosting", "NewsArticle"}):
        issue("info", "ARTICLE_PAGE_NO_ARTICLE_SCHEMA", "Информационная страница без Article schema")
    if not _has_any_schema(page, {"BreadcrumbList"}):
        issue("info", "NO_BREADCRUMB_SCHEMA", "Нет BreadcrumbList schema")

    # Изображения без alt
    without_alt = page.get("images_without_alt", 0)
    if without_alt > 0:
        pct = page.get("images_without_alt_percent", 0)
        issue("warning", "IMAGES_NO_ALT",
              f"{without_alt} изображений без alt ({pct}%)")

    empty_alt = page.get("images_empty_alt", 0)
    if empty_alt > 0:
        issue("warning", "IMAGES_EMPTY_ALT",
              f"{empty_alt} изображений с пустым alt=\"\"")

    img_count = page.get("images_count", 0)
    no_dims = page.get("images_no_dimensions", 0)
    if img_count > 0 and no_dims / img_count > 0.5:
        issue("info", "IMAGES_NO_DIMENSIONS",
              f"{no_dims} из {img_count} изображений без width/height (риск CLS)")

    # Mixed content
    mixed = page.get("mixed_content", [])
    if mixed:
        issue("critical", "MIXED_CONTENT",
              f"HTTP-ресурсы на HTTPS-странице: {len(mixed)} шт.")

    # noopener на _blank
    blank_noopener = page.get("blank_without_noopener", [])
    if blank_noopener:
        issue("warning", "BLANK_NO_NOOPENER",
              f"{len(blank_noopener)} внешних ссылок target=_blank без noopener")

    # Title ↔ H1
    min_sim = a.get("title_h1_min_similarity", 30)
    h1_list = page.get("headings", {}).get("h1", [])
    if title and h1_list and page.get("title_h1_similarity", 100) < min_sim:
        issue("warning", "TITLE_H1_MISMATCH",
              f"Title и H1 слабо связаны (similarity {page.get('title_h1_similarity')}%)")

    # Cache headers
    if status == 200 and not page.get("cache_control"):
        issue("info", "NO_CACHE_HEADERS", "Отсутствует заголовок Cache-Control")

    # Объём текста
    wc = page.get("word_count", 0)
    if wc < a["min_word_count"]:
        issue("warning", "LOW_WORD_COUNT",
              f"Мало текста: {wc} слов (мин. {a['min_word_count']})")

    stats = page.get("content_stats", {})
    if stats.get("long_paragraphs_count", 0) > a.get("max_long_paragraphs", 3):
        issue("warning", "LONG_PARAGRAPHS",
              f"Слишком много длинных абзацев: {stats.get('long_paragraphs_count')}")
    if wc >= a["min_word_count"] and stats.get("lists_count", 0) == 0 and stats.get("tables_count", 0) == 0:
        issue("info", "NO_LISTS_OR_TABLES", "Нет списков/таблиц: текст сложно сканировать")
    if _is_article_page(page) and stats.get("faq_like_count", 0) < a.get("min_faq_mentions", 2):
        issue("info", "NO_FAQ_BLOCK", "Нет заметного FAQ/вопросно-ответного блока")
    if stats.get("cta_count", 0) == 0:
        issue("warning", "NO_CLEAR_CTA", "Нет явного CTA на странице")
    if stats.get("phone_count", 0) == 0 and stats.get("messenger_links_count", 0) == 0:
        issue("warning", "NO_PHONE_OR_MESSENGER", "Нет телефона или ссылки на мессенджер")
    if _is_service_page(page) and stats.get("price_mentions", 0) == 0:
        issue("warning", "NO_PRICE_OR_TERMS", "Коммерческая страница без цены/стоимости/условий")
    if _is_service_page(page) and stats.get("guarantee_mentions", 0) == 0:
        issue("info", "NO_GUARANTEE_SIGNAL", "Нет сигналов гарантий/сроков/договора")
    if _is_service_page(page) and stats.get("proof_mentions", 0) == 0:
        issue("info", "NO_PROOF_CASES", "Нет доказательств: кейсы, фото, отзывы, примеры")
    if _is_service_page(page) and stats.get("trust_mentions", 0) == 0:
        issue("info", "NO_CONTACT_TRUST", "Нет явных trust-сигналов: ИНН/ОГРН/аккредитация/эскроу")

    anchors = page.get("anchor_stats", {})
    if anchors.get("anchors_total", 0):
        generic_pct = anchors.get("anchors_generic", 0) / anchors["anchors_total"] * 100
        if generic_pct > a.get("generic_anchor_warn_pct", 25):
            issue("info", "GENERIC_ANCHORS", f"Много общих anchor-текстов: {generic_pct:.1f}%")
    if anchors.get("anchors_empty", 0) > 0:
        issue("info", "EMPTY_ANCHORS", f"Пустые ссылки без текста: {anchors.get('anchors_empty')}")

    # x-robots-tag
    xrt = page.get("x_robots_tag", "")
    if xrt and "noindex" in xrt.lower():
        issue("critical", "NOINDEX_HEADER", f"x-robots-tag: {xrt}")

    robots_meta = page.get("robots_meta", "")
    if robots_meta and "noindex" in robots_meta.lower():
        issue("critical", "NOINDEX_META", f"meta robots: {robots_meta}")

    return issues


def _normalize_url(url: str) -> str:
    return url.rstrip("/")


def analyze_robots_sitemap(audit_dir: Path, raw_data: list, cfg: dict) -> dict:
    """Аудит robots.txt и sitemap на основе crawled_urls.json."""
    crawled_path = audit_dir / "crawled_urls.json"
    result = {
        "robots_txt_found": False,
        "disallow_rules_count": 0,
        "sitemap_found": False,
        "sitemap_urls_count": 0,
        "sitemap_under_crawled": False,
        "in_sitemap_not_crawled": [],
        "crawled_not_in_sitemap": [],
    }
    if not crawled_path.exists():
        return result

    data = json.loads(crawled_path.read_text(encoding="utf-8"))
    result["robots_txt_found"] = data.get("robots_txt_found", False)
    result["sitemap_urls_count"] = data.get("sitemap_urls_count", 0)
    result["sitemap_found"] = result["sitemap_urls_count"] > 0

    robots_path = audit_dir / "robots.txt"
    if robots_path.exists():
        disallow = 0
        for line in robots_path.read_text(encoding="utf-8").splitlines():
            if line.strip().lower().startswith("disallow:") and line.split(":", 1)[1].strip():
                disallow += 1
        result["disallow_rules_count"] = disallow

    sitemap_urls = {_normalize_url(u) for u in data.get("sitemap_urls", [])}
    crawled_urls = {_normalize_url(p["url"]) for p in raw_data}
    result["in_sitemap_not_crawled"] = sorted(sitemap_urls - crawled_urls)[:20]
    result["crawled_not_in_sitemap"] = sorted(crawled_urls - sitemap_urls)[:20]
    max_pages = cfg["crawl"].get("max_pages", 20)
    result["sitemap_under_crawled"] = (
        result["sitemap_urls_count"] > max_pages
    )
    return result


def compute_internal_linking(raw_data: list, cfg: dict) -> dict:
    """Входящие/исходящие внутренние ссылки, сироты."""
    min_out = cfg["analysis"].get("min_internal_outgoing", 3)
    incoming = {p["url"]: 0 for p in raw_data}
    crawled = {_normalize_url(p["url"]) for p in raw_data}

    for page in raw_data:
        for link in page.get("links_internal", []):
            norm = _normalize_url(link)
            for url in incoming:
                if _normalize_url(url) == norm:
                    incoming[url] += 1
                    break

    orphans = []
    weak_outgoing = []
    for page in raw_data:
        url = page["url"]
        inc = incoming.get(url, 0)
        out = page.get("links_internal_count", 0)
        page["internal_incoming_count"] = inc
        is_home = urlparse(url).path in ("", "/")
        if inc == 0 and not is_home:
            orphans.append(url)
        if out < min_out and page.get("status") == 200:
            weak_outgoing.append({"url": url, "outgoing": out})

    total_ext = sum(p.get("links_external_count", 0) for p in raw_data)
    total_nofollow = sum(len(p.get("links_nofollow", [])) for p in raw_data)
    nofollow_pct = round(total_nofollow / total_ext * 100, 1) if total_ext else 0.0

    return {
        "orphan_pages": orphans,
        "orphan_count": len(orphans),
        "weak_outgoing": weak_outgoing[:20],
        "weak_outgoing_count": len(weak_outgoing),
        "nofollow_external_count": total_nofollow,
        "nofollow_external_percent": nofollow_pct,
    }


def apply_robots_sitemap_issues(raw_data: list, robots_data: dict):
    """Сайт-уровневые issues для robots/sitemap."""
    if not robots_data.get("robots_txt_found"):
        for page in raw_data:
            page["issues"].append({
                "severity": "warning", "code": "NO_ROBOTS",
                "message": "robots.txt не найден",
            })
    if not robots_data.get("sitemap_found"):
        for page in raw_data:
            page["issues"].append({
                "severity": "warning", "code": "NO_SITEMAP",
                "message": "sitemap.xml не найден или пуст",
            })
    if robots_data.get("sitemap_under_crawled"):
        msg = (f"sitemap содержит {robots_data['sitemap_urls_count']} URL, "
               f"краулер обошёл меньше — возможен неполный аудит")
        for page in raw_data:
            page["issues"].append({
                "severity": "info", "code": "SITEMAP_GAP", "message": msg,
            })
    for page in raw_data:
        norm = _normalize_url(page["url"])
        not_in_sm = [_normalize_url(u) for u in robots_data.get("crawled_not_in_sitemap", [])]
        if norm in not_in_sm and robots_data.get("sitemap_found"):
            page["issues"].append({
                "severity": "info", "code": "PAGE_NOT_IN_SITEMAP",
                "message": "Страница не найдена в sitemap",
            })


def apply_internal_linking_issues(raw_data: list, linking: dict):
    orphan_set = set(linking.get("orphan_pages", []))
    weak_map = {w["url"]: w["outgoing"] for w in linking.get("weak_outgoing", [])}
    for page in raw_data:
        if page["url"] in orphan_set:
            page["issues"].append({
                "severity": "warning", "code": "ORPHAN_PAGE",
                "message": "Нет входящих внутренних ссылок (сирота)",
            })
        if page["url"] in weak_map:
            page["issues"].append({
                "severity": "info", "code": "WEAK_INTERNAL_LINKS",
                "message": f"Мало исходящих внутренних ссылок: {weak_map[page['url']]}",
            })


def apply_perf_issues(raw_data: list, perf: dict, cfg: dict):
    """Добавить SLOW_TTFB / LARGE_PAGE из perf_checker."""
    if not perf:
        return
    ttfb_warn = cfg["analysis"].get("ttfb_warn_ms", 500)
    size_warn_kb = cfg["analysis"].get("page_size_warn_kb", 1500)
    perf_by_url = {p["url"]: p for p in perf.get("pages", [])}
    for page in raw_data:
        pm = perf_by_url.get(page["url"])
        if not pm:
            continue
        if pm.get("slow_ttfb"):
            page["issues"].append({
                "severity": "warning", "code": "SLOW_TTFB",
                "message": f"TTFB {pm.get('ttfb_ms')} мс (порог {ttfb_warn} мс)",
            })
        if pm.get("large_page"):
            size_kb = round(pm.get("size_bytes", 0) / 1024, 1)
            page["issues"].append({
                "severity": "warning", "code": "LARGE_PAGE",
                "message": f"Размер страницы {size_kb} КБ (порог {size_warn_kb} КБ)",
            })


def compute_seo_score(aggregated: dict) -> dict:
    """Сводный SEO-балл 0–100."""
    score = 100
    iss = aggregated.get("issues_summary", {})
    score -= min(iss.get("critical", 0) * 10, 50)
    score -= min(iss.get("warning", 0) * 3, 30)
    score -= min(iss.get("info", 0) * 1, 15)

    s = aggregated.get("summary", {})
    bonuses = []
    if s.get("pages_https") == s.get("pages_total") and s.get("pages_total", 0) > 0:
        bonuses.append("100% HTTPS")
    if aggregated.get("links", {}).get("broken_links_count", 0) == 0:
        bonuses.append("нет битых ссылок")
    perf = aggregated.get("performance", {}).get("summary", {})
    measured = perf.get("pages_measured", 0)
    if measured and perf.get("pages_with_gzip") == measured:
        bonuses.append("сжатие на всех страницах")
        score = min(100, score + 3)

    score = max(0, min(100, score))
    if score >= 80:
        grade = "A"
    elif score >= 60:
        grade = "B"
    elif score >= 40:
        grade = "C"
    else:
        grade = "D"
    return {"value": score, "grade": grade, "bonuses": bonuses}


def compute_category_summary(raw_data: list) -> dict:
    summary = {}
    for page in raw_data:
        for iss in page.get("issues", []):
            cat = _category_for(iss.get("code", ""))
            sev = iss.get("severity", "info")
            if cat not in summary:
                summary[cat] = {"critical": 0, "warning": 0, "info": 0, "total": 0}
            summary[cat][sev] = summary[cat].get(sev, 0) + 1
            summary[cat]["total"] += 1
    return summary


def compute_professional_scores(aggregated: dict) -> dict:
    """Отдельные score по ключевым зонам аудита."""
    categories = aggregated.get("issue_categories", {})
    mapping = {
        "technical": ["technical", "indexability", "performance"],
        "serp": ["serp", "schema"],
        "content": ["content"],
        "commercial": ["commercial", "trust", "conversion"],
        "linking": ["linking"],
        "media": ["media"],
    }

    def score_for(cats):
        crit = sum(categories.get(c, {}).get("critical", 0) for c in cats)
        warn = sum(categories.get(c, {}).get("warning", 0) for c in cats)
        info = sum(categories.get(c, {}).get("info", 0) for c in cats)
        score = 100 - min(crit * 12, 60) - min(warn * 4, 45) - min(info * 1.2, 25)
        return max(0, round(score, 1))

    scores = {
        "technical": score_for(mapping["technical"]),
        "serp": score_for(mapping["serp"]),
        "content": score_for(mapping["content"]),
        "commercial": score_for(mapping["commercial"]),
        "linking": score_for(mapping["linking"]),
        "media": score_for(mapping["media"]),
    }
    scores["overall"] = round(sum(scores.values()) / len(scores), 1) if scores else 0
    return scores


def apply_sitewide_quality_issues(raw_data: list, cfg: dict):
    """Проверки, где нужен контекст всего сайта."""
    if not raw_data:
        return
    brand = cfg.get("analysis", {}).get("brand_name") or _site_brand(raw_data)
    cfg.setdefault("analysis", {})["brand_name"] = brand

    title_prefixes = {}
    desc_prefixes = {}
    h1_prefixes = {}
    for page in raw_data:
        title = (page.get("title") or "").strip().lower()
        desc = (page.get("description") or "").strip().lower()
        h1_list = page.get("headings", {}).get("h1", [])
        h1 = (h1_list[0] if h1_list else "").strip().lower()
        if title:
            title_prefixes.setdefault(title[:35], []).append(page["url"])
        if desc:
            desc_prefixes.setdefault(desc[:45], []).append(page["url"])
        if h1:
            h1_prefixes.setdefault(h1[:30], []).append(page["url"])

    repeated_title_urls = {u for urls in title_prefixes.values() if len(urls) >= 5 for u in urls}
    repeated_desc_urls = {u for urls in desc_prefixes.values() if len(urls) >= 5 for u in urls}
    repeated_h1_urls = {u for urls in h1_prefixes.values() if len(urls) >= 5 for u in urls}

    has_org_schema = any(_has_any_schema(p, {"Organization", "LocalBusiness"}) for p in raw_data)
    for page in raw_data:
        url = page["url"]
        if url in repeated_title_urls:
            page["issues"].append({
                "severity": "info",
                "code": "TITLE_DUPLICATE_PREFIX",
                "message": "Повторяющийся шаблон начала title на группе страниц",
            })
        if url in repeated_desc_urls:
            page["issues"].append({
                "severity": "info",
                "code": "DESC_DUPLICATE_PREFIX",
                "message": "Повторяющийся шаблон начала description на группе страниц",
            })
        if url in repeated_h1_urls:
            page["issues"].append({
                "severity": "info",
                "code": "TEMPLATE_TEXT_REPETITION",
                "message": "Похоже на шаблонную генерацию H1/контента для группы страниц",
            })
        if not has_org_schema and urlparse(url).path in ("", "/"):
            page["issues"].append({
                "severity": "info",
                "code": "NO_ORGANIZATION_SCHEMA",
                "message": "На сайте не найдена Organization/LocalBusiness schema",
            })


# ------------------------------------------------------------------
# Основная функция агрегации
# ------------------------------------------------------------------

def analyze(raw_data: list, cfg: dict, audit_dir: Path) -> dict:

    # --- Проверка битых ссылок ---
    print("  [analyzer] Проверка битых ссылок...")
    broken_links = check_broken_links(raw_data, cfg)

    # --- robots/sitemap и перелинковка ---
    print("  [analyzer] robots/sitemap и перелинковка...")
    robots_sitemap = analyze_robots_sitemap(audit_dir, raw_data, cfg)
    internal_linking = compute_internal_linking(raw_data, cfg)

    # --- Добавляем issues к каждой странице ---
    print("  [analyzer] Локальные проверки страниц...")
    for page in raw_data:
        page["issues"] = check_page(page, cfg)

    apply_robots_sitemap_issues(raw_data, robots_sitemap)
    apply_internal_linking_issues(raw_data, internal_linking)

    # --- Поиск дублей ---
    titles = {}
    descriptions = {}
    h1s = {}

    for page in raw_data:
        url = page["url"]
        t = page.get("title", "")
        d = page.get("description", "")
        h1_list = page.get("headings", {}).get("h1", [])
        h1 = h1_list[0] if h1_list else ""

        if t:
            titles.setdefault(t, []).append(url)
        if d:
            descriptions.setdefault(d, []).append(url)
        if h1:
            h1s.setdefault(h1, []).append(url)

    duplicate_titles = {t: urls for t, urls in titles.items() if len(urls) > 1}
    duplicate_descriptions = {d: urls for d, urls in descriptions.items() if len(urls) > 1}
    duplicate_h1s = {h: urls for h, urls in h1s.items() if len(urls) > 1}

    # добавляем issues за дубли
    dup_title_urls = {url for urls in duplicate_titles.values() for url in urls}
    dup_desc_urls = {url for urls in duplicate_descriptions.values() for url in urls}
    dup_h1_urls = {url for urls in duplicate_h1s.values() for url in urls}

    for page in raw_data:
        url = page["url"]
        if url in dup_title_urls:
            page["issues"].append({
                "severity": "warning", "code": "DUPLICATE_TITLE",
                "message": "Дублирующийся title (совпадает с другими страницами)"
            })
        if url in dup_desc_urls:
            page["issues"].append({
                "severity": "warning", "code": "DUPLICATE_DESC",
                "message": "Дублирующийся description"
            })
        if url in dup_h1_urls:
            page["issues"].append({
                "severity": "info", "code": "DUPLICATE_H1",
                "message": "Дублирующийся H1 (совпадает с другими страницами)"
            })

    apply_sitewide_quality_issues(raw_data, cfg)

    # --- Агрегация метрик ---
    total = len(raw_data)
    ok_pages = [p for p in raw_data if p.get("status", 0) == 200]
    n_ok = len(ok_pages)

    def pct(count):
        return round(count / total * 100, 1) if total else 0

    # подсчёт по всем страницам
    pages_https = sum(1 for p in raw_data if p.get("https"))
    pages_no_title = sum(1 for p in raw_data if not p.get("title"))
    pages_no_desc = sum(1 for p in raw_data if not p.get("description"))
    pages_no_keywords = sum(1 for p in raw_data if not p.get("keywords"))
    pages_no_viewport = sum(1 for p in raw_data if not p.get("viewport"))
    pages_no_canonical = sum(1 for p in raw_data if not p.get("canonical"))
    pages_canonical_mismatch = sum(1 for p in raw_data if not p.get("canonical_matches"))
    pages_no_h1 = sum(1 for p in raw_data if p.get("h1_count", 0) == 0)
    pages_multi_h1 = sum(1 for p in raw_data if p.get("h1_count", 0) > 1)
    pages_no_schema = sum(1 for p in raw_data if not p.get("schema_types"))
    pages_no_og = sum(1 for p in raw_data if not p.get("og", {}).get("og:title"))
    pages_no_html_lang = sum(1 for p in raw_data if not p.get("html_lang"))
    pages_invalid_viewport = sum(
        1 for p in raw_data
        if p.get("viewport") and not p.get("viewport_valid")
    )
    pages_no_favicon = sum(1 for p in raw_data if not p.get("has_favicon"))
    pages_twitter_incomplete = sum(
        1 for p in raw_data
        if not p.get("twitter", {}).get("twitter:card")
        or not p.get("twitter", {}).get("twitter:title")
    )
    pages_mixed_content = sum(1 for p in raw_data if p.get("mixed_content"))
    pages_low_words = sum(
        1 for p in raw_data
        if p.get("word_count", 0) < cfg["analysis"]["min_word_count"]
    )

    # изображения
    total_images = sum(p.get("images_count", 0) for p in raw_data)
    total_images_no_alt = sum(p.get("images_without_alt", 0) for p in raw_data)
    images_no_alt_pct = round(total_images_no_alt / total_images * 100, 1) if total_images else 0.0

    # ссылки
    total_internal = sum(p.get("links_internal_count", 0) for p in raw_data)
    total_external = sum(p.get("links_external_count", 0) for p in raw_data)

    # слова
    word_counts = [p.get("word_count", 0) for p in raw_data if p.get("word_count", 0) > 0]
    avg_words = round(sum(word_counts) / len(word_counts), 0) if word_counts else 0

    # аналитика
    analytics_sets = {}
    for p in raw_data:
        for a in p.get("analytics", []):
            analytics_sets[a] = analytics_sets.get(a, 0) + 1

    # schema types
    all_schema_types = []
    for p in raw_data:
        all_schema_types.extend(p.get("schema_types", []))
    schema_type_counts = {}
    for t in all_schema_types:
        schema_type_counts[t] = schema_type_counts.get(t, 0) + 1

    # сводка issues
    severity_counts = {"critical": 0, "warning": 0, "info": 0}
    for p in raw_data:
        for iss in p.get("issues", []):
            s = iss.get("severity", "info")
            severity_counts[s] = severity_counts.get(s, 0) + 1

    # краткая таблица страниц для отчёта
    pages_summary = []
    for p in raw_data:
        h1s_list = p.get("headings", {}).get("h1", [])
        og = p.get("og", {})
        pages_summary.append({
            "url": p["url"],
            "status": p.get("status"),
            "title": p.get("title", ""),
            "title_len": p.get("title_len", 0),
            "description": p.get("description", ""),
            "description_len": p.get("description_len", 0),
            "keywords": bool(p.get("keywords")),
            "h1": h1s_list[0] if h1s_list else "",
            "h1_count": p.get("h1_count", 0),
            "canonical": p.get("canonical", ""),
            "canonical_matches": p.get("canonical_matches", True),
            "word_count": p.get("word_count", 0),
            "images_count": p.get("images_count", 0),
            "images_without_alt": p.get("images_without_alt", 0),
            "images_empty_alt": p.get("images_empty_alt", 0),
            "schema_types": p.get("schema_types", []),
            "analytics": p.get("analytics", []),
            "og": og,
            "og_complete": bool(og.get("og:title") and og.get("og:description") and og.get("og:image")),
            "twitter": p.get("twitter", {}),
            "html_lang": p.get("html_lang", ""),
            "viewport_valid": p.get("viewport_valid", False),
            "has_favicon": p.get("has_favicon", False),
            "title_h1_similarity": p.get("title_h1_similarity", 0),
            "internal_incoming_count": p.get("internal_incoming_count", 0),
            "links_internal_count": p.get("links_internal_count", 0),
            "mixed_content_count": len(p.get("mixed_content", [])),
            "anchor_stats": p.get("anchor_stats", {}),
            "content_stats": p.get("content_stats", {}),
            "schema_jsonld_errors": p.get("schema_jsonld_errors", 0),
            "issues_count": len(p.get("issues", [])),
            "issues": p.get("issues", []),
        })

    aggregated = {
        "summary": {
            "pages_total": total,
            "pages_ok_200": n_ok,
            "pages_https": pages_https,
            "pages_https_pct": pct(pages_https),
        },
        "meta": {
            "pages_no_title": pages_no_title,
            "pages_no_description": pages_no_desc,
            "pages_no_keywords": pages_no_keywords,
            "pages_no_viewport": pages_no_viewport,
            "pages_no_canonical": pages_no_canonical,
            "pages_canonical_mismatch": pages_canonical_mismatch,
            "duplicate_titles": duplicate_titles,
            "duplicate_descriptions": duplicate_descriptions,
        },
        "headings": {
            "pages_no_h1": pages_no_h1,
            "pages_multi_h1": pages_multi_h1,
            "duplicate_h1s": duplicate_h1s,
        },
        "content": {
            "avg_word_count": int(avg_words),
            "pages_low_word_count": pages_low_words,
            "min_word_count_threshold": cfg["analysis"]["min_word_count"],
        },
        "images": {
            "total_images": total_images,
            "total_without_alt": total_images_no_alt,
            "without_alt_percent": images_no_alt_pct,
        },
        "links": {
            "total_internal": total_internal,
            "total_external": total_external,
            "broken_links": broken_links,
            "broken_links_count": len(broken_links),
        },
        "schema": {
            "pages_no_schema": pages_no_schema,
            "schema_type_counts": schema_type_counts,
        },
        "og": {
            "pages_no_og_title": pages_no_og,
        },
        "i18n": {
            "pages_no_html_lang": pages_no_html_lang,
            "pages_invalid_viewport": pages_invalid_viewport,
            "pages_no_favicon": pages_no_favicon,
            "pages_twitter_incomplete": pages_twitter_incomplete,
            "pages_mixed_content": pages_mixed_content,
        },
        "robots_sitemap": robots_sitemap,
        "internal_linking": internal_linking,
        "analytics": analytics_sets,
        "issues_summary": severity_counts,
        "issue_categories": compute_category_summary(raw_data),
        "pages": pages_summary,
    }

    # Сохраняем aggregated.json
    agg_path = audit_dir / "aggregated.json"
    agg_path.write_text(
        json.dumps(aggregated, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # Сохраняем обновлённый raw_data с issues
    raw_path = audit_dir / "raw_data.json"
    raw_path.write_text(
        json.dumps(raw_data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print(f"  [analyzer] Критических: {severity_counts['critical']}, "
          f"Важных: {severity_counts['warning']}, "
          f"Рекомендаций: {severity_counts['info']}")
    print(f"  [analyzer] Битых ссылок: {len(broken_links)}")

    # --- Производительность (perf_checker) ---
    try:
        from scripts.perf_checker import analyze as perf_analyze
        aggregated["performance"] = perf_analyze(raw_data, cfg)
        apply_perf_issues(raw_data, aggregated["performance"], cfg)
        print("  [analyzer] Производительность измерена")
    except Exception as e:
        print(f"  [analyzer] perf_checker пропущен: {e}")
        aggregated["performance"] = {}

    # Пересчёт issues после perf
    severity_counts = {"critical": 0, "warning": 0, "info": 0}
    for p in raw_data:
        for iss in p.get("issues", []):
            s = iss.get("severity", "info")
            severity_counts[s] = severity_counts.get(s, 0) + 1
    aggregated["issues_summary"] = severity_counts
    aggregated["issue_categories"] = compute_category_summary(raw_data)
    for ps in aggregated["pages"]:
        for p in raw_data:
            if p["url"] == ps["url"]:
                ps["issues"] = p.get("issues", [])
                ps["issues_count"] = len(ps["issues"])
                break

    # --- Плагины из config.plugins ---
    plugin_results = {}
    plugins = cfg.get("analysis", {}).get("plugins", []) or []
    plugin_base = Path(__file__).parent
    for plugin_path in plugins:
        p = Path(plugin_path)
        if not p.is_absolute():
            p = plugin_base / plugin_path
        if not p.exists():
            print(f"  [analyzer] Плагин не найден: {plugin_path}")
            continue
        try:
            spec = importlib.util.spec_from_file_location(p.stem, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            result = mod.analyze(raw_data, cfg)
            plugin_results[p.stem] = result
            print(f"  [analyzer] Плагин выполнен: {p.stem}")
        except Exception as e:
            print(f"  [analyzer] Ошибка плагина {p.stem}: {e}")
            plugin_results[p.stem] = {"error": str(e)}
    aggregated["plugins"] = plugin_results

    # SEO-балл
    aggregated["score"] = compute_seo_score(aggregated)
    aggregated["professional_scores"] = compute_professional_scores(aggregated)

    # Пересохраняем aggregated.json с новыми данными
    agg_path.write_text(
        json.dumps(aggregated, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    return aggregated
