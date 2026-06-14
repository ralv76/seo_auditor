"""
Reporter — генерация MD и HTML отчёта.
Вход: aggregated.json + LLM-ответы.
Выход: report_YYYY-MM-DD_HHMMSS.md / .html в audit_dir.
"""

import json
import re
import html as html_module
from pathlib import Path
from datetime import date, datetime
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape


# ------------------------------------------------------------------
# Вспомогательные функции форматирования
# ------------------------------------------------------------------

def _status_icon(ok: bool) -> str:
    return "✅" if ok else "❌"


def _severity_icon(s: str) -> str:
    return {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(s, "⚪")


def _pct_bar(pct: float, width: int = 10) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled) + f" {pct:.1f}%"


def _score_status(score) -> str:
    if score is None:
        return "—"
    if score >= 80:
        return "сильная зона"
    if score >= 60:
        return "нужно улучшить"
    if score >= 40:
        return "риск"
    return "критично"


def _category_label(cat: str) -> str:
    labels = {
        "technical": "Техническое SEO",
        "indexability": "Индексация",
        "serp": "Сниппеты и meta",
        "schema": "Schema.org",
        "content": "Качество контента",
        "commercial": "Коммерческая полнота",
        "trust": "Доверие / E-E-A-T",
        "conversion": "Конверсия / CTA",
        "linking": "Перелинковка",
        "media": "Медиа",
        "performance": "Производительность",
    }
    return labels.get(cat, cat)


def _issues_by_severity(issues: list, severity: str) -> list:
    return [i for i in issues if i.get("severity") == severity]


def _uniq_paths(paths: list, limit: int = 50) -> list:
    seen = set()
    out = []
    for p in paths:
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= limit:
            break
    return out


def build_top_changes(
    aggregated: dict,
    llm: dict,
    base_url: str = "",
    limit: int = 20,
) -> dict:
    """TOP-N изменений: что исправить + список страниц."""
    base = base_url.rstrip("/")

    def url_short(url: str) -> str:
        if url.startswith("/"):
            return url
        return url.replace(base, "") or "/"

    candidates = []

    def add(task: str, urls: list, severity: str = "warning", source: str = "audit", sort_key: int = 100):
        urls = _uniq_paths([url_short(u) for u in urls if u])
        if not task:
            return
        if not urls and severity not in ("critical",):
            return
        candidates.append({
            "task": task,
            "urls": urls,
            "severity": severity,
            "source": source,
            "_sort": sort_key,
            "_key": task[:100].lower(),
        })

    grouped = {}
    for p in aggregated.get("pages", []):
        us = url_short(p["url"])
        for iss in p.get("issues", []):
            code = iss["code"]
            if code not in grouped:
                grouped[code] = {
                    "severity": iss["severity"],
                    "message": iss["message"],
                    "urls": [],
                }
            if us not in grouped[code]["urls"]:
                grouped[code]["urls"].append(us)

    for t in llm.get("synthesis", {}).get("top_tasks", []):
        add(
            t.get("task", ""),
            t.get("urls") or [],
            "llm",
            "synthesis",
            10 + int(t.get("priority", 50)),
        )

    for code, data in grouped.items():
        if data["severity"] != "critical":
            continue
        add(data["message"], data["urls"], "critical", code, 0)

    broken = aggregated.get("links", {}).get("broken_links", {})
    bad_links = [u for u, status in broken.items() if status not in (200, 201, 301, 302, 303, 307, 308)]
    if bad_links:
        add("Исправить или удалить битые внешние ссылки", bad_links, "warning", "broken_links", 25)

    warns = [(k, v) for k, v in grouped.items() if v["severity"] == "warning"]
    warns.sort(key=lambda x: -len(x[1]["urls"]))
    for code, data in warns:
        add(data["message"], data["urls"], "warning", code, 100 + max(0, 500 - len(data["urls"])))

    struct = llm.get("structure", {})
    weak_struct = []
    for p in struct.get("pages", []):
        if isinstance(p.get("t_h1"), (int, float)) and p["t_h1"] <= 3:
            weak_struct.append(p.get("path", "/"))
    for batch in struct.get("batches", []):
        for p in batch.get("result", {}).get("pages", []):
            if isinstance(p.get("t_h1"), (int, float)) and p["t_h1"] <= 3:
                weak_struct.append(p.get("path", "/"))
    if weak_struct:
        add(
            "Согласовать title, description и H1 (низкая оценка LLM)",
            weak_struct,
            "warning",
            "llm_structure",
            180,
        )

    text = llm.get("text", {})
    weak_text = list(text.get("priority_urls") or [])
    for p in text.get("pages", []):
        r, s = p.get("read", 5), p.get("seo", 5)
        if (isinstance(r, (int, float)) and r <= 2) or (isinstance(s, (int, float)) and s <= 2):
            weak_text.append(p.get("path", "/"))
    for batch in text.get("batches", []):
        for p in batch.get("result", {}).get("pages", []):
            r, s = p.get("read", 5), p.get("seo", 5)
            if (isinstance(r, (int, float)) and r <= 2) or (isinstance(s, (int, float)) and s <= 2):
                weak_text.append(p.get("path", "/"))
    if weak_text:
        add(
            "Доработать тексты: низкая читаемость или SEO-потенциал (LLM)",
            weak_text,
            "warning",
            "llm_text",
            185,
        )

    infos = [(k, v) for k, v in grouped.items() if v["severity"] == "info"]
    infos.sort(key=lambda x: -len(x[1]["urls"]))
    for code, data in infos:
        add(data["message"], data["urls"], "info", code, 300 + max(0, 200 - len(data["urls"])))

    seen = set()
    unique = []
    candidates.sort(key=lambda x: (x["_sort"], -len(x["urls"])))
    for c in candidates:
        if c["_key"] in seen:
            continue
        seen.add(c["_key"])
        c.pop("_sort", None)
        c.pop("_key", None)
        unique.append(c)
        if len(unique) >= limit:
            break

    for i, c in enumerate(unique, 1):
        c["priority"] = i

    iss = aggregated.get("issues_summary", {})
    syn = llm.get("synthesis", {})
    if syn.get("conclusion"):
        summary = syn["conclusion"]
    else:
        summary = (
            f"Аудит {aggregated.get('summary', {}).get('pages_total', '—')} стр.: "
            f"критических {iss.get('critical', 0)}, важных {iss.get('warning', 0)}. "
            f"Ниже — TOP-{len(unique)} изменений с указанием страниц для правок."
        )

    return {"summary": summary, "changes": unique, "limit": limit}


# ------------------------------------------------------------------
# Генератор MD-отчёта
# ------------------------------------------------------------------

