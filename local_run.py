"""IT 工单本地启动与数据库检查入口。"""
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


def check() -> None:
    from config.incident import IncidentConfig
    import psycopg

    config = IncidentConfig.from_file(ROOT / 'config.json')
    print(f"模型 Key: {'已配置（未验证有效性）' if config.api_key else '未配置，可运行免费控制模式'}")
    with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
        print(f"PostgreSQL: {'正常' if conn.execute('SELECT 1').fetchone()[0] == 1 else '异常'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="IT Incident Agent 本地启动入口")
    parser.add_argument("command", choices=["backend", "frontend", "check", "init"])
    args = parser.parse_args()
    load_local_env()
    if args.command == "check":
        check()
    elif args.command == "init":
        # IT 默认仅初始化数据库；图在每次诊断时构建，不依赖研究记忆。
        from workflow.service import WorkflowService
        service = WorkflowService(str(ROOT / "config.json"))
        try:
            service.start()
        finally:
            service.close()
        print("运行存储与工单表初始化成功（未调用模型和搜索 API）")
    elif args.command == "backend":
        port = int(os.getenv("PORT", "8003"))
        with socket.socket() as probe:
            probe.bind((os.getenv("HOST", "127.0.0.1"), port))
        import uvicorn
        uvicorn.run("app_main:app", host=os.getenv("HOST", "127.0.0.1"), port=port)
    elif args.command == "frontend":
        node = shutil.which("node")
        vite = ROOT / "front" / "agent_front" / "node_modules" / "vite" / "bin" / "vite.js"
        if not node or not vite.exists():
            parser.error("需要 Node.js 和前端依赖；在 front/agent_front 执行 npm ci")
        raise SystemExit(subprocess.call([node, str(vite), "--host", "127.0.0.1", "--strictPort"], cwd=vite.parents[3]))


if __name__ == "__main__":
    main()
