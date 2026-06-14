"""
LLM Client — пакетные запросы (25–50 стр.), агрегация и итоговое заключение.
"""

import json
import re
import statistics
from urllib.parse import urlparse
from openai import OpenAI


PROMPT_STRUCTURE_BATCH = """Профессиональный SEO-аналитик. Оцени согласованность title, description, h1, h2 и соответствие интенту страницы (пакет {batch_num}/{batch_total}).
Данные:
{data}

Ответ — ТОЛЬКО JSON:
{{"structure_score":<1-10>,"title_h1_score":<1-10>,"pages":[{{"path":"/...","t_h1":<1-5>,"note":"title/H1/H2/intent до 80 симв"}}],"problems":["до 4"],"strengths":["до 2"],"summary":"до 160 симв"}}"""

PROMPT_TEXT_BATCH = """SEO-редактор и коммерческий SEO-аналитик. Оцени тексты, E-E-A-T, полезность, коммерческую полноту, CTA, конкретику и риск шаблонности (пакет {batch_num}/{batch_total}).
Данные:
{data}

Ответ — ТОЛЬКО JSON:
{{"text_quality_score":<1-10>,"readability_avg":<1-5>,"seo_potential_avg":<1-5>,"pages":[{{"path":"/...","read":<1-5>,"seo":<1-5>,"note":"до 80 симв"}}],"problems":["до 5"],"priority_urls":["/..."],"summary":"до 180 симв"}}"""

PROMPT_SYNTHESIS = """SEO-эксперт. По итогам аудита сайта сформируй заключение и приоритетные задачи.

Сводка структуры (title/h1/h2): score {structure_score}/10, проблемы: {structure_problems}
Сводка текстов: score {text_score}/10, проблемы: {text_problems}
Технический SEO: критических {critical}, важных {warning}

Ответ — ТОЛЬКО JSON:
{{"conclusion":"до 550 симв — как улучшить качество материала, доверие и конверсию","top_tasks":[{{"priority":1,"task":"конкретная задача с ожидаемым эффектом","urls":["/path"]}}]}}
Дай 5–7 top_tasks, отсортированных по priority. Задачи должны быть применимы к страницам, а не абстрактны."""


def _path(url: str) -> str:
    p = urlparse(url).path or "/"
    return p if p != "" else "/"


def _parse_json_response(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return {"error": "parse_failed", "raw": text[:500]}


def _avg_numeric(values: list) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.mean(nums), 1) if nums else None


def _uniq_list(items: list, limit: int = 30) -> list:
    seen = set()
    out = []
    for x in items:
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
        if len(out) >= limit:
            break
    return out


