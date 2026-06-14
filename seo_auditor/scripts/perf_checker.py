"""
perf_checker.py — HTTP-уровневые метрики производительности.
Не требует Playwright: измеряет TTFB, размер страницы, gzip, редиректы через requests.

Интерфейс плагина:
    analyze(raw_data: list, cfg: dict) -> dict

Возвращает:
    {
        "pages": [
            {
                "url": str,
                "ttfb_ms": float,
                "total_ms": float,
                "size_bytes": int,
                "compressed": bool,
                "redirect_count": int,
                "status": int,
            },
            ...
        ],
        "summary": {
            "avg_ttfb_ms": float,
            "avg_total_ms": float,
            "avg_size_kb": float,
            "pages_with_gzip": int,
            "pages_with_redirect": int,
        }
    }
"""

import time
import requests
from urllib.parse import urlparse


def _measure(url: str, timeout: int, ua: str) -> dict:
    result = {
        "url": url,
        "ttfb_ms": None,
        "total_ms": None,
        "size_bytes": None,
        "compressed": False,
        "redirect_count": 0,
        "status": None,
        "error": None,
    }
    session = requests.Session()
    session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate, br"})

    t0 = time.perf_counter()
    try:
        resp = session.get(url, timeout=timeout, stream=True)
        ttfb = time.perf_counter()
        content = resp.content  # читаем полностью
        t1 = time.perf_counter()

        result["ttfb_ms"] = round((ttfb - t0) * 1000, 1)
        result["total_ms"] = round((t1 - t0) * 1000, 1)
        result["size_bytes"] = len(content)
        result["compressed"] = "gzip" in resp.headers.get("content-encoding", "").lower() or \
                                "br" in resp.headers.get("content-encoding", "").lower()
        result["redirect_count"] = len(resp.history)
        result["status"] = resp.status_code
    except requests.exceptions.Timeout:
        result["error"] = "timeout"
    except Exception as e:
        result["error"] = str(e)[:100]

    return result


def analyze(raw_data: list, cfg: dict) -> dict:
    crawl_cfg = cfg.get("crawl", {})
    analysis_cfg = cfg.get("analysis", {})
    timeout = crawl_cfg.get("timeout", 15)
    ua = crawl_cfg.get("user_agent", "SEO-Auditor/1.0")
    ttfb_warn = analysis_cfg.get("ttfb_warn_ms", 500)
    size_warn_kb = analysis_cfg.get("page_size_warn_kb", 1500)
    size_warn_bytes = size_warn_kb * 1024

    # Измеряем только успешные страницы (status 200), до max_pages
    max_pages = analysis_cfg.get("perf_max_pages", crawl_cfg.get("max_pages", 20))
    if not max_pages:
        max_pages = len(raw_data)
    ok_pages = [p for p in raw_data if p.get("status") == 200][:max_pages]

    print(f"  [perf_checker] Измеряем производительность {len(ok_pages)} страниц...")

    results = []
    for page in ok_pages:
        metrics = _measure(page["url"], timeout, ua)
        if metrics.get("ttfb_ms") is not None:
            metrics["slow_ttfb"] = metrics["ttfb_ms"] > ttfb_warn
        if metrics.get("size_bytes") is not None:
            metrics["large_page"] = metrics["size_bytes"] > size_warn_bytes
        results.append(metrics)

    # Сводка
    valid = [r for r in results if r["ttfb_ms"] is not None]
    avg_ttfb = round(sum(r["ttfb_ms"] for r in valid) / len(valid), 1) if valid else None
    avg_total = round(sum(r["total_ms"] for r in valid) / len(valid), 1) if valid else None
    avg_size = round(sum(r["size_bytes"] for r in valid) / len(valid) / 1024, 1) if valid else None
    gzip_count = sum(1 for r in valid if r["compressed"])
    redirect_count = sum(1 for r in valid if r["redirect_count"] > 0)
    slow_ttfb_count = sum(1 for r in valid if r.get("slow_ttfb"))
    large_page_count = sum(1 for r in valid if r.get("large_page"))

    return {
        "pages": results,
        "summary": {
            "avg_ttfb_ms": avg_ttfb,
            "avg_total_ms": avg_total,
            "avg_size_kb": avg_size,
            "pages_with_gzip": gzip_count,
            "pages_with_redirect": redirect_count,
            "pages_measured": len(valid),
            "pages_slow_ttfb": slow_ttfb_count,
            "pages_large": large_page_count,
            "ttfb_warn_ms": ttfb_warn,
            "page_size_warn_kb": size_warn_kb,
        }
    }
