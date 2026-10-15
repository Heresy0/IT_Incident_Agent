"""Execute an immutable live plan and retain every request, report and failure."""
import json
from uuid import uuid4
from scripts.compare_orchestration_live import (
    ROOT, Principal, RequestLedger, provider_for, capture, summarize,
    digest, render, write_json,
)


def run_batch(config, plan, plan_builder, models_for, limits):
    principal = Principal('synthetic_demo', 'cli_reader')
    directory = ROOT / 'output/orchestration-autonomous' / str(uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / 'execution-plan.json', plan)
    bundle = {'plan': plan, 'paid_attempts': 0, 'runs': []}
    for job in plan['execution_order']:
        if plan_builder(config) != plan:
            raise ValueError('Frozen application or evaluation changed during batch')
        stem = f"{job['case_id']}-{job['orchestration']}-r{job['repetition']}-{job['profile']}"
        print(json.dumps({'starting': job, 'directory': str(directory)}), flush=True)
        ledger_path = directory / f'{stem}-requests.json'
        ledger = RequestLedger(path=ledger_path)
        write_json(ledger_path, {'paid_attempts': 0, 'records': []})
        report = capture(job['case_id'], 'multi', lambda _: models_for(config, job, ledger),
            principal, limits, provider_factory=provider_for(job['case_id']),
            investigation_mode='programmatic', knowledge_mode='programmatic',
            blind_review=False, orchestration=job['orchestration'])
        report.update(model=config.model, execution_mode='live', source_version=plan['source_version'], **job)
        path = directory / f'{stem}.json'
        write_json(path, report)
        try:
            path.with_suffix('.md').write_text(render(report), encoding='utf-8')
            render_error = None
        except Exception as exc:
            render_error = type(exc).__name__
        entry = {**job, **summarize(report, ledger), 'render_error': render_error,
            'report_file': path.relative_to(ROOT).as_posix(), 'report_sha256': digest(path),
            'requests_file': ledger_path.relative_to(ROOT).as_posix(), 'requests_sha256': digest(ledger_path)}
        bundle['runs'].append(entry)
        bundle['paid_attempts'] += ledger.attempts
        write_json(directory / 'summary.json', bundle)
        print(json.dumps({'completed': job, 'status': entry['status'], 'review_status': entry['review_status'],
            'paid_attempts': ledger.attempts, 'token_usage': entry['recorded_token_usage']}, ensure_ascii=False), flush=True)
        if plan_builder(config) != plan:
            raise ValueError('Frozen application or evaluation changed during batch')
    print(json.dumps({'summary_file': str(directory / 'summary.json'), 'paid_attempts': bundle['paid_attempts'],
        'cumulative_paid_attempts': plan['previous_paid_attempts'] + bundle['paid_attempts']}), flush=True)
    return 0
