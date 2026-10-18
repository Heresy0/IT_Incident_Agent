"""Reuse the standard runner unchanged with this phase's immutable freeze."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from scripts import verify_interview_flow as standard


if __name__ == '__main__':
    # Only this standalone evaluation process selects the new freeze. Runtime
    # orchestration, model defaults and the historical runner remain unchanged.
    standard.LOCK = ROOT / 'evals/interview_minimal_repairs/freeze.json'
    raise SystemExit(standard.main())
