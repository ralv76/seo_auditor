"""
Extractor — парсинг HTML страниц.
Входные данные: список page-объектов от Crawler.
Выходные данные: raw_data.json — список структурированных записей по каждой странице.
"""

import json
import re
from pathlib import Path
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup


# ------------------------------------------------------------------
# Вспомогательные функции
# ------------------------------------------------------------------

def _text(tag) -> str:
    return tag.get_text(strip=True) if tag else ""


def _attr(tag, attr: str) -> str:
    return (tag.get(attr) or "").strip() if tag else ""


def _count_words(text: str) -> int:
    return len(re.findall(r"\w+", text, re.UNICODE))


def _count_matches(text: str, patterns: list[str]) -> int:
    text_l = text.lower()
    return sum(len(re.findall(p, text_l, re.UNICODE)) for p in patterns)


def _is_external(href: str, base_domain: str) -> bool:
    parsed = urlparse(href)
    if not parsed.scheme:
        return False
    return parsed.netloc != "" and parsed.netloc != base_domain


def _detect_analytics(html: str) -> list[str]:
    """Определяем наличие счётчиков по сигнатурам в HTML."""
    found = []
    checks = {
        "yandex_metrika": ["mc.yandex.ru/metrika", "ym(", "Ya.Metrika"],
        "google_analytics": ["google-analytics.com", "gtag(", "ga(", "UA-"],
        "google_tag_manager": ["googletagmanager.com", "GTM-"],
        "vk_pixel": ["vk.com/js/api/openapi", "VK.Retargeting"],
        "facebook_pixel": ["connect.facebook.net", "fbq("],
        "top_mail_ru": ["top-fwz1.mail.ru", "top.mail.ru"],
    }
    for name, patterns in checks.items():
        if any(p in html for p in patterns):
            found.append(name)
    return found


def _extract_schema_types(soup: BeautifulSoup) -> list[str]:
    """Извлекаем типы Schema.org из JSON-LD блоков."""
    types = []
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
            for t in _walk_schema_types(data):
                types.append(t)
        except Exception:
            pass
    return types


def _extract_schema_errors(soup: BeautifulSoup) -> int:
    errors = 0
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            json.loads(script.string or "")
        except Exception:
            errors += 1
    return errors


def _walk_schema_types(data) -> list[str]:
    types = []
    if isinstance(data, dict):
        t = data.get("@type")
        if isinstance(t, list):
            types.extend(str(x) for x in t)
        elif t:
            types.append(str(t))
        for key in ("@graph", "itemListElement", "mainEntity"):
            if key in data:
                types.extend(_walk_schema_types(data[key]))
    elif isinstance(data, list):
        for item in data:
            types.extend(_walk_schema_types(item))
    return types


def _title_h1_similarity(title: str, h1: str) -> int:
    """Доля общих слов title↔h1 (0–100)."""
    if not title or not h1:
        return 0
    title_words = set(re.findall(r"\w+", title.lower(), re.UNICODE))
    h1_words = set(re.findall(r"\w+", h1.lower(), re.UNICODE))
    if not title_words or not h1_words:
        return 0
    common = title_words & h1_words
    return round(len(common) / max(len(title_words), len(h1_words)) * 100)


def _detect_mixed_content(soup: BeautifulSoup, is_https: bool) -> list[str]:
    """HTTP-ресурсы на HTTPS-странице."""
    if not is_https:
        return []
    mixed = []
    for tag in soup.find_all(["img", "script", "link", "iframe", "source", "video", "audio"]):
        for attr in ("src", "href", "data-src"):
            val = (tag.get(attr) or "").strip()
            if val.startswith("http://"):
                mixed.append(val)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("http://"):
            mixed.append(href)
    return list(dict.fromkeys(mixed))[:20]


def _check_heading_hierarchy(headings: dict) -> list[str]:
    """Проверяем нарушения иерархии заголовков."""
    issues = []
    order = ["h1", "h2", "h3", "h4", "h5", "h6"]
    prev_level = 0
    for level_name in order:
        level_num = int(level_name[1])
        items = headings.get(level_name, [])
        for text in items:
            if not text:
                issues.append(f"Пустой {level_name.upper()}")
            if prev_level > 0 and level_num > prev_level + 1:
                issues.append(
                    f"Пропущен уровень: после H{prev_level} идёт H{level_num}"
                )
                break
        if items:
            prev_level = level_num
    return issues


