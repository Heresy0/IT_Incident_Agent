"""One fixed unseeded case3 follow-up, reusing the standard paid recorder."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'app'),str(ROOT)]
from scripts import verify_interview_flow as standard


if __name__=='__main__':
    standard.LOCK=ROOT/'evals/interview_blocker_followup/freeze.json'
    standard.CASES=('closeout_003',)
    original_plan=standard.execution_plan
    def plan(config):
        value=original_plan(config)
        value.update(maximum_new_paid_attempts=16,
            maximum_cumulative_paid_attempts=value['previous_paid_attempts']+16)
        return value
    standard.execution_plan=plan
    raise SystemExit(standard.main())
