from pathlib import Path
import sys


def _bootstrap() -> None:
    root = Path(__file__).resolve().parent
    src = root / "app"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def main() -> None:
    _bootstrap()
    from scripts.investigate_incident import main as run_main
    raise SystemExit(run_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
