"""Small curated origin catalog for explicitly official-source research."""
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


_CATALOG_TEXT = Path(__file__).with_name("official_sources.json").read_text(encoding="utf-8")
CATALOG = json.loads(_CATALOG_TEXT)
CATALOG_VERSION = hashlib.sha256(_CATALOG_TEXT.encode()).hexdigest()[:12]


def requires_official_sources(query):
    for clause in re.split(r"[。！？!?；;，,\n]", query):
        for match in re.finditer(r"官方(?:文档|\s*GitHub|仓库|资料|来源)", clause, re.I):
            if not re.search(r"(?:不要|无需|不必|不需要|不用|非)[^。！？!?；;，,\n]{0,8}$", clause[:match.start()]):
                return True
    return False


def requested_entities(query):
    return [name for name, config in CATALOG.items()
            if any(re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", query, re.I)
                   for alias in config["aliases"])]


def _matches_origin(url, origin):
    try:
        actual, allowed = urlsplit(url), urlsplit(origin)
        if (actual.scheme != "https" or actual.hostname != allowed.hostname
                or actual.username is not None or actual.password is not None
                or actual.port not in (None, 443)):
            return False
        path = unquote(actual.path).lower().rstrip("/")
        root = allowed.path.lower().rstrip("/")
        if any(part in {".", ".."} for part in path.split("/")):
            return False
        if allowed.hostname == "github.com" and root:
            # User-created issues/discussions are not maintainer documentation.
            return path == root or any(path.startswith(root + suffix) for suffix in ("/blob/", "/tree/", "/releases/")) or path == root + "/releases"
        return not root or path == root or path.startswith(root + "/")
    except (TypeError, ValueError):
        return False


def approved_source(record, query):
    return any(_matches_origin(record.get("url", ""), origin)
               for name in requested_entities(query) for origin in CATALOG[name]["origins"])


def official_queries(query, base_queries):
    """Keep the planner's topic while constraining each lookup to known origins."""
    if not base_queries:
        return []
    topic = re.sub(r"site:\S+", "", base_queries[0]["query"]).strip()
    result = []
    for name in requested_entities(query):
        for origin in CATALOG[name]["origins"]:
            target = urlsplit(origin)
            result.append({**base_queries[0], "query": f"{topic[:350]} site:{target.hostname}{target.path}",
                           "include_domains": [target.hostname], "source_preference": "web",
                           "reason": "用户要求官方来源；按已核对的入口限定检索"})
    return result[:6]