class MDReporter:
    def __init__(self, base_url: str, aggregated: dict, llm: dict, cfg: dict):
        self.base_url = base_url
        self.agg = aggregated
        self.llm = llm
        self.cfg = cfg
        self.domain = urlparse(base_url).netloc
        self.today = date.today().strftime("%Y-%m-%d")
        self.lines = []
        self._limit_notice_shown = False

    def _h(self, level: int, text: str):
        self.lines.append("#" * level + " " + text)

    def _p(self, text: str = ""):
        self.lines.append(text)

    def _table(self, headers: list, rows: list):
        self.lines.append("| " + " | ".join(headers) + " |")
        self.lines.append("|" + "|".join(["---"] * len(headers)) + "|")
        for row in rows:
            self.lines.append("| " + " | ".join(str(c) for c in row) + " |")

    def _hr(self):
        self.lines.append("---")

    # ------------------------------------------------------------------
    # Разделы отчёта
    # ------------------------------------------------------------------

    def _section_header(self):
        self._h(1, f"SEO-аудит {self.domain}")
        self._p(f"**Дата аудита:** {self.today}")
        self._p(f"**URL:** {self.base_url}")
        self._p(f"**Проанализировано страниц:** {self.agg['summary']['pages_total']}")
        self._hr()

    def _section_toc(self):
        self._p("## Содержание\n")
        toc = [
            "0. [Заключение LLM](#0-заключение-llm)",
            "0.1. [Профессиональная сводка](#01-профессиональная-сводка)",
            "0.2. [TOP-20 необходимых изменений](#02-top-20-необходимых-изменений)",
            "1. [Резюме](#1-резюме)",
            "   - 1.1. [SEO-балл](#11-seo-балл)",
            "2. [Технический SEO](#2-технический-seo)",
            "   - 2.1. [Индексация и HTTPS](#21-индексация-и-https)",
            "   - 2.2. [Мета-теги](#22-мета-теги)",
            "   - 2.3. [Заголовки](#23-заголовки)",
            "   - 2.4. [Canonical](#24-canonical)",
            "   - 2.5. [Open Graph и Schema.org](#25-open-graph-и-schemaorg)",
            "   - 2.6. [robots.txt и sitemap](#26-robotstxt-и-sitemap)",
            "   - 2.7. [Twitter Cards и lang](#27-twitter-cards-и-lang)",
            "3. [Контент](#3-контент)",
            "   - 3.1. [Объём текста](#31-объём-текста)",
            "   - 3.2. [Изображения](#32-изображения)",
            "4. [Ссылки](#4-ссылки)",
            "   - 4.1. [Внутренняя перелинковка](#41-внутренняя-перелинковка)",
            "5. [Производительность](#5-производительность)",
            "   - 5.1. [Пороги производительности](#51-пороги-производительности)",
            "6. [Структура URL](#6-структура-url)",
            "7. [Аналитика](#7-аналитика)",
            "8. [Аудит страниц](#8-аудит-страниц)",
            "9. [Анализ логичности структуры (LLM)](#9-анализ-логичности-структуры-llm)",
            "10. [Анализ качества текстов (LLM)](#10-анализ-качества-текстов-llm)",
            "11. [Приоритетные рекомендации](#11-приоритетные-рекомендации)",
            "12. [Ключевые слова](#12-ключевые-слова)",
        ]
        for line in toc:
            self._p(line)
        self._hr()

    def _url_short(self, url: str) -> str:
        return url.replace(self.base_url, "") or "/"

    def _report_pages(self, limit: int = 60) -> list:
        pages = self.agg.get("pages", [])
        problem = [p for p in pages if p.get("issues_count", 0) > 0]
        selected = problem[:limit] if problem else pages[:limit]
        if len(pages) > len(selected) and not self._limit_notice_shown:
            self._p(f"_Показано {len(selected)} из {len(pages)} страниц; полный список сохранён в `aggregated.json`._")
            self._p()
            self._limit_notice_shown = True
        return selected

    def _section_executive(self):
        self._h(2, "0. Заключение LLM")
        executive = self.llm.get("executive", "")
        synthesis = self.llm.get("synthesis", {})
        if executive:
            self._p(executive)
        elif synthesis.get("conclusion"):
            scores = self.agg.get("llm_scores") or self.llm.get("scores") or {}
            self._p(f"**Контент:** {scores.get('content_score', '—')}/100 · "
                    f"**Структура:** {scores.get('structure_score', '—')}/10 · "
                    f"**Тексты:** {scores.get('text_quality_score', '—')}/10")
            self._p()
            self._p(synthesis.get("conclusion", ""))
            tasks = synthesis.get("top_tasks", [])
            if tasks:
                self._p()
                self._p("**TOP задачи:**")
                for t in tasks:
                    urls = t.get("urls", [])
                    u = f" → {', '.join(f'`{x}`' for x in urls[:3])}" if urls else ""
                    self._p(f"{t.get('priority', '—')}. {t.get('task', '')}{u}")
        else:
            scores = self.agg.get("llm_scores") or self.llm.get("scores") or {}
            if scores.get("content_score") or scores.get("structure_score"):
                self._p(f"**Контент:** {scores.get('content_score', '—')}/100 · "
                        f"**Структура:** {scores.get('structure_score', '—')}/10 · "
                        f"**Тексты:** {scores.get('text_quality_score', '—')}/10")
                struct = self.llm.get("structure", {})
                text = self.llm.get("text", {})
                for label, block in [("Структура", struct), ("Тексты", text)]:
                    if block.get("summary"):
                        self._p()
                        self._p(f"**{label}:** {block['summary']}")
                self._p()
                self._p("_Итоговое заключение и TOP задачи — перезапустите LLM: "
                        "`--from-audit ... --llm-only`_")
            else:
                self._p("_LLM-заключение не сформировано._")
        self._hr()

    def _section_professional_summary(self):
        self._h(2, "0.1. Профессиональная сводка")
        scores = self.agg.get("professional_scores", {})
        if scores:
            rows = [
                ["Общий профиль", f"{scores.get('overall', '—')}/100", _score_status(scores.get("overall"))],
                ["Техническое SEO", f"{scores.get('technical', '—')}/100", _score_status(scores.get("technical"))],
                ["SERP / meta / schema", f"{scores.get('serp', '—')}/100", _score_status(scores.get("serp"))],
                ["Контент", f"{scores.get('content', '—')}/100", _score_status(scores.get("content"))],
                ["Коммерция / доверие / CTA", f"{scores.get('commercial', '—')}/100", _score_status(scores.get("commercial"))],
                ["Перелинковка", f"{scores.get('linking', '—')}/100", _score_status(scores.get("linking"))],
                ["Медиа", f"{scores.get('media', '—')}/100", _score_status(scores.get("media"))],
            ]
            self._table(["Направление", "Балл", "Статус"], rows)
            self._p()

        cats = self.agg.get("issue_categories", {})
        if cats:
            top = sorted(cats.items(), key=lambda x: x[1].get("total", 0), reverse=True)[:8]
            self._p("**Основные зоны риска:**")
            for cat, data in top:
                self._p(
                    f"- **{_category_label(cat)}:** "
                    f"🔴 {data.get('critical', 0)}, "
                    f"🟡 {data.get('warning', 0)}, "
                    f"🔵 {data.get('info', 0)}"
                )
        self._hr()

    def _top_changes(self) -> dict:
        limit = self.cfg.get("output", {}).get("top_changes_count", 20)
        return build_top_changes(self.agg, self.llm, self.base_url, limit)

    def _section_top20(self):
        top = self._top_changes()
        changes = top.get("changes", [])
        n = top.get("limit", 20)
        self._h(2, f"0.2. TOP-{n} необходимых изменений")
        if top.get("summary"):
            self._p(top["summary"])
            self._p()
        if not changes:
            self._p("_Нет данных для формирования списка изменений._")
            self._hr()
            return
        for c in changes:
            sev = c.get("severity", "")
            badge = {"critical": "🔴", "warning": "🟡", "info": "🔵", "llm": "🟣"}.get(sev, "")
            self._p(f"**{c['priority']}. {badge} {c['task']}**")
            urls = c.get("urls", [])
            if urls:
                show = urls[:15]
                for u in show:
                    self._p(f"   - `{u}`")
                if len(urls) > 15:
                    self._p(f"   - _…ещё {len(urls) - 15} стр._")
            else:
                self._p("   - _сайт целиком / внешние URL_")
            self._p()
        self._hr()

    def _section_score(self):
        self._h(3, "1.1. SEO-балл")
        score = self.agg.get("score", {})
        val = score.get("value", "—")
        grade = score.get("grade", "—")
        self._p(f"**Итоговый балл:** {val}/100 (оценка **{grade}**)")
        bonuses = score.get("bonuses", [])
        if bonuses:
            self._p("Бонусы: " + ", ".join(bonuses))
        self._p()

    def _section_summary(self):
        self._h(2, "1. Резюме")
        s = self.agg["summary"]
        m = self.agg["meta"]
        h = self.agg["headings"]
        iss = self.agg["issues_summary"]
        lnk = self.agg["links"]
        i18n = self.agg.get("i18n", {})
        schema = self.agg.get("schema", {})
        perf = self.agg.get("performance", {}).get("summary", {})
        score = self.agg.get("score", {})

        rows = [
            ["SEO-балл", "✅" if score.get("value", 0) >= 70 else "⚠️",
             f"{score.get('value', '—')}/100 ({score.get('grade', '—')})"],
            ["HTTPS", _status_icon(s["pages_https"] == s["pages_total"]),
             f"{s['pages_https']}/{s['pages_total']} страниц"],
            ["Страницы доступны (200)", _status_icon(s["pages_ok_200"] == s["pages_total"]),
             f"{s['pages_ok_200']}/{s['pages_total']}"],
            ["Без title", _status_icon(m["pages_no_title"] == 0),
             str(m["pages_no_title"])],
            ["Без description", _status_icon(m["pages_no_description"] == 0),
             str(m["pages_no_description"])],
            ["Без H1", _status_icon(h["pages_no_h1"] == 0), str(h["pages_no_h1"])],
            ["Без viewport", _status_icon(m["pages_no_viewport"] == 0),
             str(m["pages_no_viewport"])],
            ["Без Schema.org", _status_icon(schema.get("pages_no_schema", 0) == 0),
             str(schema.get("pages_no_schema", 0))],
            ["Дублирующиеся title", _status_icon(len(m["duplicate_titles"]) == 0),
             str(len(m["duplicate_titles"]))],
            ["Битые ссылки", _status_icon(lnk["broken_links_count"] == 0),
             str(lnk["broken_links_count"])],
            ["Mixed content", _status_icon(i18n.get("pages_mixed_content", 0) == 0),
             str(i18n.get("pages_mixed_content", 0))],
            ["Средний TTFB", "✅" if (perf.get("avg_ttfb_ms") or 999) < 500 else "⚠️",
             f"{perf.get('avg_ttfb_ms', '—')} мс" if perf.get("avg_ttfb_ms") is not None else "—"],
        ]
        self._table(["Параметр", "Статус", "Детали"], rows)
        self._p()
        self._p(f"**🔴 Критических проблем:** {iss.get('critical', 0)}")
        self._p(f"**🟡 Важных:** {iss.get('warning', 0)}")
        self._p(f"**🔵 Рекомендаций:** {iss.get('info', 0)}")
        self._section_score()
        self._hr()

    def _section_technical(self):
        self._h(2, "2. Технический SEO")
        s = self.agg["summary"]
        m = self.agg["meta"]

        self._h(3, "2.1. Индексация и HTTPS")
        self._p(f"- HTTPS: {s['pages_https']} из {s['pages_total']} страниц ({s['pages_https_pct']}%)")
        if s["pages_https"] < s["pages_total"]:
            self._p("  ⚠️ Не все страницы работают по HTTPS.")
        self._p()

        self._h(3, "2.2. Мета-теги")
        pages = self._report_pages()
        rows = []
        for p in pages:
            rows.append([
                f"`{p['url'].replace(self.base_url, '') or '/'}`",
                p["title"][:50] + ("…" if len(p["title"]) > 50 else "") if p["title"] else "❌",
                f"{p['title_len']}",
                p["description"][:40] + "…" if len(p.get("description","")) > 40 else p.get("description","❌") or "❌",
                f"{p['description_len']}",
                "✅" if p["keywords"] else "❌",
            ])
        self._table(
            ["URL", "Title", "Дл.", "Description", "Дл.", "Keywords"],
            rows
        )

        # дублирующиеся title
        dup_t = m.get("duplicate_titles", {})
        if dup_t:
            self._p()
            self._p("**Дублирующиеся title:**")
            for title, urls in dup_t.items():
                self._p(f"- `{title[:60]}` → {', '.join(u.replace(self.base_url,'') or '/' for u in urls)}")

        dup_d = m.get("duplicate_descriptions", {})
        if dup_d:
            self._p()
            self._p("**Дублирующиеся description:**")
            for desc, urls in dup_d.items():
                self._p(f"- `{desc[:60]}` → {', '.join(u.replace(self.base_url,'') or '/' for u in urls)}")

        self._p()
        self._h(3, "2.3. Заголовки")
        hd = self.agg["headings"]
        self._p(f"- Страниц без H1: **{hd['pages_no_h1']}**")
        self._p(f"- Страниц с несколькими H1: **{hd['pages_multi_h1']}**")
        if hd.get("duplicate_h1s"):
            self._p("- Дублирующиеся H1:")
            for h1, urls in hd["duplicate_h1s"].items():
                self._p(f"  - `{h1[:60]}` → {', '.join(u.replace(self.base_url,'') or '/' for u in urls)}")

        rows = []
        for p in pages:
            hi_list = [i["message"] for i in p.get("issues", []) if i["code"] == "HEADING_ISSUE"]
            rows.append([
                f"`{p['url'].replace(self.base_url,'') or '/'}`",
                p["h1"][:50] if p["h1"] else "❌",
                str(p["h1_count"]),
                "; ".join(hi_list) if hi_list else "✅",
            ])
        self._p()
        self._table(["URL", "H1", "Кол-во H1", "Проблемы"], rows)

        self._p()
        self._h(3, "2.4. Canonical")
        rows = []
        for p in pages:
            rows.append([
                f"`{p['url'].replace(self.base_url,'') or '/'}`",
                p["canonical"][:50] if p["canonical"] else "—",
                "✅" if p["canonical_matches"] else "❌",
            ])
        self._table(["URL", "Canonical", "Совпадает с URL"], rows)

        self._p()
        self._h(3, "2.5. Open Graph и Schema.org")
        rows = []
        schema = self.agg["schema"]
        for p in pages:
            og = p.get("og", {})
            rows.append([
                f"`{self._url_short(p['url'])}`",
                (og.get("og:title", "")[:30] + "…") if og.get("og:title") else "❌",
                (og.get("og:description", "")[:25] + "…") if og.get("og:description") else "❌",
                "✅" if og.get("og:image") else "❌",
                ", ".join(p["schema_types"]) if p["schema_types"] else "—",
            ])
        self._table(["URL", "og:title", "og:desc", "og:image", "Schema.org"], rows)
        if schema.get("schema_type_counts"):
            self._p()
            self._p("**Используемые Schema.org типы:**")
            for t, cnt in schema["schema_type_counts"].items():
                self._p(f"- `{t}`: {cnt} страниц")

    def _section_robots_sitemap(self):
        self._h(3, "2.6. robots.txt и sitemap")
        rs = self.agg.get("robots_sitemap", {})
        rows = [
            ["robots.txt", "✅" if rs.get("robots_txt_found") else "❌",
             f"Disallow-правил: {rs.get('disallow_rules_count', 0)}"],
            ["sitemap.xml", "✅" if rs.get("sitemap_found") else "❌",
             f"URL в sitemap: {rs.get('sitemap_urls_count', 0)}"],
            ["Неполный обход", "⚠️" if rs.get("sitemap_under_crawled") else "✅",
             "sitemap больше лимита краулера" if rs.get("sitemap_under_crawled") else "—"],
        ]
        self._table(["Параметр", "Статус", "Детали"], rows)
        not_crawled = rs.get("in_sitemap_not_crawled", [])
        not_in_sm = rs.get("crawled_not_in_sitemap", [])
        if not_crawled:
            self._p()
            self._p("**В sitemap, но не обойдены:**")
            for u in not_crawled[:10]:
                self._p(f"- `{u}`")
        if not_in_sm:
            self._p()
            self._p("**Обойдены, но не в sitemap:**")
            for u in not_in_sm[:10]:
                self._p(f"- `{self._url_short(u)}`")
        self._p()
        self._hr()

    def _section_i18n(self):
        self._h(3, "2.7. Twitter Cards и lang")
        pages = self._report_pages()
        rows = []
        for p in pages:
            tw = p.get("twitter", {})
            rows.append([
                f"`{self._url_short(p['url'])}`",
                p.get("html_lang", "") or "❌",
                "✅" if p.get("viewport_valid") else ("❌" if not p.get("html_lang") else "⚠️"),
                tw.get("twitter:card", "—")[:20],
                "✅" if tw.get("twitter:title") else "❌",
                "✅" if p.get("has_favicon") else "—",
            ])
        self._table(["URL", "lang", "viewport", "twitter:card", "twitter:title", "favicon"], rows)
        self._hr()

    def _section_content(self):
        self._h(2, "3. Контент")

        self._h(3, "3.1. Объём текста")
        c = self.agg["content"]
        self._p(f"- Среднее количество слов: **{c['avg_word_count']}**")
        self._p(f"- Страниц с малым объёмом текста (< {c['min_word_count_threshold']} слов): **{c['pages_low_word_count']}**")
        self._p()
        rows = []
        for p in self._report_pages():
            flag = "⚠️" if p["word_count"] < c["min_word_count_threshold"] else ""
            rows.append([
                f"`{p['url'].replace(self.base_url,'') or '/'}`",
                str(p["word_count"]),
                flag,
            ])
        rows.sort(key=lambda r: int(r[1]))
        self._table(["URL", "Слов", ""], rows)

        self._p()
        self._h(3, "3.2. Изображения")
        img = self.agg["images"]
        self._p(f"- Всего изображений: **{img['total_images']}**")
        self._p(f"- Без alt: **{img['total_without_alt']}** ({img['without_alt_percent']}%)")
        self._p()
        rows = []
        for p in self._report_pages():
            if p["images_count"] > 0:
                pct = round(p["images_without_alt"] / p["images_count"] * 100, 1)
                flag = "⚠️" if p["images_without_alt"] > 0 else "✅"
                rows.append([
                    f"`{p['url'].replace(self.base_url,'') or '/'}`",
                    str(p["images_count"]),
                    str(p["images_without_alt"]),
                    f"{pct}%",
                    flag,
                ])
        self._table(["URL", "Изображений", "Без alt", "%", ""], rows)
        self._hr()

    def _section_links(self):
        self._h(2, "4. Ссылки")
        lnk = self.agg["links"]
        self._p(f"- Внутренних ссылок (всего по сайту): **{lnk['total_internal']}**")
        self._p(f"- Внешних ссылок: **{lnk['total_external']}**")
        self._p(f"- Битых ссылок: **{lnk['broken_links_count']}**")

        if lnk["broken_links"]:
            self._p()
            self._p("**Битые ссылки:**")
            self._table(
                ["URL", "HTTP статус"],
                [[url, str(status)] for url, status in lnk["broken_links"].items()]
            )

        self._p()
        self._h(3, "4.1. Внутренняя перелинковка")
        il = self.agg.get("internal_linking", {})
        self._p(f"- Сирот (0 входящих): **{il.get('orphan_count', 0)}**")
        self._p(f"- Слабая перелинковка (< порога): **{il.get('weak_outgoing_count', 0)}**")
        self._p(f"- Nofollow на внешних: **{il.get('nofollow_external_count', 0)}** "
                f"({il.get('nofollow_external_percent', 0)}%)")
        if il.get("orphan_pages"):
            self._p()
            self._p("**Сироты:**")
            for u in il["orphan_pages"][:10]:
                self._p(f"- `{self._url_short(u)}`")
        if il.get("weak_outgoing"):
            self._p()
            self._p("**Слабая перелинковка:**")
            for w in il["weak_outgoing"][:10]:
                self._p(f"- `{self._url_short(w['url'])}` — {w['outgoing']} исходящих")
        self._hr()

    def _section_performance(self):
        self._h(2, "5. Производительность")
        perf = self.agg.get("performance", {})
        summary = perf.get("summary", {})

        if not summary:
            self._p("_Данные производительности недоступны._")
            self._hr()
            return

        avg_ttfb = summary.get("avg_ttfb_ms")
        avg_total = summary.get("avg_total_ms")
        avg_size = summary.get("avg_size_kb")
        gzip_count = summary.get("pages_with_gzip", 0)
        redirect_count = summary.get("pages_with_redirect", 0)
        measured = summary.get("pages_measured", 0)

        def ttfb_rating(ms):
            if ms is None:
                return "—"
            if ms < 200:
                return "✅ Отлично"
            if ms < 500:
                return "⚠️ Приемлемо"
            return "❌ Медленно"

        self._p(f"Измерено страниц: **{measured}**\n")
        self._table(
            ["Метрика", "Значение", "Оценка"],
            [
                ["TTFB (среднее)", f"{avg_ttfb} мс" if avg_ttfb else "—", ttfb_rating(avg_ttfb)],
                ["Время загрузки (среднее)", f"{avg_total} мс" if avg_total else "—", ""],
                ["Размер страницы (среднее)", f"{avg_size} КБ" if avg_size else "—", ""],
                ["Сжатие (gzip/br)", f"{gzip_count} из {measured}", "✅" if gzip_count == measured else "⚠️"],
                ["Редиректы", str(redirect_count), "⚠️" if redirect_count > 0 else "✅"],
            ]
        )

        pages = perf.get("pages", [])
        if pages:
            self._p()
            self._p("**Детализация по страницам:**")
            rows = []
            for p in pages:
                url_short = p["url"].replace("https://", "").replace("http://", "")
                if len(url_short) > 55:
                    url_short = url_short[:52] + "..."
                ttfb = f"{p['ttfb_ms']} мс" if p.get("ttfb_ms") else p.get("error", "—")
                size = f"{round(p['size_bytes'] / 1024, 1)} КБ" if p.get("size_bytes") else "—"
                gz = "✅" if p.get("compressed") else "—"
                rows.append([url_short, ttfb, size, gz])
            self._table(["URL", "TTFB", "Размер", "Сжатие"], rows)

        slow = [p for p in pages if p.get("slow_ttfb")]
        large = [p for p in pages if p.get("large_page")]
        if slow or large:
            self._p()
            self._h(3, "5.1. Пороги производительности")
            if slow:
                self._p(f"**Медленный TTFB (>{summary.get('ttfb_warn_ms', 500)} мс):** {len(slow)} стр.")
                for p in slow[:10]:
                    self._p(f"- `{self._url_short(p['url'])}` — {p.get('ttfb_ms')} мс")
            if large:
                self._p(f"**Большие страницы (>{summary.get('page_size_warn_kb', 1500)} КБ):** {len(large)} стр.")
                for p in large[:10]:
                    kb = round(p.get("size_bytes", 0) / 1024, 1)
                    self._p(f"- `{self._url_short(p['url'])}` — {kb} КБ")
        self._hr()

    def _section_url_structure(self):
        self._h(2, "6. Структура URL")
        pages = self.agg.get("pages", [])
        if not pages:
            self._p("_Нет данных._")
            self._hr()
            return

        from urllib.parse import urlparse

        depths = []
        long_urls = []
        non_readable = []
        for p in pages:
            url = p["url"]
            parsed = urlparse(url)
            path = parsed.path.rstrip("/")
            depth = len([s for s in path.split("/") if s])
            depths.append(depth)
            if len(url) > 100:
                long_urls.append(url)
            # нечитаемые: содержат цифровые сегменты длиннее 6 символов или query-параметры
            import re
            segments = [s for s in path.split("/") if s]
            ugly = any(re.fullmatch(r"\d{5,}", seg) for seg in segments) or bool(parsed.query)
            if ugly:
                non_readable.append(url)

        avg_depth = round(sum(depths) / len(depths), 1) if depths else 0
        max_depth = max(depths) if depths else 0

        self._table(
            ["Параметр", "Значение"],
            [
                ["Средняя глубина URL", str(avg_depth)],
                ["Максимальная глубина", str(max_depth)],
                ["URL длиннее 100 символов", str(len(long_urls))],
                ["URL с числовыми сегментами / параметрами", str(len(non_readable))],
            ]
        )

        if long_urls:
            self._p()
            self._p("**Длинные URL (>100 символов):**")
            for u in long_urls[:10]:
                self._p(f"- `{u}`")

        if non_readable:
            self._p()
            self._p("**Нечитаемые URL:**")
            for u in non_readable[:10]:
                self._p(f"- `{u}`")
        self._hr()

    def _section_analytics(self):
        self._h(2, "7. Аналитика")
        analytics = self.agg.get("analytics", {})
        analytics_names = {
            "yandex_metrika": "Яндекс.Метрика",
            "google_analytics": "Google Analytics",
            "google_tag_manager": "Google Tag Manager",
            "vk_pixel": "VK Pixel",
            "facebook_pixel": "Facebook Pixel",
            "top_mail_ru": "Top.Mail.Ru",
        }
        rows = []
        for key, name in analytics_names.items():
            found = key in analytics
            rows.append([name, "✅ Найден" if found else "—"])
        self._table(["Сервис", "Статус"], rows)
        self._hr()

    def _section_pages_audit(self):
        self._h(2, "8. Аудит страниц")

        pages = self._report_pages(80)

        # --- Сводная таблица ---
        self._h(3, "8.1. Сводная таблица")
        rows = []
        for p in pages:
            url_short = p["url"].replace(self.base_url, "") or "/"
            if len(url_short) > 50:
                url_short = url_short[:47] + "..."
            status = p.get("status", "—")
            title_ok = "✅" if p.get("title") else "❌"
            desc_ok = "✅" if p.get("description") else "❌"
            h1_ok = "✅" if p.get("h1_count", 0) == 1 else ("❌" if p.get("h1_count", 0) == 0 else "⚠️")
            canon_ok = "✅" if p.get("canonical_matches") else ("—" if not p.get("canonical") else "⚠️")
            og_ok = "✅" if p.get("og_complete") else "—"
            schema_ok = "✅" if p.get("schema_types") else "—"
            issues_n = p.get("issues_count", 0)
            crits_n = sum(1 for i in p.get("issues", []) if i["severity"] == "critical")
            issues_str = f"{'🔴 ' + str(crits_n) + ' ' if crits_n else ''}{'⚠️ ' + str(issues_n - crits_n) if issues_n - crits_n else ''}" or "✅"
            rows.append([url_short, str(status), title_ok, desc_ok, h1_ok, canon_ok, og_ok, schema_ok, issues_str.strip()])
        self._table(
            ["URL", "Статус", "Title", "Desc", "H1", "Canonical", "OG", "Schema", "Проблемы"],
            rows
        )
        self._p()

        # --- Детальный список ---
        self._h(3, "8.2. Детализация проблем")
        has_any = False
        for p in pages:
            issues = p.get("issues", [])
            if not issues:
                continue
            has_any = True
            url_short = p["url"].replace(self.base_url, "") or "/"
            self._h(4, f"`{url_short}`")
            crits = _issues_by_severity(issues, "critical")
            warns = _issues_by_severity(issues, "warning")
            infos = _issues_by_severity(issues, "info")
            for iss in crits:
                self._p(f"  🔴 **{iss['code']}**: {iss['message']}")
            for iss in warns:
                self._p(f"  🟡 **{iss['code']}**: {iss['message']}")
            for iss in infos:
                self._p(f"  🔵 {iss['code']}: {iss['message']}")
            self._p()
        if not has_any:
            self._p("✅ Проблем не обнаружено.")
        self._hr()

    def _section_llm_logic(self):
        self._h(2, "9. Анализ логичности структуры (LLM)")
        scores = self.agg.get("llm_scores") or self.llm.get("scores") or {}
        if scores.get("structure_score") is not None:
            self._p(f"**Балл структуры:** {scores.get('structure_score')}/10 · "
                    f"**Title↔H1:** {scores.get('title_h1_score', '—')}/10 · "
                    f"**Контент (сводный):** {scores.get('content_score', '—')}/100")
            self._p()
        logic = self.llm.get("logic", "")
        if logic:
            self._p(logic)
        else:
            self._p("_LLM-анализ не выполнялся (api_key не задан или использован флаг --no-llm)._")
        self._hr()

    def _section_llm_text(self):
        self._h(2, "10. Анализ качества текстов (LLM)")
        scores = self.agg.get("llm_scores") or self.llm.get("scores") or {}
        if scores.get("text_quality_score") is not None:
            self._p(f"**Балл текстов:** {scores.get('text_quality_score')}/10 · "
                    f"**Читаемость:** {scores.get('readability_avg', '—')}/5 · "
                    f"**SEO-потенциал:** {scores.get('seo_potential_avg', '—')}/5")
            self._p()
        quality = self.llm.get("text_quality", "")
        if quality:
            self._p(quality)
        else:
            self._p("_LLM-анализ не выполнялся (api_key не задан или использован флаг --no-llm)._")
        self._hr()

    def _dedup_recommendations(self) -> dict:
        """Группировка рекомендаций по code (без дублей из §8)."""
        grouped = {}
        for p in self.agg["pages"]:
            url_short = self._url_short(p["url"])
            for iss in p.get("issues", []):
                code = iss["code"]
                if code not in grouped:
                    grouped[code] = {
                        "severity": iss["severity"],
                        "message": iss["message"],
                        "urls": [],
                    }
                if url_short not in grouped[code]["urls"]:
                    grouped[code]["urls"].append(url_short)
        return grouped

    def _section_keywords(self):
        self._h(2, "12. Ключевые слова")
        kw = self.agg.get("plugins", {}).get("keywords", {})
        if not kw or kw.get("error"):
            self._p("_Плагин keywords не выполнен._")
            self._hr()
            return
        self._p(f"**Покрытие title топ-словами:** {kw.get('meta_coverage_score', 0)}%")
        self._p(f"**Уникальных слов в корпусе:** {kw.get('unique_words', 0)}")
        top = kw.get("top_keywords", [])
        if top:
            self._p()
            self._p("**Топ слов:**")
            self._table(
                ["Слово", "Частота"],
                [[t["word"], str(t["count"])] for t in top[:15]],
            )
        coverage = kw.get("page_coverage", [])
        weak = [c for c in coverage if not c.get("title_keywords")]
        if weak:
            self._p()
            self._p(f"**Страницы без топ-слов в title:** {len(weak)}")
            for c in weak[:8]:
                self._p(f"- `{self._url_short(c['url'])}`")
        self._hr()

    def _section_recommendations(self):
        self._h(2, "11. Приоритетные рекомендации")

        grouped = self._dedup_recommendations()
        crits = {k: v for k, v in grouped.items() if v["severity"] == "critical"}
        warns = {k: v for k, v in grouped.items() if v["severity"] == "warning"}
        infos = {k: v for k, v in grouped.items() if v["severity"] == "info"}

        def fmt_entry(code, data):
            urls = data["urls"]
            url_part = ", ".join(f"`{u}`" for u in urls[:5])
            if len(urls) > 5:
                url_part += f" (+{len(urls) - 5})"
            return f"**{code}**: {data['message']} — {url_part}"

        if crits:
            self._h(3, "Критические (исправить в первую очередь)")
            for i, (code, data) in enumerate(crits.items(), 1):
                self._p(f"{i}. {fmt_entry(code, data)}")
            self._p()

        if warns:
            self._h(3, "Важные")
            for i, (code, data) in enumerate(warns.items(), 1):
                self._p(f"{i}. {fmt_entry(code, data)}")
            self._p()

        if infos:
            self._h(3, "Рекомендации")
            for i, (code, data) in enumerate(infos.items(), 1):
                self._p(f"{i}. {fmt_entry(code, data)}")
            self._p()

        if not crits and not warns and not infos:
            self._p("✅ Критических и важных проблем не обнаружено.")

        self._p()
        self._p("---")
        self._p(f"_Отчёт сформирован автоматически SEO Auditor. Дата: {self.today}_")

    # ------------------------------------------------------------------
    # Основной метод
    # ------------------------------------------------------------------

    def build(self) -> str:
        self._section_header()
        self._section_toc()
        self._section_executive()
        self._section_professional_summary()
        self._section_top20()
        self._section_summary()
        self._section_technical()
        self._section_robots_sitemap()
        self._section_i18n()
        self._section_content()
        self._section_links()
        self._section_performance()
        self._section_url_structure()
        self._section_analytics()
        self._section_pages_audit()
        self._section_llm_logic()
        self._section_llm_text()
        self._section_recommendations()
        self._section_keywords()
        return "\n".join(self.lines)