def _text_blocks_stats(soup: BeautifulSoup) -> dict:
    paragraphs = []
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        wc = _count_words(txt)
        if wc:
            paragraphs.append(wc)
    long_threshold = 80
    return {
        "paragraphs_count": len(paragraphs),
        "avg_paragraph_words": round(sum(paragraphs) / len(paragraphs), 1) if paragraphs else 0,
        "long_paragraphs_count": sum(1 for wc in paragraphs if wc > long_threshold),
        "lists_count": len(soup.find_all(["ul", "ol"])),
        "tables_count": len(soup.find_all("table")),
    }


def _anchor_stats(soup: BeautifulSoup) -> dict:
    generic_patterns = [
        r"\bподробнее\b", r"\bчитать\b", r"\bздесь\b", r"\bсюда\b",
        r"\bперейти\b", r"\bоткрыть\b", r"\bузнать больше\b",
    ]
    empty = 0
    generic = 0
    total = 0
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        total += 1
        text = a.get_text(" ", strip=True).lower()
        if not text:
            empty += 1
            continue
        if any(re.search(p, text, re.UNICODE) for p in generic_patterns):
            generic += 1
    return {"anchors_total": total, "anchors_empty": empty, "anchors_generic": generic}


# ------------------------------------------------------------------
# Основной экстрактор страницы
# ------------------------------------------------------------------

