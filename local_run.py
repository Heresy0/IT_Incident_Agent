"""IT 工单本地入口；研究依赖仅在显式兼容模式下使用。"""
import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def load_local_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env.local", override=True)
    load_dotenv(ROOT / ".env", override=False)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / "app"))


def check(legacy=False) -> None:
    from backend.config.incident import IncidentConfig
    import psycopg

    config = IncidentConfig.from_file(ROOT / 'app' / 'config.json')
    print(f"模型 Key: {'已配置（未验证有效性）' if config.api_key else '未配置，可运行免费控制模式'}")
    with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
        print(f"PostgreSQL: {'正常' if conn.execute('SELECT 1').fetchone()[0] == 1 else '异常'}")
    if not legacy:
        return
    from mult_agents.config import AppConfig
    from pymilvus import connections, utility
    config = AppConfig.from_file()
    connections.connect(alias="local_check", host=config.milvus_host, port=config.milvus_port, timeout=10)
    try:
        print(f"Milvus: {utility.get_server_version(using='local_check')}")
        print(f"知识库集合: {os.getenv('KNOWLEDGE_COLLECTION')}")
        print(f"记忆集合: {config.milvus_collection}")
    finally:
        connections.disconnect("local_check")


def main() -> None:
    parser = argparse.ArgumentParser(description="IT Incident Agent 本地启动入口")
    parser.add_argument("command", choices=["backend", "frontend", "check", "init", "ingest"])
    parser.add_argument("input", nargs="?", help="ingest 的 UTF-8 文本文件或目录")
    parser.add_argument("--public", action="store_true", help="确认入库资料可以公开共享")
    parser.add_argument("--tenant-id", help="私有资料所属租户")
    parser.add_argument("--user-id", help="私有资料所属用户；省略则租户内共享")
    parser.add_argument('--legacy-research', action='store_true', help='显式启用历史研究/记忆兼容功能')
    args = parser.parse_args()
    load_local_env()
    if args.legacy_research:
        os.environ['ENABLE_LEGACY_RESEARCH'] = 'true'
    if args.command == "check":
        check(args.legacy_research)
    elif args.command == "init":
        # IT 默认仅初始化数据库；图在每次诊断时构建，不依赖研究记忆。
        from backend.service.workflow_service import WorkflowService
        service = WorkflowService(str(ROOT / "app" / "config.json"), enable_legacy_research=args.legacy_research)
        try:
            service.start()
            if args.legacy_research:
                service._ensure_initialized()
        finally:
            service.close()
        print("运行存储与工单表初始化成功（未调用模型和搜索 API）")
    elif args.command == "backend":
        port = int(os.environ["PORT"])
        with socket.socket() as probe:
            probe.bind((os.environ["HOST"], port))
        import uvicorn
        uvicorn.run("app_main:app", host=os.environ["HOST"], port=port)
    elif args.command == "frontend":
        node = shutil.which("node")
        vite = ROOT / "front" / "agent_front" / "node_modules" / "vite" / "bin" / "vite.js"
        if not node or not vite.exists():
            parser.error("需要 Node.js 和前端依赖；在 front/agent_front 执行 npm ci")
        raise SystemExit(subprocess.call([node, str(vite), "--host", "127.0.0.1", "--strictPort"], cwd=vite.parents[3]))
    elif args.command == "ingest":
        if not args.legacy_research:
            parser.error('历史研究知识入库需要显式 --legacy-research')
        if not args.input:
            parser.error("ingest 需要指定文件或目录")
        from mult_agents.rag.ingest import main as ingest
        options = [args.input]
        if args.public:
            options.append("--public")
        if args.tenant_id:
            options.extend(["--tenant-id", args.tenant_id])
        if args.user_id:
            options.extend(["--user-id", args.user_id])
        ingest(options)


if __name__ == "__main__":
    main()
