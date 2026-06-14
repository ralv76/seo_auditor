"""
Тестовый скрипт для диагностики работы с LLM.
Проверяет разные варианты подключения к OpenAI-совместимому API.
"""

import yaml
import json
from openai import OpenAI

CONFIG_PATH = "config.yaml"


def load_cfg():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_variant(label: str, base_url: str, api_key: str, model: str, extra_headers=None):
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"  base_url: {base_url}")
    print(f"  model   : {model}")
    print(f"  extra_headers: {extra_headers}")

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        # Пробуем простой запрос
        resp = client.chat.completions.create(
            model=model,
            temperature=0.2,
            max_tokens=100,
            messages=[{"role": "user", "content": "Say hello in one word."}],
            extra_headers=extra_headers or {},
        )
        content = resp.choices[0].message.content
        print(f"  [OK] SUCCESS: {content!r}")
        return True, content
    except Exception as e:
        print(f"  [ERR] ERROR: {e}")
        return False, str(e)


def main():
    cfg = load_cfg()
    llm = cfg["llm"]

    raw_url = llm["url"]
    api_key = llm["api_key"]
    model = llm["model"]

    # Пытаемся вычислить base_url несколькими способами
    candidates = []

    # 1. Как есть (полный URL)
    candidates.append(("Raw URL (as-is)", raw_url))

    # 2. Без /chat/completions
    if raw_url.endswith("/chat/completions"):
        candidates.append(("Without /chat/completions", raw_url[: -len("/chat/completions")]))
    # 3. Без последнего сегмента path
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(raw_url)
    path = parts.path.rsplit("/", 1)[0]  # strip last segment
    candidates.append(("Strip last path segment", urlunsplit(parts._replace(path=path))))

    # 4. Просто origin + /v1
    origin = f"{parts.scheme}://{parts.netloc}"
    candidates.append(("Origin + /v1", origin + "/v1"))
    candidates.append(("Origin + /api/v1", origin + "/api/v1"))
    candidates.append(("Origin only", origin))

    results = []
    for label, url in candidates:
        ok, result = test_variant(label, url, api_key, model)
        results.append({"label": label, "url": url, "ok": ok, "result": result})

    # Попробуем с заголовками
    print(f"\n{'='*60}")
    print("Extra headers test (если провайдер требует HTTP-Referer etc.)")
    ok, result = test_variant(
        "With HTTP-Referer",
        raw_url[: -len("/chat/completions")] if raw_url.endswith("/chat/completions") else raw_url,
        api_key,
        model,
        extra_headers={"HTTP-Referer": "https://example.com", "X-Title": "SEO Auditor"},
    )
    results.append({"label": "With HTTP-Referer", "url": raw_url[: -len("/chat/completions")] if raw_url.endswith("/chat/completions") else raw_url, "ok": ok, "result": result})

    # Итог
    print(f"\n{'='*60}")
    print("SUMMARY")
    for r in results:
        status = "[OK]" if r["ok"] else "[ERR]"
        print(f"  {status} {r['label']}: {r['url']}")

    # Если получилось — выводим рекомендацию
    success = [r for r in results if r["ok"]]
    if success:
        print(f"\n[OK] Рабочий base_url: {success[0]['url']}")
        print(f"   Обнови config.yaml -> llm.url: {success[0]['url']}")
    else:
        print("\n[!] Ни один вариант не сработал.")
        print("   Проверь:")
        print("   - API-ключ действителен?")
        print("   - Баланс / лимиты на аккаунте?")
        print("   - Провайдер требует доп. заголовков?")


if __name__ == "__main__":
    print("LLM Diagnostic Tool")
    print("=" * 60)
    main()