def extract_page(page: dict, base_domain: str) -> dict:
    url = page["url"]
    html = page.get("html", "")
    headers = page.get("headers", {})
    status = page.get("status", 0)

    result = {
        "url": url,
        "final_url": page.get("final_url", url),
        "status": status,
        "redirected": page.get("redirected", False),
        "error": page.get("error"),
        "depth": page.get("depth", 0),
        "https": url.startswith("https://"),

        # Мета
        "title": "",
        "title_len": 0,
        "description": "",
        "description_len": 0,
        "keywords": "",
        "viewport": "",
        "canonical": "",
        "canonical_matches": True,
        "canonical_absolute": False,
        "canonical_external": False,
        "robots_meta": "",
        "x_robots_tag": headers.get("x-robots-tag", ""),

        # Open Graph
        "og": {},

        # Twitter Cards
        "twitter": {},

        # Заголовки
        "headings": {"h1": [], "h2": [], "h3": [], "h4": [], "h5": [], "h6": []},
        "h1_count": 0,
        "heading_issues": [],

        # Ссылки
        "links_internal": [],
        "links_external": [],
        "links_internal_count": 0,
        "links_external_count": 0,
        "links_nofollow": [],
        "anchor_stats": {"anchors_total": 0, "anchors_empty": 0, "anchors_generic": 0},

        # Изображения
        "images": [],
        "images_count": 0,
        "images_without_alt": 0,
        "images_without_alt_percent": 0.0,
        "images_empty_alt": 0,
        "images_no_dimensions": 0,

        # HTML / i18n
        "html_lang": "",
        "hreflang": [],
        "has_favicon": False,
        "viewport_valid": False,
        "title_h1_similarity": 0,
        "mixed_content": [],
        "blank_without_noopener": [],
        "internal_incoming_count": 0,

        # Schema.org
        "schema_types": [],
        "schema_jsonld_errors": 0,

        # Аналитика
        "analytics": [],

        # Контент
        "word_count": 0,
        "text_preview": "",  # первые 500 символов текста
        "content_stats": {
            "paragraphs_count": 0,
            "avg_paragraph_words": 0,
            "long_paragraphs_count": 0,
            "lists_count": 0,
            "tables_count": 0,
            "faq_like_count": 0,
            "cta_count": 0,
            "price_mentions": 0,
            "guarantee_mentions": 0,
            "proof_mentions": 0,
            "trust_mentions": 0,
            "phone_count": 0,
            "email_count": 0,
            "messenger_links_count": 0,
        },

        # Технические
        "content_type": headers.get("content-type", ""),
        "server": headers.get("server", ""),
        "cache_control": headers.get("cache-control", ""),
    }

    if not html:
        return result

    soup = BeautifulSoup(html, "lxml")

    # --- Title ---
    title_tag = soup.find("title")
    result["title"] = _text(title_tag)
    result["title_len"] = len(result["title"])

    # --- Meta tags ---
    def get_meta(name=None, prop=None) -> str:
        if name:
            tag = soup.find("meta", attrs={"name": name})
            return _attr(tag, "content")
        if prop:
            tag = soup.find("meta", attrs={"property": prop})
            return _attr(tag, "content")
        return ""

    result["description"] = get_meta("description")
    result["description_len"] = len(result["description"])
    result["keywords"] = get_meta("keywords")
    result["viewport"] = get_meta("viewport")
    result["viewport_valid"] = "width=device-width" in result["viewport"].lower()
    result["robots_meta"] = get_meta("robots")

    # --- HTML lang / hreflang / favicon ---
    html_tag = soup.find("html")
    result["html_lang"] = (html_tag.get("lang") or html_tag.get("xml:lang") or "").strip() if html_tag else ""
    result["hreflang"] = [
        {"lang": _attr(link, "hreflang"), "href": _attr(link, "href")}
        for link in soup.find_all("link", rel=lambda r: r and "alternate" in r, hreflang=True)
        if _attr(link, "hreflang")
    ]
    result["has_favicon"] = bool(soup.find("link", rel=lambda r: r and "icon" in r))

    # --- Canonical ---
    canon_tag = soup.find("link", rel=lambda r: r and "canonical" in r)
    result["canonical"] = _attr(canon_tag, "href")
    # Сравниваем canonical с фактическим URL (с учётом trailing slash)
    canon_norm = result["canonical"].rstrip("/")
    url_norm = result["final_url"].rstrip("/")
    result["canonical_matches"] = (not result["canonical"]) or (canon_norm == url_norm)
    parsed_canon = urlparse(result["canonical"])
    result["canonical_absolute"] = bool(parsed_canon.scheme and parsed_canon.netloc)
    result["canonical_external"] = bool(parsed_canon.netloc and parsed_canon.netloc != base_domain)

    # --- Open Graph ---
    og_props = ["og:title", "og:description", "og:image", "og:type",
                "og:url", "og:site_name", "og:locale"]
    for prop in og_props:
        val = get_meta(prop=prop)
        if val:
            result["og"][prop] = val

    # --- Twitter Cards ---
    twitter_names = ["twitter:card", "twitter:title", "twitter:description",
                     "twitter:image", "twitter:site"]
    for name in twitter_names:
        val = get_meta(name)
        if val:
            result["twitter"][name] = val

    # --- Headings ---
    for level in ["h1", "h2", "h3", "h4", "h5", "h6"]:
        result["headings"][level] = [
            tag.get_text(strip=True) for tag in soup.find_all(level)
        ]
    result["h1_count"] = len(result["headings"]["h1"])
    result["heading_issues"] = _check_heading_hierarchy(result["headings"])

    # --- Links ---
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        abs_url = urljoin(url, href)
        rel = a.get("rel") or []
        if isinstance(rel, str):
            rel = [rel]
        is_nofollow = "nofollow" in rel
        is_ext = _is_external(abs_url, base_domain)
        target = (a.get("target") or "").lower()
        if target == "_blank" and is_ext:
            has_noopener = "noopener" in rel or "noreferrer" in rel
            if not has_noopener:
                result["blank_without_noopener"].append(abs_url)

        if is_ext:
            result["links_external"].append(abs_url)
            if is_nofollow:
                result["links_nofollow"].append(abs_url)
        else:
            # внутренние — только http/https
            parsed = urlparse(abs_url)
            if parsed.scheme in ("http", "https"):
                result["links_internal"].append(abs_url)

    # дедупликация
    result["links_internal"] = list(dict.fromkeys(result["links_internal"]))
    result["links_external"] = list(dict.fromkeys(result["links_external"]))
    result["links_nofollow"] = list(dict.fromkeys(result["links_nofollow"]))
    result["blank_without_noopener"] = list(dict.fromkeys(result["blank_without_noopener"]))
    result["links_internal_count"] = len(result["links_internal"])
    result["links_external_count"] = len(result["links_external"])
    result["anchor_stats"] = _anchor_stats(soup)

    # --- Images ---
    for img in soup.find_all("img"):
        src = _attr(img, "src")
        alt = img.get("alt")  # None если атрибута нет совсем
        width = _attr(img, "width")
        height = _attr(img, "height")
        result["images"].append({
            "src": src,
            "alt": alt if alt is not None else None,
            "has_alt": alt is not None,
            "alt_empty": alt == "",
            "width": width,
            "height": height,
        })

    result["images_count"] = len(result["images"])
    without_alt = sum(1 for img in result["images"] if not img["has_alt"])
    empty_alt = sum(1 for img in result["images"] if img["alt_empty"])
    no_dims = sum(1 for img in result["images"] if not img["width"] or not img["height"])
    result["images_without_alt"] = without_alt
    result["images_empty_alt"] = empty_alt
    result["images_no_dimensions"] = no_dims
    if result["images_count"] > 0:
        result["images_without_alt_percent"] = round(
            without_alt / result["images_count"] * 100, 1
        )

    # --- Title ↔ H1 similarity ---
    h1_text = result["headings"]["h1"][0] if result["headings"]["h1"] else ""
    result["title_h1_similarity"] = _title_h1_similarity(result["title"], h1_text)

    # --- Mixed content ---
    result["mixed_content"] = _detect_mixed_content(soup, result["https"])

    # --- Schema.org ---
    result["schema_types"] = _extract_schema_types(soup)
    result["schema_jsonld_errors"] = _extract_schema_errors(soup)

    # --- Analytics ---
    result["analytics"] = _detect_analytics(html)

    # --- Word count ---
    # Удаляем script/style перед подсчётом
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    body = soup.find("body")
    text = body.get_text(separator=" ", strip=True) if body else soup.get_text(separator=" ", strip=True)
    result["word_count"] = _count_words(text)
    result["text_preview"] = text[:500]
    content_stats = _text_blocks_stats(soup)
    content_stats.update({
        "faq_like_count": _count_matches(text, [r"\bfaq\b", r"часто задаваем", r"вопрос", r"ответ"]),
        "cta_count": _count_matches(text, [
            r"оставить заявку", r"заказать", r"получить консультац", r"рассчитать",
            r"связаться", r"позвон", r"написать", r"отправить", r"оставьте телефон",
        ]),
        "price_mentions": _count_matches(text, [r"\bцена\b", r"\bстоимость\b", r"\bсмет", r"\bруб", r"\b₽"]),
        "guarantee_mentions": _count_matches(text, [r"гаранти", r"договор", r"фиксирован", r"срок"]),
        "proof_mentions": _count_matches(text, [r"кейс", r"портфолио", r"отзыв", r"пример", r"реализован", r"фото"]),
        "trust_mentions": _count_matches(text, [r"\bинн\b", r"\bогрн\b", r"аккредитац", r"эскроу", r"лиценз"]),
        "phone_count": len(re.findall(r"(?:\+7|8)\s*[\(\- ]?\d{3}[\)\- ]?\s*\d{3}[\- ]?\d{2}[\- ]?\d{2}", text)),
        "email_count": len(re.findall(r"[\w.\-]+@[\w.\-]+\.\w+", text)),
        "messenger_links_count": sum(
            1 for u in result["links_external"]
            if any(host in u.lower() for host in ("t.me", "telegram", "wa.me", "whatsapp", "max.ru"))
        ),
    })
    result["content_stats"] = content_stats

    return result


# ------------------------------------------------------------------
# Обработка всех страниц
# ------------------------------------------------------------------

def extract_all(pages: list, cfg: dict, audit_dir: Path) -> list:
    from urllib.parse import urlparse
    base_domain = urlparse(
        next((p["url"] for p in pages), "")
    ).netloc

    raw_data = []
    for page in pages:
        extracted = extract_page(page, base_domain)
        raw_data.append(extracted)

    # Сохраняем raw_data.json (без полного HTML для экономии места)
    raw_data_path = audit_dir / "raw_data.json"
    raw_data_path.write_text(
        json.dumps(raw_data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    return raw_data
