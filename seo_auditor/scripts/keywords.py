"""
keywords.py — анализ ключевых слов и покрытия мета-тегов.

Плагинный интерфейс:
    analyze(raw_data: list, cfg: dict) -> dict
"""

from collections import Counter
import re


DEFAULT_STOPWORDS = {
    "этот", "этого", "этой", "этом", "который", "которая", "которые",
    "когда", "где", "как", "что", "для", "или", "при", "над", "под",
    "без", "про", "все", "всё", "еще", "ещё", "так", "также", "если",
    "the", "and", "for", "with", "from", "that", "this", "your", "have",
}


def _tokenize(text: str) -> list:
    return re.findall(r"[а-яёa-z]{4,}", text.lower())


def analyze(raw_data: list, cfg: dict) -> dict:
    analysis = cfg.get("analysis", {})
    stopwords = set(analysis.get("stopwords", [])) | DEFAULT_STOPWORDS
    top_n = analysis.get("top_keywords", 20)

    counter = Counter()
    ok_pages = [p for p in raw_data if p.get("status") == 200]

    for page in ok_pages:
        text = page.get("text_preview", "") or ""
        tokens = [t for t in _tokenize(text) if t not in stopwords]
        counter.update(tokens)

    top_words = [w for w, _ in counter.most_common(top_n)]
    top_keywords = [{"word": w, "count": c} for w, c in counter.most_common(top_n)]

    page_coverage = []
    title_with_keyword = 0
    desc_with_keyword = 0

    for page in ok_pages:
        title = (page.get("title") or "").lower()
        desc = (page.get("description") or "").lower()
        title_hits = [w for w in top_words[:10] if w in title]
        desc_hits = [w for w in top_words[:10] if w in desc]
        if title_hits:
            title_with_keyword += 1
        if desc_hits:
            desc_with_keyword += 1
        page_coverage.append({
            "url": page["url"],
            "title_keywords": title_hits,
            "description_keywords": desc_hits,
        })

    total_ok = len(ok_pages) or 1
    meta_coverage_score = round(title_with_keyword / total_ok * 100, 1)

    return {
        "top_keywords": top_keywords,
        "unique_words": len(counter),
        "meta_coverage_score": meta_coverage_score,
        "pages_title_with_top_keyword": title_with_keyword,
        "pages_desc_with_top_keyword": desc_with_keyword,
        "page_coverage": page_coverage[:30],
    }
