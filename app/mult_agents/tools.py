"""工具模块：封装 Web 检索、本地 RAG 查询与通用辅助工具函数。"""

from datetime import datetime
import ast
import json
import logging
import operator
import os
import re
import socket
import time
from contextlib import nullcontext
from pathlib import Path
from dataclasses import replace
from threading import Lock
import urllib.error
import urllib.request

from langchain_core.tools import tool
from typing import Optional
from .rag.core import RAGSystem, RAGConfig
from .harness.runtime import ExecutionError, current

logger = logging.getLogger("mult_agents")

# 全局 RAG 系统实例
_RAG_SYSTEM: Optional[RAGSystem] = None
_SCOPED_RAG = {}
_RAG_LOCK = Lock()


def _knowledge_collections(context):
    from .rag.scope import private_collection
    public = os.getenv("RESEARCH_PUBLIC_KNOWLEDGE_COLLECTION", "").strip()
    names = [public] if public else []
    if context and context.scope:
        tenant, user = context.scope
        names.extend([private_collection(_RAG_SYSTEM.config.collection_name, tenant),
                      private_collection(_RAG_SYSTEM.config.collection_name, tenant, user)])
    return names


def _scoped_rag(name):
    if name == _RAG_SYSTEM.config.collection_name:
        return _RAG_SYSTEM
    with _RAG_LOCK:
        if name not in _SCOPED_RAG:
            _SCOPED_RAG[name] = RAGSystem(_RAG_SYSTEM.api_key, replace(_RAG_SYSTEM.config, collection_name=name))
        return _SCOPED_RAG[name]

def init_rag_system(api_key: str, config: Optional[RAGConfig] = None):
    """初始化全局 RAG 系统"""
    global _RAG_SYSTEM
    if _RAG_SYSTEM is None:
        try:
            _RAG_SYSTEM = RAGSystem(api_key, config)
        except Exception as e:
            logger.warning("RAG 初始化失败 | exception_type=%s", type(e).__name__)


def search_knowledge_base_records(query: str, limit: int = 5) -> list[dict]:
    if _RAG_SYSTEM is None:
        raise ExecutionError("RAG_UNAVAILABLE", "knowledge")
    context = current()
    if context:
        context.reserve("local")
    try:
        with context.span("tool", "search_knowledge") if context else nullcontext():
            records = []
            for name in _knowledge_collections(context):
                records.extend(_scoped_rag(name).search_records(query, k=min(limit, 4)))
            # Each collection has its own top-k; prefer private evidence when merging.
            records = records[-min(limit, 4):]
            if context:
                context.emit({"type": "retrieval", "provider": "knowledge", "result_count": len(records)})
            return records
    except ExecutionError:
        raise
    except Exception as exc:
        raise ExecutionError("RAG_UNAVAILABLE", "knowledge") from exc


