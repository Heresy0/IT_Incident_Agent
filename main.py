from pathlib import Path
import sys


def _bootstrap() -> None:
    root = Path(__file__).resolve().parent
    src = root / "app"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    # 加载 .env 文件（在导入其他模块之前）
    from dotenv import load_dotenv

    env_path = root / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def main() -> None:
    _bootstrap()
    from mult_agents.main import main as run_main

    run_main()


if __name__ == "__main__":
    main()
