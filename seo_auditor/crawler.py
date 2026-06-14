"""
Crawler — обход сайта.
Порядок:
  1. robots.txt → парсинг Disallow/Sitemap
  2. sitemap.xml → список URL
  3. BFS по внутренним ссылкам до max_depth / max_pages
Сохраняет HTML страниц в audit_dir/pages/
"""

import re
import time
import json
import hashlib
import requests
from pathlib import Path
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET


class Crawler:
    def __init__(self, base_url: str, cfg: dict, audit_dir: Path):
        self.base_url = base_url
        self.base_domain = urlparse(base_url).netloc
        self.cfg = cfg
        self.audit_dir = audit_dir
        self.pages_dir = audit_dir / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)

        c = cfg["crawl"]
        self.max_pages = c.get("max_pages", 20)
        self.max_depth = c.get("max_depth", 3)
        self.strategy = c.get("strategy", "sitemap_first")  # sitemap_first | bfs
        self.unlimited = self.max_pages == 0
        self.timeout = c.get("timeout", 15)
        self.delay = c.get("delay", 0.5)
        self.user_agent = c.get("user_agent", "SEO-Auditor/1.0")
        self.save_html = cfg["output"].get("save_html_pages", True)

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})

        self.visited: set = set()
        self.disallowed: list = []
        self.pages: list = []  # [{url, status, html, headers, depth, source}]

        # сохраняем robots.txt и sitemap данные
        self.robots_txt: str = ""
        self.sitemap_urls: list = []

    # ------------------------------------------------------------------
    # robots.txt
    # ------------------------------------------------------------------
    def fetch_robots(self) -> str:
        url = f"{self.base_url}/robots.txt"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                self.robots_txt = r.text
                print(f"  [robots.txt] OK ({len(r.text)} chars)")
                self._parse_robots(r.text)
                return r.text
            else:
                print(f"  [robots.txt] HTTP {r.status_code}")
        except Exception as e:
            print(f"  [robots.txt] Ошибка: {e}")
        return ""

    def _parse_robots(self, text: str):
        """Извлекаем Disallow и Sitemap директивы."""
        sitemap_urls = []
        in_global = False
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("user-agent:"):
                agent = line.split(":", 1)[1].strip()
                in_global = (agent == "*")
            elif in_global and line.lower().startswith("disallow:"):
                path = line.split(":", 1)[1].strip()
                if path:
                    self.disallowed.append(path)
            elif line.lower().startswith("sitemap:"):
                sm_url = line.split(":", 1)[1].strip()
                # sitemap: может начинаться с http/https, восстанавливаем
                if sm_url.startswith("//"):
                    sm_url = "https:" + sm_url
                sitemap_urls.append(sm_url)

        # если sitemap не указан в robots — пробуем стандартный путь
        if not sitemap_urls:
            sitemap_urls.append(f"{self.base_url}/sitemap.xml")

        for sm in sitemap_urls:
            self._fetch_sitemap(sm)

    # ------------------------------------------------------------------
    # sitemap.xml
    # ------------------------------------------------------------------
    def _fetch_sitemap(self, url: str, depth: int = 0):
        if depth > 3:
            return
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code != 200:
                print(f"  [sitemap] {url} → HTTP {r.status_code}")
                return
            content = r.text.strip()
            root = ET.fromstring(content)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

            # sitemapindex — содержит ссылки на другие sitemap
            for sitemap_el in root.findall("sm:sitemap/sm:loc", ns):
                self._fetch_sitemap(sitemap_el.text.strip(), depth + 1)

            # urlset — содержит страницы
            for url_el in root.findall("sm:url/sm:loc", ns):
                loc = url_el.text.strip()
                if self._is_internal(loc) and loc not in self.sitemap_urls:
                    self.sitemap_urls.append(loc)

            print(f"  [sitemap] {url} → {len(self.sitemap_urls)} URL")
        except ET.ParseError:
            # попытка как html-sitemap
            pass
        except Exception as e:
            print(f"  [sitemap] {url} → Ошибка: {e}")

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------
    def _is_internal(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc == self.base_domain or parsed.netloc == ""

    def _is_allowed(self, url: str) -> bool:
        path = urlparse(url).path
        for dis in self.disallowed:
            if path.startswith(dis):
                return False
        return True

    def _normalize(self, url: str, base: str) -> str | None:
        """Абсолютный URL, без якоря, без лишних параметров."""
        url, _ = urldefrag(url)
        url = urljoin(base, url)
        parsed = urlparse(url)
        # только http/https, только внутренние
        if parsed.scheme not in ("http", "https"):
            return None
        if not self._is_internal(url):
            return None
        # убираем trailing slash для единообразия (кроме корня)
        normalized = parsed._replace(fragment="").geturl()
        if normalized != self.base_url and normalized.endswith("/"):
            normalized = normalized.rstrip("/")
        return normalized

    def _url_to_filename(self, url: str) -> str:
        path = urlparse(url).path.strip("/").replace("/", "__") or "index"
        path = re.sub(r"[^\w\-]", "_", path)
        if len(path) > 80:
            path = path[:80] + "_" + hashlib.md5(url.encode()).hexdigest()[:6]
        return path + ".html"

    def _skip_extension(self, url: str) -> bool:
        skip = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".avif",
                ".bmp", ".tiff", ".tif", ".heic", ".ico",
                ".zip", ".doc", ".docx", ".xls", ".xlsx", ".css", ".js",
                ".mp4", ".mp3", ".avi", ".mov", ".webm", ".ogg", ".wav",
                ".woff", ".woff2", ".ttf", ".eot", ".xml"}
        ext = Path(urlparse(url).path).suffix.lower()
        if ext in skip:
            return True
        path = urlparse(url).path.lower()
        for seg in ("/images/", "/image/", "/img/", "/uploads/", "/media/", "/assets/img"):
            if seg in path:
                return True
        return False

    def _filter_sitemap_html(self):
        """Оставить в sitemap только HTML-страницы (без картинок и файлов)."""
        if not self.cfg["crawl"].get("sitemap_html_only", True):
            return
        before = len(self.sitemap_urls)
        self.sitemap_urls = [u for u in self.sitemap_urls if not self._skip_extension(u)]
        print(f"  [sitemap] HTML-only: {len(self.sitemap_urls)} из {before} URL")

    # ------------------------------------------------------------------
    # Получение страницы
    # ------------------------------------------------------------------
    def _fetch_page(self, url: str) -> dict | None:
        try:
            r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            final_url = r.url
            # Если редирект привёл на другой домен — обновляем base_domain
            final_domain = urlparse(final_url).netloc
            if final_domain and final_domain != self.base_domain:
                print(f"  [redirect] {self.base_domain} -> {final_domain}, обновляем base_domain")
                self.base_domain = final_domain
                self.base_url = f"{urlparse(final_url).scheme}://{final_domain}"

            headers = dict(r.headers)
            html = ""
            ct = headers.get("Content-Type", headers.get("content-type", ""))
            if "text/html" in ct:
                html = r.text
            return {
                "url": url,
                "final_url": final_url,
                "status": r.status_code,
                "html": html,
                "headers": {k.lower(): v for k, v in headers.items()},
                "redirected": (final_url.rstrip("/") != url.rstrip("/")),
            }
        except requests.exceptions.Timeout:
            return {"url": url, "final_url": url, "status": 0,
                    "html": "", "headers": {}, "redirected": False,
                    "error": "timeout"}
        except Exception as e:
            return {"url": url, "final_url": url, "status": 0,
                    "html": "", "headers": {}, "redirected": False,
                    "error": str(e)}

    def _extract_links(self, html: str, page_url: str) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        links = []
        for tag in soup.find_all("a", href=True):
            norm = self._normalize(tag["href"], page_url)
            if norm and not self._skip_extension(norm):
                links.append(norm)
        return list(dict.fromkeys(links))  # дедупликация с сохранением порядка

    def _save_html(self, url: str, html: str):
        fname = self._url_to_filename(url)
        fpath = self.pages_dir / fname
        fpath.write_text(html, encoding="utf-8")
        return str(fpath)

    # ------------------------------------------------------------------
    # BFS
    # ------------------------------------------------------------------
    def run(self) -> list:
        print("[Crawler] Получаем robots.txt и sitemap...")
        self.fetch_robots()
        self._filter_sitemap_html()

        # Сохраняем robots.txt
        robots_path = self.audit_dir / "robots.txt"
        robots_path.write_text(self.robots_txt, encoding="utf-8")

        # Лимит: 0 = все URL из sitemap (или без ограничения)
        effective_max = self.max_pages
        if self.unlimited and self.sitemap_urls:
            effective_max = len(self.sitemap_urls)
            print(f"[Crawler] max_pages=0 → обход всех {effective_max} URL из sitemap")
        elif self.unlimited:
            effective_max = 10_000
            print("[Crawler] max_pages=0, sitemap пуст → лимит 10000")

        # Очередь: (url, depth, from_sitemap)
        queue = deque()
        seen_in_queue = set()

        def enqueue(url, depth, from_sitemap=False):
            if url and url not in seen_in_queue and url not in self.visited:
                seen_in_queue.add(url)
                queue.append((url, depth, from_sitemap))

        if self.strategy == "sitemap_first" and self.sitemap_urls:
            for sm_url in self.sitemap_urls:
                norm = self._normalize(sm_url, self.base_url)
                enqueue(norm, 1, from_sitemap=True)
            # главная первой, если не в sitemap
            home = self._normalize(self.base_url, self.base_url)
            if home and home not in seen_in_queue:
                queue.appendleft((home, 0, False))
        else:
            enqueue(self._normalize(self.base_url, self.base_url), 0, False)
            for sm_url in self.sitemap_urls:
                norm = self._normalize(sm_url, self.base_url)
                enqueue(norm, 1, from_sitemap=True)

        print(f"[Crawler] Стратегия: {self.strategy}. В очереди: {len(queue)} URL, лимит: {effective_max}")

        while queue and len(self.pages) < effective_max:
            url, depth, _from_sm = queue.popleft()

            if url in self.visited:
                continue
            if not self._is_allowed(url):
                print(f"  [skip] Disallowed: {url}")
                continue
            if self._skip_extension(url):
                continue

            self.visited.add(url)

            print(f"  [{len(self.pages)+1}/{effective_max}] depth={depth} {url}")
            page = self._fetch_page(url)
            page["depth"] = depth

            if self.save_html and page["html"]:
                page["html_file"] = self._save_html(url, page["html"])
            else:
                page["html_file"] = None

            self.pages.append(page)
            time.sleep(self.delay)

            # BFS-ссылки — только если не исчерпали sitemap-лимит и стратегия не чистый sitemap
            if page["html"] and depth < self.max_depth and self.strategy != "sitemap_only":
                links = self._extract_links(page["html"], url)
                for link in links:
                    enqueue(link, depth + 1, False)

        # Сохраняем список всех собранных URL
        urls_path = self.audit_dir / "crawled_urls.json"
        urls_data = {
            "base_url": self.base_url,
            "robots_txt_found": bool(self.robots_txt),
            "sitemap_urls_count": len(self.sitemap_urls),
            "sitemap_urls": self.sitemap_urls,
            "crawled": [
                {"url": p["url"], "status": p["status"], "depth": p["depth"]}
                for p in self.pages
            ]
        }
        urls_path.write_text(json.dumps(urls_data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

        return self.pages