def bocha_web_search_records(query: str, count: int = 8, include_domains: list[str] | None = None) -> list[dict]:
    api_key = os.getenv("BOCHA_API_KEY", "").strip()
    context = current()
    if context and "bocha" in context.blocked_providers:
        code = next((item["code"] for item in reversed(context.errors)
                     if item["provider"] == "bocha" and item["code"] in {"AUTH_ERROR", "QUOTA_EXCEEDED"}), "AUTH_ERROR")
        raise ExecutionError(code, "bocha")
    if not api_key:
        raise ExecutionError("AUTH_ERROR", "bocha")
    count = max(1, min(count, 8))
    payload = {
        "query": (re.sub(r"site:\S+", "", query).strip() if include_domains else query)[:500],
        "summary": True,
        "freshness": "noLimit",
        "count": count,
    }
    if include_domains:
        payload["include"] = ",".join(dict.fromkeys(include_domains[:10]))
    request = urllib.request.Request(
        url="https://api.bochaai.com/v1/web-search",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(3):
        if context:
            context.reserve("web")
        try:
            with context.span("tool", "search_web") if context else nullcontext():
                with urllib.request.urlopen(request, timeout=10) as response:
                    raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ExecutionError("PARSE_ERROR", "bocha")
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
                    raise ExecutionError("PARSE_ERROR", "bocha")
            break
        except ExecutionError:
            raise
        except urllib.error.HTTPError as exc:
            code = "AUTH_ERROR" if exc.code in (401, 403) else "RATE_LIMIT" if exc.code == 429 else "NETWORK_ERROR" if exc.code >= 500 else "PARSE_ERROR"
            if exc.code == 403:
                # Bocha also returns 403 for insufficient balance/quota; do not mislabel it as a bad key.
                try:
                    body = json.loads(exc.read(4096).decode("utf-8"))
                    message = str(body.get("message") or body.get("msg") or "").lower()
                except (ValueError, UnicodeError, AttributeError, TypeError, OSError):
                    message = ""
                if any(marker in message for marker in ("not have enough money or package quota", "not enough money or package quota", "insufficient balance", "quota exhausted", "余额不足", "额度不足")):
                    code = "QUOTA_EXCEEDED"
            error = ExecutionError(code, "bocha", exc.code == 429 or exc.code >= 500)
        except (socket.timeout, TimeoutError):
            error = ExecutionError("TIMEOUT", "bocha", True)
        except urllib.error.URLError as exc:
            code = "TIMEOUT" if isinstance(exc.reason, (socket.timeout, TimeoutError)) else "NETWORK_ERROR"
            error = ExecutionError(code, "bocha", True)
        except (json.JSONDecodeError, UnicodeError):
            raise ExecutionError("PARSE_ERROR", "bocha")
        if not error.retryable or attempt == 2:
            raise error
        if context:
            context.emit({"type": "retry", "provider": "bocha", "code": error.code, "attempt": attempt + 1})
        time.sleep(.1 * (attempt + 1))
    data = result["data"]
    pages = data.get("webPages", [])
    if isinstance(pages, dict):
        if isinstance(pages.get("value"), list):
            pages = pages.get("value", [])
        elif isinstance(pages.get("items"), list):
            pages = pages.get("items", [])
        else:
            raise ExecutionError("PARSE_ERROR", "bocha")
    if not isinstance(pages, list):
        raise ExecutionError("PARSE_ERROR", "bocha")
    records: list[dict] = []
    for idx, page in enumerate(pages[:count], 1):
        if not isinstance(page, dict):
            logger.warning("[bocha_web_search] 第 %s 条记录格式异常 | type=%s", idx, type(page).__name__)
            continue
        url = str(page.get("url") or "").strip()
        domain = ""
        if "://" in url:
            domain = url.split("://", 1)[1].split("/", 1)[0]
        title = page.get("name") or f"web_result_{idx}"
        snippet = str(page.get("summary") or "")[:1800]
        records.append(
            {
                "source_id": f"WEB-{idx}",
                "title": title,
                "url": url,
                "snippet": snippet,
                "domain": domain,
                "source_type": "web",
                "retrieval_level": "summary_only",
                "published_at": page.get("datePublished") or page.get("dateLastCrawled") or "",
            }
        )
    if context:
        context.emit({"type": "retrieval", "provider": "bocha", "result_count": len(records), "retrieval_level": "summary_only"})
    return records

@tool
def search_knowledge_base(query: str) -> str:
    """
    查询本地知识库/向量数据库。
    当用户询问关于专业知识、历史文档或私有数据时使用此工具。
    输入应该是具体的查询问题。
    """
    if _RAG_SYSTEM is None:
        return "错误：RAG 系统未初始化或连接失败。请检查 Milvus 服务状态。"
    return _RAG_SYSTEM.search(query)


ALLOWED_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}


def _eval_node(node):
    if isinstance(node, ast.Num):
        return node.n
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in ALLOWED_OPERATORS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        return ALLOWED_OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    raise ValueError("Unsupported expression")


@tool
def get_current_time() -> str:
    """返回当前时间的 ISO 字符串。"""
    return datetime.now().isoformat()


@tool
def simple_calculator(expression: str) -> str:
    """计算简单算术表达式并返回结果。"""
    tree = ast.parse(expression, mode="eval")
    result = _eval_node(tree.body)
    return str(result)


@tool
def extract_requirements(text: str) -> str:
    """从文本中提取需求要点列表。"""
    items = [part.strip() for part in text.replace("\n", " ").split("。") if part.strip()]
    return "\n".join(f"- {item}" for item in items[:8])


@tool
def outline_from_topics(topics: str) -> str:
    """根据主题列表生成编号大纲。"""
    raw = topics.replace("\n", ",")
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return "\n".join(f"{idx+1}. {item}" for idx, item in enumerate(items[:10]))


@tool
def merge_notes(note_a: str, note_b: str) -> str:
    """合并两段文本为一段笔记。"""
    return f"{note_a}\n{note_b}".strip()


@tool
def summarize_points(text: str) -> str:
    """从文本中抽取要点列表。"""
    sentences = [s.strip() for s in text.replace("\n", " ").split("。") if s.strip()]
    points = sentences[:6]
    return "\n".join(f"- {p}" for p in points)


@tool
def dedupe_lines(text: str) -> str:
    """对文本按行去重并输出。"""
    seen = set()
    lines = []
    for line in text.splitlines():
        key = line.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines)


@tool
def web_search_stub(query: str) -> str:
    """网络检索接口（Bocha Web Search）。"""
    records = bocha_web_search_records(query, count=5)
    if not records:
        return "未配置 BOCHA_API_KEY，无法执行网络检索。"
    lines = ["Bocha 检索结果："]
    for idx, record in enumerate(records, 1):
        lines.append(f"{idx}. {record['title']}")
        url = record.get("url", "")
        if url:
            lines.append(f"   链接: {url}")
        snippet = record.get("snippet", "")
        if snippet:
            lines.append(f"   摘要: {snippet[:200]}")
    return "\n".join(lines)


