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
    args = sys.argv[1:]
    if '--workflow' not in args and not any(a.startswith('--workflow=') for a in args):
        args = ['--workflow', 'collaboration', *args]
    raise SystemExit(run_main(args))


if __name__ == "__main__":
    main()
