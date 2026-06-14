"""
SEO Auditor — точка входа.
Использование:
    python main.py https://example.com
    python main.py https://example.com --config config.yaml --no-llm
"""

import argparse
import os
import sys
import re
import yaml
from datetime import date, datetime
from pathlib import Path


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# Windows: принудительная utf-8 кодировка для вывода в консоль
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # перекрыть api_key из переменной окружения
    env_key = os.environ.get("SEO_LLM_API_KEY")
    if env_key:
        cfg["llm"]["api_key"] = env_key
    return cfg


def make_slug(url: str) -> str:
    """https://www.example.com/path → example-com"""
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    url = url.split("/")[0]
    url = re.sub(r"[^a-zA-Z0-9а-яА-Я]", "-", url)
    return url.strip("-").lower()


def make_audit_dir(base_url: str, base_dir: Path) -> Path:
    slug = make_slug(base_url)
    today = date.today().strftime("%Y-%m-%d")
    audit_dir = base_dir / f"{slug}_{today}"
    (audit_dir / "pages").mkdir(parents=True, exist_ok=True)
    return audit_dir


def main():
    parser = argparse.ArgumentParser(description="SEO Auditor")
    parser.add_argument("url", help="URL главной страницы сайта")
    parser.add_argument(
        "--config", default=str(Path(__file__).parent / "config.yaml"),
        help="Путь к config.yaml"
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Пропустить LLM-анализ (только локальные проверки)"
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help="Переопределить crawl.max_pages из config (0 = все URL из sitemap)"
    )
    parser.add_argument(
        "--all-pages", action="store_true",
        help="Обойти все страницы из sitemap (max_pages=0)"
    )
    parser.add_argument(
        "--max-depth", type=int, default=None,
        help="Переопределить crawl.max_depth из config"
    )
    parser.add_argument(
        "--output-dir", default=str(Path(__file__).parent / "audits"),
        help="Базовая папка для результатов"
    )
    parser.add_argument(
        "--from-audit", default=None,
        help="Папка готового аудита — пропустить краулинг, взять raw_data.json + aggregated.json"
    )
    parser.add_argument(
        "--llm-only", action="store_true",
        help="С --from-audit: только LLM + отчёт (без краулинга и локального анализа)"
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="С --from-audit: только пересобрать отчёты из сохранённых данных"
    )
    args = parser.parse_args()

    # загрузка конфига
    cfg = load_config(args.config)
    if args.all_pages:
        cfg["crawl"]["max_pages"] = 0
    elif args.max_pages is not None:
        cfg["crawl"]["max_pages"] = args.max_pages
    if args.max_depth is not None:
        cfg["crawl"]["max_depth"] = args.max_depth

    import json
    base_url = args.url.rstrip("/")
    output_base = Path(args.output_dir)

    if args.from_audit and not (args.llm_only or args.report_only):
        print("[SEO Auditor] --from-audit требует --llm-only или --report-only")
        sys.exit(1)

    if args.from_audit:
        audit_dir = Path(args.from_audit)
        if not audit_dir.is_dir():
            print(f"[SEO Auditor] Папка не найдена: {audit_dir}")
            sys.exit(1)
        raw_path = audit_dir / "raw_data.json"
        agg_path = audit_dir / "aggregated.json"
        if not raw_path.exists() or not agg_path.exists():
            print("[SEO Auditor] Нужны raw_data.json и aggregated.json в папке аудита")
            sys.exit(1)
        raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
        aggregated = json.loads(agg_path.read_text(encoding="utf-8"))
        llm_results = {}
        llm_path = audit_dir / "llm_results.json"
        if llm_path.exists():
            llm_results = json.loads(llm_path.read_text(encoding="utf-8"))
        _log(f"[SEO Auditor] Загружен аудит: {audit_dir} ({len(raw_data)} стр.)")
        if args.report_only:
            from reporter import Reporter
            reporter = Reporter(audit_dir, cfg)
            md_path, html_path = reporter.generate(base_url, raw_data, aggregated, llm_results)
            print(f"[SEO Auditor] Reports saved to: {audit_dir}")
            print(f"  -> {md_path}")
            print(f"  -> {html_path}")
            return
    else:
        audit_dir = make_audit_dir(base_url, output_base)
        raw_data = aggregated = None
        llm_results = {}

    _log(f"[SEO Auditor] URL: {base_url}")
    _log(f"[SEO Auditor] Папка аудита: {audit_dir}")

    if not args.from_audit or not args.llm_only:
        mp = cfg["crawl"]["max_pages"]
        mp_label = "все из sitemap" if mp == 0 else str(mp)
        _log(f"[SEO Auditor] Этап 1–3: краулинг · лимит {mp_label}, глубина {cfg['crawl']['max_depth']}")

        from crawler import Crawler
        crawler = Crawler(base_url, cfg, audit_dir)
        pages = crawler.run()
        _log(f"[SEO Auditor] Собрано страниц: {len(pages)}")

        from extractor import extract_all
        raw_data = extract_all(pages, cfg, audit_dir)
        _log(f"[SEO Auditor] Извлечены данные: {len(raw_data)} страниц")

        from analyzer import analyze
        aggregated = analyze(raw_data, cfg, audit_dir)
        _log("[SEO Auditor] Локальный анализ завершён")

    # --- Этап 4: LLM-анализ (пакетами по batch_size стр.) ---
    if args.from_audit and args.llm_only:
        llm_results = {}
    if not args.no_llm and cfg["llm"].get("api_key"):
        from llm_client import LLMClient
        batches = (len(raw_data) + cfg["llm"].get("batch_size", 40) - 1) // cfg["llm"].get("batch_size", 40)
        _log(f"[SEO Auditor] Этап 4: LLM (~{batches * 2 + 1} запросов, timeout {cfg['llm'].get('request_timeout', 120)}с)")
        llm = LLMClient(cfg["llm"])
        try:
            llm_results = llm.analyze(raw_data, aggregated, cfg)
        except Exception as e:
            _log(f"[SEO Auditor] LLM: критическая ошибка — {e}")
            llm_results = {"errors": [{"step": "analyze", "error": str(e)}], "scores": {}}
        llm_path = audit_dir / "llm_results.json"
        llm_path.write_text(json.dumps(llm_results, ensure_ascii=False, indent=2), encoding="utf-8")
        aggregated["llm_scores"] = llm_results.get("scores", {})
        agg_path = audit_dir / "aggregated.json"
        agg_path.write_text(
            json.dumps(aggregated, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        sc = aggregated["llm_scores"]
        if sc.get("llm_errors"):
            _log(f"[SEO Auditor] LLM: частично — ошибки: {sc['llm_errors']}")
        _log(f"[SEO Auditor] LLM: структура {sc.get('structure_score', '—')}/10, "
             f"тексты {sc.get('text_quality_score', '—')}/10, контент {sc.get('content_score', '—')}/100")
        _log(f"  -> {llm_path}")
    elif not args.no_llm:
        _log("[SEO Auditor] LLM пропущен: api_key не задан (config.yaml или SEO_LLM_API_KEY)")

    # --- Этап 5: Отчёт (MD + HTML с данными LLM внутри) ---
    _log("[SEO Auditor] Этап 5: генерация отчётов")
    from reporter import Reporter
    reporter = Reporter(audit_dir, cfg)
    md_path, html_path = reporter.generate(base_url, raw_data, aggregated, llm_results)
    _log(f"[SEO Auditor] Reports saved to: {audit_dir}")
    _log(f"  -> {md_path}")
    _log(f"  -> {html_path}")


if __name__ == "__main__":
    main()
