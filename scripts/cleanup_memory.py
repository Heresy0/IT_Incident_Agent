"""Local administrator maintenance; expired memories are already excluded on reads."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_run import load_local_env


def main():
    load_local_env()
    from mult_agents.config import AppConfig
    from mult_agents.memory.scoped import ScopedMemoryManager
    memory = ScopedMemoryManager(postgres_dsn=AppConfig.from_file().postgres_dsn, enable_milvus=False)
    try:
        print(memory.cleanup_expired())
    finally:
        memory.close()


if __name__ == '__main__':
    main()
