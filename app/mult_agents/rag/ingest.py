import argparse
import logging
import os
import sys
from pathlib import Path

# 支持直接执行该脚本；先设置 app 路径，再导入项目模块。
project_root = Path(__file__).resolve().parents[3]
app_path = project_root / "app"
if str(app_path) not in sys.path:
    sys.path.insert(0, str(app_path))

# 先加载 .env，再导入其他模块（确保 Milvus 配置正确）
from dotenv import load_dotenv
env_path = project_root / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
load_dotenv(project_root / ".env.local", override=True)






EMBEDDING_MODEL = "text-embedding-v1"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50


def _collect_paths(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() not in {".txt", ".md", ".markdown"}:
            raise ValueError("只支持 UTF-8 编码的 .txt、.md、.markdown 文件")
        return [input_path]
    patterns = ("*.txt", "*.md", "*.markdown")
    paths: list[Path] = []
    for pat in patterns:
        paths.extend(sorted(input_path.rglob(pat)))
    return paths


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="导入 UTF-8 文本到本地知识库")
    parser.add_argument("input", type=Path, help="文本文件或目录")
    parser.add_argument("--collection", help="覆盖知识库集合名")
    parser.add_argument("--tenant-id", help="私有资料所属租户（本地管理员操作）")
    parser.add_argument("--user-id", help="私有资料所属用户；省略则租户内共享")
    parser.add_argument("--public", action="store_true", help="明确确认导入公共资料")
    args = parser.parse_args(argv)
    if bool(args.tenant_id) == args.public or (args.user_id and not args.tenant_id):
        parser.error("须选择 --public 或 --tenant-id；--user-id 需要 --tenant-id")
    import re
    if any(v and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", v) for v in (args.tenant_id, args.user_id)):
        parser.error("身份仅支持 1–80 位英文、数字、下划线、连字符")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    input_path = args.input.expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(str(input_path))
    paths = _collect_paths(input_path)
    if not paths:
        raise ValueError(f"未找到可入库文件: {input_path}")

    from mult_agents.config import AppConfig
    from mult_agents.rag.core import RAGSystem, RAGConfig

    config = AppConfig.from_file()
    from mult_agents.rag.scope import private_collection
    base = os.getenv("KNOWLEDGE_COLLECTION") or config.milvus_collection
    if args.public:
        collection_name = args.collection or os.getenv("RESEARCH_PUBLIC_KNOWLEDGE_COLLECTION")
        if not collection_name:
            parser.error("公共资料需要配置 RESEARCH_PUBLIC_KNOWLEDGE_COLLECTION 或指定 --collection")
    else:
        if args.collection:
            parser.error("私有资料集合名由服务端规则生成，不支持 --collection")
        collection_name = private_collection(base, args.tenant_id, args.user_id)
    rag_cfg = RAGConfig(
        milvus_host=config.milvus_host,
        milvus_port=config.milvus_port,
        collection_name=collection_name,
        embedding_model=EMBEDDING_MODEL,
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    rag = RAGSystem(api_key=config.api_key, config=rag_cfg)

    total_chunks = rag.ingest_paths(paths)
    print(f"入库完成 | 文件数={len(paths)} | chunk数={total_chunks} | collection={collection_name}")


if __name__ == "__main__":
    main()