# ------------------------------------------------------------------
# HTML-обёртка над MD (legacy)
# ------------------------------------------------------------------

def _md_fragment_to_html(md: str) -> str:
    """Конвертация фрагмента MD в HTML (для LLM-блоков)."""
    if not md:
        return ""
    return _md_to_html_simple(md, "").split("<body>")[1].split("</body>")[0].strip()


def _md_to_html_simple(md: str, title: str) -> str:
    """
    Простой конвертер MD → HTML без внешних зависимостей.
    Поддерживает: заголовки, таблицы, параграфы, жирный, код, горизонтальную линию.
    """
    import re
    lines = md.split("\n")
    html_lines = []
    in_table = False
    in_para = False

    def flush_para():
        nonlocal in_para
        if in_para:
            html_lines.append("</p>")
            in_para = False

    def inline(text: str) -> str:
        # жирный
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        # курсив
        text = re.sub(r"_(.+?)_", r"<em>\1</em>", text)
        # инлайн-код
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        # ссылки [text](url)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
        return text

    for line in lines:
        # горизонтальная линия
        if line.strip() in ("---", "***", "___"):
            flush_para()
            if in_table:
                html_lines.append("</table>")
                in_table = False
            html_lines.append("<hr>")
            continue

        # заголовки
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush_para()
            if in_table:
                html_lines.append("</table>")
                in_table = False
            level = len(m.group(1))
            content = inline(m.group(2))
            anchor = re.sub(r"[^\w\-]", "-", m.group(2).lower()).strip("-")
            anchor = re.sub(r"-+", "-", anchor)
            html_lines.append(f'<h{level} id="{anchor}">{content}</h{level}>')
            continue

        # таблица
        if line.strip().startswith("|"):
            if not in_table:
                flush_para()
                html_lines.append('<table class="seo-table">')
                in_table = True
                table_first_row = True
            # else: table_first_row уже False после первой строки

            # разделитель (|---|---|)
            if re.match(r"^\|[-|\s:]+\|$", line.strip()):
                continue

            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if table_first_row:
                html_lines.append("<thead><tr>" +
                                  "".join(f"<th>{inline(c)}</th>" for c in cells) +
                                  "</tr></thead><tbody>")
                table_first_row = False
            else:
                html_lines.append("<tr>" +
                                  "".join(f"<td>{inline(c)}</td>" for c in cells) +
                                  "</tr>")
            continue

        if in_table and not line.strip().startswith("|"):
            html_lines.append("</tbody></table>")
            in_table = False

        # список
        m_li = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)", line)
        if m_li:
            flush_para()
            indent = len(m_li.group(1))
            html_lines.append(f'<li style="margin-left:{indent*8}px">{inline(m_li.group(3))}</li>')
            continue

        # пустая строка
        if not line.strip():
            flush_para()
            if in_table:
                html_lines.append("</tbody></table>")
                in_table = False
            continue

        # обычный текст — параграф
        if not in_para:
            html_lines.append("<p>")
            in_para = True
        html_lines.append(inline(line))

    flush_para()
    if in_table:
        html_lines.append("</tbody></table>")

    body = "\n".join(html_lines)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 1100px; margin: 0 auto; padding: 24px; color: #1a1a1a; background: #fff; }}
  h1 {{ color: #1a1a2e; border-bottom: 3px solid #4a6cf7; padding-bottom: 8px; }}
  h2 {{ color: #16213e; border-bottom: 1px solid #e0e0e0; padding-bottom: 4px; margin-top: 40px; }}
  h3 {{ color: #0f3460; margin-top: 24px; }}
  h4 {{ color: #333; font-size: 0.95em; }}
  table.seo-table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 0.9em; }}
  table.seo-table th {{ background: #4a6cf7; color: white; padding: 8px 12px; text-align: left; }}
  table.seo-table td {{ padding: 7px 12px; border-bottom: 1px solid #eee; }}
  table.seo-table tr:hover td {{ background: #f5f7ff; }}
  code {{ background: #f0f0f0; padding: 1px 5px; border-radius: 3px; font-size: 0.88em; }}
  hr {{ border: none; border-top: 1px solid #e0e0e0; margin: 32px 0; }}
  p {{ line-height: 1.65; }}
  li {{ line-height: 1.6; }}
  strong {{ color: #1a1a2e; }}
  a {{ color: #4a6cf7; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .toc {{ background: #f8f9ff; border-left: 4px solid #4a6cf7; padding: 16px 24px;
          border-radius: 4px; margin: 20px 0; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


# ------------------------------------------------------------------
# HTML Reporter (Jinja2)
# ------------------------------------------------------------------

class HTMLReporter:
    ANALYTICS_NAMES = {
        "yandex_metrika": "Яндекс.Метрика",
        "google_analytics": "Google Analytics",
        "google_tag_manager": "Google Tag Manager",
        "vk_pixel": "VK Pixel",
        "facebook_pixel": "Facebook Pixel",
        "top_mail_ru": "Top.Mail.Ru",
    }

    def __init__(self, base_url: str, aggregated: dict, llm: dict, cfg: dict):
        self.base_url = base_url.rstrip("/")
        self.agg = aggregated
        self.llm = llm
        self.cfg = cfg
        self.domain = urlparse(base_url).netloc
        self.today = date.today().strftime("%Y-%m-%d")
        tpl_dir = Path(__file__).parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(str(tpl_dir)),
            autoescape=select_autoescape(["html", "j2"]),
        )

    def url_short(self, url: str) -> str:
        return url.replace(self.base_url, "").replace("https://", "").replace("http://", "") or "/"

    def _summary_rows(self) -> list:
        s = self.agg["summary"]
        m = self.agg["meta"]
        h = self.agg["headings"]
        lnk = self.agg["links"]
        schema = self.agg.get("schema", {})
        i18n = self.agg.get("i18n", {})
        perf = self.agg.get("performance", {}).get("summary", {})
        score = self.agg.get("score", {})

        def icon(ok):
            return "✅" if ok else "❌"

        return [
            {"label": "SEO-балл", "status": icon(score.get("value", 0) >= 70),
             "detail": f"{score.get('value', '—')}/100 ({score.get('grade', '—')})"},
            {"label": "HTTPS", "status": icon(s["pages_https"] == s["pages_total"]),
             "detail": f"{s['pages_https']}/{s['pages_total']}"},
            {"label": "Доступны (200)", "status": icon(s["pages_ok_200"] == s["pages_total"]),
             "detail": f"{s['pages_ok_200']}/{s['pages_total']}"},
            {"label": "Без title", "status": icon(m["pages_no_title"] == 0), "detail": str(m["pages_no_title"])},
            {"label": "Без description", "status": icon(m["pages_no_description"] == 0),
             "detail": str(m["pages_no_description"])},
            {"label": "Без H1", "status": icon(h["pages_no_h1"] == 0), "detail": str(h["pages_no_h1"])},
            {"label": "Без Schema", "status": icon(schema.get("pages_no_schema", 0) == 0),
             "detail": str(schema.get("pages_no_schema", 0))},
            {"label": "Битые ссылки", "status": icon(lnk["broken_links_count"] == 0),
             "detail": str(lnk["broken_links_count"])},
            {"label": "Mixed content", "status": icon(i18n.get("pages_mixed_content", 0) == 0),
             "detail": str(i18n.get("pages_mixed_content", 0))},
            {"label": "TTFB среднее", "status": icon((perf.get("avg_ttfb_ms") or 999) < 500),
             "detail": f"{perf.get('avg_ttfb_ms', '—')} мс" if perf.get("avg_ttfb_ms") is not None else "—"},
        ]

    def _url_stats(self) -> dict:
        depths, long_urls = [], []
        for p in self.agg.get("pages", []):
            parsed = urlparse(p["url"])
            path = parsed.path.rstrip("/")
            depths.append(len([s for s in path.split("/") if s]))
            if len(p["url"]) > 100:
                long_urls.append(p["url"])
        return {
            "avg_depth": round(sum(depths) / len(depths), 1) if depths else 0,
            "max_depth": max(depths) if depths else 0,
            "long_count": len(long_urls),
        }

    def _recommendations(self) -> dict:
        md = MDReporter(self.base_url, self.agg, self.llm, self.cfg)
        grouped = md._dedup_recommendations()
        return {
            "critical": {k: v for k, v in grouped.items() if v["severity"] == "critical"},
            "warning": {k: v for k, v in grouped.items() if v["severity"] == "warning"},
            "info": {k: v for k, v in grouped.items() if v["severity"] == "info"},
        }

    def _issue_category_rows(self) -> list:
        rows = []
        for cat, data in self.agg.get("issue_categories", {}).items():
            rows.append({
                "code": cat,
                "label": _category_label(cat),
                "critical": data.get("critical", 0),
                "warning": data.get("warning", 0),
                "info": data.get("info", 0),
                "total": data.get("total", 0),
            })
        return sorted(rows, key=lambda r: r["total"], reverse=True)

    def _issues_list(self) -> list:
        """Ошибки для HTML: код, severity, message, urls (аккордеон)."""
        grouped = self._recommendations()
        order = [("critical", 0), ("warning", 1), ("info", 2)]
        items = []
        for sev, _ in order:
            for code, data in grouped.get(sev, {}).items():
                items.append({
                    "code": code,
                    "severity": sev,
                    "message": data["message"],
                    "count": len(data["urls"]),
                    "urls": data["urls"],
                })
        return items

    def _errors_visible(self) -> list:
        return [i for i in self._issues_list() if i["severity"] in ("critical", "warning")]

    def _infos_collapsed(self) -> list:
        return [i for i in self._issues_list() if i["severity"] == "info"]

    def build(self) -> str:
        template = self.env.get_template("report.html.j2")
        agg = self.agg
        if "score" not in agg:
            agg["score"] = {"value": 0, "grade": "—", "bonuses": []}
        return template.render(
            domain=self.domain,
            base_url=self.base_url,
            today=self.today,
            agg=agg,
            llm=self.llm,
            perf_summary=agg.get("performance", {}).get("summary", {}),
            summary_rows=self._summary_rows(),
            url_stats=self._url_stats(),
            analytics_names=self.ANALYTICS_NAMES,
            recommendations=self._recommendations(),
            errors_visible=self._errors_visible(),
            infos_collapsed=self._infos_collapsed(),
            llm_scores=agg.get("llm_scores") or self.llm.get("scores") or {},
            professional_scores=agg.get("professional_scores", {}),
            issue_categories=agg.get("issue_categories", {}),
            issue_category_rows=self._issue_category_rows(),
            synthesis=self.llm.get("synthesis", {}),
            executive=self.llm.get("executive", ""),
            executive_html=_md_fragment_to_html(self.llm.get("executive", "")),
            top_changes=build_top_changes(
                self.agg,
                self.llm,
                self.base_url,
                self.cfg.get("output", {}).get("top_changes_count", 20),
            ),
            keywords=agg.get("plugins", {}).get("keywords"),
            llm_html={
                "logic": _md_fragment_to_html(self.llm.get("logic", "")),
                "text_quality": _md_fragment_to_html(self.llm.get("text_quality", "")),
            },
            url_short=self.url_short,
        )


# ------------------------------------------------------------------
# Основной класс
# ------------------------------------------------------------------

class Reporter:
    def __init__(self, audit_dir: Path, cfg: dict):
        self.audit_dir = audit_dir
        self.cfg = cfg

    def _report_paths(self) -> tuple[Path, Path]:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        base = f"report_{stamp}"
        return self.audit_dir / f"{base}.md", self.audit_dir / f"{base}.html"

    def generate(self, base_url: str, raw_data: list, aggregated: dict, llm_results: dict) -> tuple[Path, Path]:
        domain = urlparse(base_url).netloc
        md_path, html_path = self._report_paths()

        # --- MD ---
        md_builder = MDReporter(base_url, aggregated, llm_results, self.cfg)
        md_content = md_builder.build()
        md_path.write_text(md_content, encoding="utf-8")

        # --- HTML ---
        html_mode = self.cfg.get("output", {}).get("report_html_mode", "jinja")
        if html_mode == "jinja":
            html_builder = HTMLReporter(base_url, aggregated, llm_results, self.cfg)
            html_content = html_builder.build()
        else:
            html_content = _md_to_html_simple(md_content, f"SEO-аудит {domain}")
        html_path.write_text(html_content, encoding="utf-8")
        return md_path, html_path