@tool
def local_docs_lookup_stub(query: str) -> str:
    """模拟本地检索接口。"""
    return f"未配置本地检索服务，收到查询: {query}"


@tool
def local_vector_search_stub(query: str) -> str:
    """模拟向量数据库检索接口。"""
    return f"未配置向量数据库，收到查询: {query}"


@tool
def optimize_query(query: str) -> str:
    """对检索问题进行改写与优化。"""
    return f"优化后的查询建议: {query}"


@tool
def explain_term(term: str) -> str:
    """解释领域术语。"""
    return f"{term} 需要结合上下文进一步解释"


@tool
def python_inter(code: str) -> str:
    """模拟 Python 执行环境。"""
    return f"未配置Python执行环境，收到代码: {code}"


@tool
def fig_inter(spec: str) -> str:
    """模拟绘图执行环境。"""
    return f"未配置绘图环境，收到图表需求: {spec}"


@tool
def amap_weather(city: str) -> str:
    """模拟高德天气查询。"""
    return f"未配置高德API，收到天气查询: {city}"


@tool
def amap_geocode(address: str) -> str:
    """模拟高德地理编码。"""
    return f"未配置高德API，收到地理编码请求: {address}"


@tool
def amap_poi_search(query: str) -> str:
    """模拟高德 POI 检索。"""
    return f"未配置高德API，收到POI检索: {query}"


@tool
def amap_route_plan(origin: str, destination: str) -> str:
    """模拟高德路径规划。"""
    return f"未配置高德API，收到路径规划: {origin} -> {destination}"


def _workspace_root() -> Path:
    base = os.getenv("WORKSPACE_DIR", "/workspace")
    return Path(base).resolve()


def _safe_path(path: str) -> Path:
    root = _workspace_root()
    target = (root / path).resolve()
    if root not in target.parents and target != root:
        raise ValueError("路径超出工作目录")
    return target


@tool
def safe_list_dir(path: str = ".") -> str:
    """安全列出工作目录下的文件与子目录。"""
    root = _workspace_root()
    if not root.exists():
        return f"工作目录不存在: {root}"
    target = _safe_path(path)
    if not target.exists() or not target.is_dir():
        return "目录不存在"
    items = [p.name for p in target.iterdir()]
    return "\n".join(items)


@tool
def safe_read_file(path: str) -> str:
    """安全读取工作目录内的文件。"""
    root = _workspace_root()
    if not root.exists():
        return f"工作目录不存在: {root}"
    target = _safe_path(path)
    if not target.exists() or not target.is_file():
        return "文件不存在"
    return target.read_text(encoding="utf-8")


@tool
def safe_write_file(path: str, content: str) -> str:
    """安全写入工作目录内的文件。"""
    root = _workspace_root()
    if not root.exists():
        return f"工作目录不存在: {root}"
    target = _safe_path(path)
    if not target.parent.exists():
        return "目录不存在"
    target.write_text(content, encoding="utf-8")
    return f"已写入: {target}"


@tool
def safe_move_file(src: str, dst: str) -> str:
    """安全移动工作目录内的文件。"""
    root = _workspace_root()
    if not root.exists():
        return f"工作目录不存在: {root}"
    src_path = _safe_path(src)
    dst_path = _safe_path(dst)
    if not src_path.exists():
        return "源文件不存在"
    if not dst_path.parent.exists():
        return "目标目录不存在"
    src_path.replace(dst_path)
    return f"已移动: {dst_path}"


@tool
def sql_inter(query: str) -> str:
    """模拟 SQL 执行接口。"""
    return f"未配置数据库，收到SQL: {query}"


@tool
def extract_data_stub(query: str) -> str:
    """模拟数据抽取接口。"""
    return f"未配置数据抽取环境，收到请求: {query}"


@tool
def execute_terminal_command(command: str) -> str:
    """模拟终端命令执行接口。"""
    return f"未配置终端执行环境，收到命令: {command}"


@tool
def file_operation_stub(request: str) -> str:
    """模拟文件操作接口。"""
    return f"未配置文件操作环境，收到请求: {request}"


@tool
def news_search_stub(query: str) -> str:
    """模拟新闻检索接口。"""
    return f"未配置新闻检索服务，收到查询: {query}"


@tool
def finance_search_stub(query: str) -> str:
    """模拟金融检索接口。"""
    return f"未配置金融检索服务，收到查询: {query}"


@tool
def extract_url_content_stub(url: str) -> str:
    """模拟 URL 内容抽取接口。"""
    return f"未配置URL解析服务，收到URL: {url}"