class LLMClient:
    def __init__(self, llm_cfg: dict):
        self.cfg = llm_cfg
        self.request_timeout = llm_cfg.get("request_timeout", 120)
        self.client = OpenAI(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["url"],
            timeout=self.request_timeout,
            max_retries=1,
        )
        self.model = llm_cfg["model"]
        self.temperature = llm_cfg.get("temperature", 0.2)
        self.batch_size = llm_cfg.get("batch_size", 40)
        self.max_text_chars = llm_cfg.get("max_text_chars_per_page", 600)
        self.max_h2 = llm_cfg.get("max_h2_per_page", 4)
        self.top_tasks_count = llm_cfg.get("top_tasks_count", 7)

    def _call(self, prompt: str) -> str:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[{"role": "user", "content": prompt}],
                timeout=self.request_timeout,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            err = str(e)[:200]
            print(f"  [LLM] API ошибка: {err}", flush=True)
            return json.dumps({"error": err}, ensure_ascii=False)

    def _chunks(self, items: list) -> list[list]:
        n = self.batch_size
        return [items[i : i + n] for i in range(0, len(items), n)]

    def _structure_rows(self, pages: list) -> str:
        rows = []
        for page in pages:
            h2 = page.get("headings", {}).get("h2", [])[: self.max_h2]
            rows.append({
                "p": _path(page["url"]),
                "t": (page.get("title") or "")[:60],
                "d": (page.get("description") or "")[:80],
                "h1": (page.get("headings", {}).get("h1", [""])[0] if page.get("headings", {}).get("h1") else "")[:60],
                "h2": [h[:40] for h in h2],
            })
        return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))

    def _text_rows(self, pages: list) -> str:
        rows = []
        for page in pages:
            rows.append({
                "p": _path(page["url"]),
                "w": page.get("word_count", 0),
                "s": page.get("content_stats", {}),
                "txt": (page.get("text_preview") or "")[: self.max_text_chars],
            })
        return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))

    def _run_batches(self, label: str, pages: list, prompt_tpl: str) -> tuple[dict, list]:
        chunks = self._chunks(pages)
        if not chunks:
            return {}, []
        batches_out = []
        errors = []
        total = len(chunks)
        print(f"  [LLM] {label}: {len(pages)} стр., {total} пакет(ов) по ≤{self.batch_size}", flush=True)

        for i, chunk in enumerate(chunks, 1):
            data = self._structure_rows(chunk) if "structure" in label else self._text_rows(chunk)
            prompt = prompt_tpl.format(batch_num=i, batch_total=total, data=data)
            print(f"  [LLM] {label} пакет {i}/{total}: запрос...", flush=True)
            parsed = _parse_json_response(self._call(prompt))
            batches_out.append({"batch": i, "pages_count": len(chunk), "result": parsed})
            if parsed.get("error"):
                errors.append({"batch": i, "error": parsed.get("error")})
                print(f"  [LLM] {label} пакет {i}/{total}: ошибка — {parsed.get('error')}")
            else:
                print(f"  [LLM] {label} пакет {i}/{total}: OK")

        merged = self._merge_batches(batches_out, label)
        if errors:
            merged["batch_errors"] = errors
        merged["batches"] = batches_out
        merged["batches_total"] = total
        merged["pages_analyzed"] = len(pages)
        return merged, errors

    def _merge_batches(self, batches_out: list, label: str) -> dict:
        ok = [b["result"] for b in batches_out if not b["result"].get("error")]
        if not ok:
            return {"error": "all_batches_failed"}

        problems, strengths, pages_all, priority_urls = [], [], [], []
        if "structure" in label:
            merged = {
                "structure_score": _avg_numeric([r.get("structure_score") for r in ok]),
                "title_h1_score": _avg_numeric([r.get("title_h1_score") for r in ok]),
            }
        else:
            merged = {
                "text_quality_score": _avg_numeric([r.get("text_quality_score") for r in ok]),
                "readability_avg": _avg_numeric([r.get("readability_avg") for r in ok]),
                "seo_potential_avg": _avg_numeric([r.get("seo_potential_avg") for r in ok]),
            }

        for r in ok:
            if isinstance(r.get("problems"), list):
                problems.extend(r["problems"])
            if isinstance(r.get("strengths"), list):
                strengths.extend(r["strengths"])
            if isinstance(r.get("pages"), list):
                pages_all.extend(r["pages"])
            if isinstance(r.get("priority_urls"), list):
                priority_urls.extend(r["priority_urls"])

        summaries = [r.get("summary") for r in ok if r.get("summary")]
        merged["problems"] = _uniq_list(problems, 20)
        merged["strengths"] = _uniq_list(strengths, 10)
        merged["pages"] = pages_all[:50]
        merged["priority_urls"] = _uniq_list(priority_urls, 15)
        merged["summary"] = " ".join(summaries[:3])[:400] if summaries else ""
        return merged

    def analyze_structure(self, raw_data: list) -> dict:
        ok = [p for p in raw_data if p.get("status") == 200]
        merged, _ = self._run_batches("structure", ok, PROMPT_STRUCTURE_BATCH)
        return merged

    def analyze_text_quality(self, raw_data: list) -> dict:
        ok = [p for p in raw_data if p.get("status") == 200 and p.get("word_count", 0) > 50]
        merged, _ = self._run_batches("text", ok, PROMPT_TEXT_BATCH)
        return merged

    def analyze_synthesis(self, structure: dict, text: dict, aggregated: dict) -> dict:
        print("  [LLM] Итоговое заключение...")
        iss = aggregated.get("issues_summary", {})
        sp = structure.get("problems", [])[:8] if not structure.get("error") else []
        tp = text.get("problems", [])[:8] if not text.get("error") else []
        prompt = PROMPT_SYNTHESIS.format(
            structure_score=structure.get("structure_score", "—"),
            structure_problems=json.dumps(sp, ensure_ascii=False)[:600],
            text_score=text.get("text_quality_score", "—"),
            text_problems=json.dumps(tp, ensure_ascii=False)[:600],
            critical=iss.get("critical", 0),
            warning=iss.get("warning", 0),
        )
        parsed = _parse_json_response(self._call(prompt))
        if parsed.get("error"):
            print(f"  [LLM] synthesis: ошибка — {parsed.get('error')}")
        tasks = parsed.get("top_tasks", [])
        if isinstance(tasks, list):
            parsed["top_tasks"] = sorted(
                [t for t in tasks if isinstance(t, dict)],
                key=lambda x: x.get("priority", 99),
            )[: self.top_tasks_count]
        return parsed

    def _format_executive_md(self, synthesis: dict, scores: dict) -> str:
        if synthesis.get("error"):
            return f"_Заключение LLM недоступно: {synthesis.get('error')}_"
        lines = [
            f"**Контент (сводный):** {scores.get('content_score', '—')}/100",
            f"**Структура:** {scores.get('structure_score', '—')}/10 · **Тексты:** {scores.get('text_quality_score', '—')}/10",
            "",
            synthesis.get("conclusion", ""),
            "",
            "**TOP задачи:**",
        ]
        for t in synthesis.get("top_tasks", []):
            urls = t.get("urls", [])
            u = f" → {', '.join(f'`{x}`' for x in urls[:3])}" if urls else ""
            lines.append(f"{t.get('priority', '—')}. {t.get('task', '')}{u}")
        return "\n".join(lines)

    def _format_structure_md(self, data: dict) -> str:
        if data.get("error"):
            return f"_Ошибка: {data.get('error')}_"
        lines = [
            f"**Балл структуры:** {data.get('structure_score', '—')}/10 "
            f"(пакетов: {data.get('batches_total', 1)}, стр.: {data.get('pages_analyzed', '—')})",
            f"**Title↔H1:** {data.get('title_h1_score', '—')}/10",
            "", data.get("summary", ""), "",
        ]
        if data.get("problems"):
            lines.append("**Проблемы:**")
            for p in data["problems"][:10]:
                lines.append(f"- {p}")
        return "\n".join(lines)

    def _format_text_md(self, data: dict) -> str:
        if data.get("error"):
            return f"_Ошибка: {data.get('error')}_"
        lines = [
            f"**Балл текстов:** {data.get('text_quality_score', '—')}/10 "
            f"(пакетов: {data.get('batches_total', 1)}, стр.: {data.get('pages_analyzed', '—')})",
            f"**Читаемость:** {data.get('readability_avg', '—')}/5 · **SEO:** {data.get('seo_potential_avg', '—')}/5",
            "", data.get("summary", ""), "",
        ]
        if data.get("problems"):
            lines.append("**Проблемы:**")
            for p in data["problems"][:10]:
                lines.append(f"- {p}")
        return "\n".join(lines)

    def analyze(self, raw_data: list, aggregated: dict, cfg: dict) -> dict:
        structure, text, synthesis = {}, {}, {}
        errors = []

        for name, fn in [("structure", self.analyze_structure), ("text", self.analyze_text_quality)]:
            try:
                result = fn(raw_data)
                if name == "structure":
                    structure = result
                else:
                    text = result
                if result.get("error"):
                    errors.append({"step": name, "error": result["error"]})
                for be in result.get("batch_errors", []):
                    errors.append({"step": f"{name}_batch_{be['batch']}", "error": be["error"]})
            except Exception as e:
                errors.append({"step": name, "error": str(e)})
                if name == "structure":
                    structure = {"error": str(e)}
                else:
                    text = {"error": str(e)}

        try:
            synthesis = self.analyze_synthesis(structure, text, aggregated)
            if synthesis.get("error"):
                errors.append({"step": "synthesis", "error": synthesis["error"]})
        except Exception as e:
            synthesis = {"error": str(e)}
            errors.append({"step": "synthesis", "error": str(e)})

        scores = {
            "structure_score": structure.get("structure_score") if not structure.get("error") else None,
            "title_h1_score": structure.get("title_h1_score") if not structure.get("error") else None,
            "text_quality_score": text.get("text_quality_score") if not text.get("error") else None,
            "readability_avg": text.get("readability_avg") if not text.get("error") else None,
            "seo_potential_avg": text.get("seo_potential_avg") if not text.get("error") else None,
            "structure_batches": structure.get("batches_total"),
            "text_batches": text.get("batches_total"),
        }
        parts = [s for s in [scores.get("structure_score"), scores.get("text_quality_score")]
                 if isinstance(s, (int, float))]
        scores["content_score"] = round(sum(parts) / len(parts) * 10, 1) if parts else None
        if errors:
            scores["llm_errors"] = errors

        executive = self._format_executive_md(synthesis, scores)

        return {
            "structure": structure,
            "text": text,
            "synthesis": synthesis,
            "scores": scores,
            "errors": errors,
            "executive": executive,
            "logic": self._format_structure_md(structure),
            "text_quality": self._format_text_md(text),
        }
