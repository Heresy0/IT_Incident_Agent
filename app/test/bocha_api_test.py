import json
import os
import urllib.error
import urllib.request


BASE_URL = "https://api.bocha.cn/v1"
WEB_SEARCH_ENDPOINT = f"{BASE_URL}/web-search"
AI_SEARCH_ENDPOINT = f"{BASE_URL}/ai-search"

WEB_SEARCH_PAYLOAD = {
    "query": "帮我调查下openclaw的最新使用趋势",
    "summary": True,
    "freshness": "noLimit",
    "count": 10,
}

AI_SEARCH_PAYLOAD = {
    "query": "北京天气",
    "freshness": "noLimit",
    "count": 10,
    "answer": False,
    "stream": False,
}


def call_bocha_api(url: str, api_key: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response_text = response.read().decode("utf-8")
    return json.loads(response_text)


def summarize_result(name: str, result: dict):
    print(f"\n=== {name} ===")
    print("top_level_keys:", list(result.keys()))
    if "code" in result:
        print("code:", result.get("code"))
    if "msg" in result:
        print("msg:", result.get("msg"))
    data = result.get("data")
    if isinstance(data, dict):
        print("data_keys:", list(data.keys()))
        web_pages = data.get("webPages")
        if isinstance(web_pages, list):
            print("webPages_count:", len(web_pages))
            if web_pages:
                first = web_pages[0]
                if isinstance(first, dict):
                    preview = {
                        "name": first.get("name"),
                        "url": first.get("url"),
                        "summary": first.get("summary"),
                    }
                    print("webPages_first:", json.dumps(preview, ensure_ascii=False))
    print("raw:", json.dumps(result, ensure_ascii=False)[:1200])


def main():
    api_key = os.getenv("BOCHA_API_KEY", "").strip()
    if not api_key:
        raise ValueError("缺少 BOCHA_API_KEY 环境变量")
    try:
        web_result = call_bocha_api(WEB_SEARCH_ENDPOINT, api_key, WEB_SEARCH_PAYLOAD)
        summarize_result("Web Search", web_result)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        print("\n=== Web Search HTTPError ===")
        print("status:", exc.code)
        print("body:", body[:1200])
    except Exception as exc:
        print("\n=== Web Search Exception ===")
        print(str(exc))
    try:
        ai_result = call_bocha_api(AI_SEARCH_ENDPOINT, api_key, AI_SEARCH_PAYLOAD)
        summarize_result("AI Search", ai_result)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        print("\n=== AI Search HTTPError ===")
        print("status:", exc.code)
        print("body:", body[:1200])
    except Exception as exc:
        print("\n=== AI Search Exception ===")
        print(str(exc))


if __name__ == "__main__":
    main()
