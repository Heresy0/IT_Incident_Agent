"""Real workflow + fixed public synthetic retrieval. Paid calls require --live-model."""
import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys
from uuid import uuid4
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
from evals.scoring import SCORER_VERSION, grade, review_template


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def implementation_version():
    code = hashlib.sha256()
    for path in sorted((ROOT/'app/mult_agents').rglob('*.py')):
        code.update(str(path.relative_to(ROOT)).replace('\\', '/').encode())
        code.update(path.read_bytes())
    return code.hexdigest()[:16]


def fixtures():
    text = (ROOT / "evals/fixtures/solutions.md").read_text(encoding="utf-8")
    docs = {}
    for match in re.finditer(r"^## (.+)\n\n([^#]+)", text, re.M):
        label = match[1]
        key = label[0] if label[0] in "ABCD" else "TEAM"
        docs[key] = {"title": label, "doc_id": f"evals/fixtures/solutions.md#{key}", "snippet": match[2].strip(),
                     "source_type": "local", "retrieval_level": "document_chunk", "is_simulated": True}
    cases = json.loads((ROOT / "evals/cases.json").read_text(encoding="utf-8"))
    if len(docs) != 5 or not cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Invalid corpus or duplicate case IDs")
    for case in cases:
        if case['split'] not in {'development', 'holdout'} or case['expected_behavior'] not in {'answer', 'qualified_unknown', 'abstain', 'personalize'}:
            raise ValueError('Invalid case classification: ' + case['id'])
        corpus = " ".join(docs[d]["snippet"] for d in case["docs"])
        if any(quote not in corpus for quote in case["expected_quotes"]):
            raise ValueError('Expected quote absent: ' + case['id'])
        requirements = case['requirements']
        if not requirements or len({r['id'] for r in requirements}) != len(requirements):
            raise ValueError('Invalid gold requirements: ' + case['id'])
        for requirement in requirements:
            if not set(requirement['support_docs']).issubset(case['docs']):
                raise ValueError('Gold refers to unavailable docs: ' + case['id'])
            support = ' '.join(docs[d]['snippet'] for d in requirement['support_docs'])
            if any(quote not in support for quote in requirement['support_quotes']):
                raise ValueError('Gold support absent: ' + case['id'])
    return cases, docs


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def capture(case, docs, agents, graph, attempt, metadata):
    from mult_agents.harness.runtime import RunContext, activate, current, register, unregister
    from mult_agents.state import create_initial_state
    from mult_agents.harness.coverage import final_coverage
    context = RunContext()
    register(context)
    def records(query, limit=4):
        ctx = current()
        if ctx:
            ctx.reserve('local')
        with ctx.span('tool', 'fixed_knowledge') if ctx else nullcontext():
            return [docs[d] for d in case['docs']][:limit]
    def web(query, count=4, **kwargs):
        ctx = current()
        if ctx:
            ctx.reserve('web')
        return []
    result = {}
    try:
        with activate(context), patch('mult_agents.nodes.search_knowledge_base_records', side_effect=records), patch('mult_agents.nodes.bocha_web_search_records', side_effect=web):
            state = create_initial_state(case['query'], 1, 'eval', 'eval', run_id=context.run_id,
                                         memory_context=case.get('memory_context', ''))
            result = graph.invoke(state, {'configurable': {'run_id':context.run_id, 'thread_id':context.run_id}})
    except Exception as exc:
        result = {'status':'failed', 'final':'Evaluation execution failed; inspect recorded error types.',
                  'evaluation_error_type':type(exc).__name__}
    finally:
        unregister(context.run_id)
    row = {'run_id':context.run_id, 'attempt':attempt, 'case':case, **metadata,
           'status':result.get('status', 'failed'), 'route':result.get('intent', 'unknown'),
           'report':result.get('final', ''), 'candidate_findings':result.get('findings', []),
           'verified_findings':result.get('verified_findings', []), 'evidence':result.get('evidence_pool', []),
           'summary':context.summary(), 'question_coverage':final_coverage(result),
           'coverage_stage':result.get('coverage_stage', 'not_assessed'),
           'missing_gaps':result.get('missing_gaps', []), 'verification_rejections':result.get('verification_rejections', []),
           'evaluation_error_type':result.get('evaluation_error_type')}
    row['workflow_termination_reason'] = result.get('termination_reason', '')
    row['grading'] = grade(row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-model', action='store_true')
    parser.add_argument('--check-fixtures', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--case', action='append', help='Repeat for multiple IDs')
    parser.add_argument('--split', choices=['all','development','holdout'], default='all')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--output', type=Path, help='New JSON file; existing output is never overwritten')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    cases, docs = fixtures()
    if args.check_fixtures:
        print(f'{len(cases)} quality cases ({sum(c["split"]=="holdout" for c in cases)} holdout) and {len(docs)} fixture sections validated')
        return
    if not args.live_model:
        parser.error('Choose --check-fixtures or --live-model; live-model consumes model API credits')
    if not 1 <= args.repeats <= 5 or (args.limit is not None and args.limit < 1):
        parser.error('repeats must be 1..5 and limit must be positive')
    if args.case and set(args.case) - {c['id'] for c in cases}:
        parser.error('Unknown case ID')
    cases = [c for c in cases if (args.split=='all' or c['split']==args.split) and (not args.case or c['id'] in args.case)]
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        parser.error('No matching cases')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    evaluation_id = str(uuid4())
    output = args.output or ROOT / 'output/evaluations' / f'{stamp}-{evaluation_id[:8]}' / 'evaluation.json'
    if output.exists():
        parser.error('Output exists; choose a new file to preserve historical results')
    from local_run import load_local_env
    load_local_env()
    from langgraph.checkpoint.memory import InMemorySaver
    from mult_agents.config import AppConfig
    from mult_agents.main import build_agents
    from mult_agents.graph import build_app
    from mult_agents.prompts import PROMPTS
    from mult_agents.nodes import SUPPORT_PROMPT
    from mult_agents.harness.validation import SCHEMAS
    from mult_agents.harness.version import WORKFLOW_VERSION
    config = AppConfig.from_file()
    if not config.api_key:
        parser.error('Model API key missing')
    with patch('mult_agents.main.init_rag_system'):
        agents = build_agents(config.model, config.api_key, config)
    graph = build_app(agents, InMemorySaver())
    contracts = {name:s.model_json_schema() for name,s in SCHEMAS.items() if s}
    roles = {}
    for name, agent in vars(agents).items():
        raw = getattr(agent, '_harness_llm', None)
        if raw:
            roles[name] = {'temperature':getattr(raw,'temperature',None), 'top_p':getattr(raw,'top_p',None),
                           **{k:v for k,v in raw.model_kwargs.items() if k in {'max_tokens','request_timeout'}}}
    metadata = {'model':config.model, 'model_config':roles, 'workflow_version':WORKFLOW_VERSION,
                'implementation_version':implementation_version(),
                'prompt_version':hashlib.sha256(json.dumps([PROMPTS,SUPPORT_PROMPT,contracts],sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16],
                'fixture_version':digest(ROOT/'evals/fixtures/solutions.md'), 'dataset_version':digest(ROOT/'evals/cases.json')}
    artifact = {'schema_version':2, 'suite':'fixed_workflow', 'evaluation_id':evaluation_id,
                'created_at':datetime.now(timezone.utc).isoformat(), 'scorer_version':SCORER_VERSION,
                'selected_cases':[c['id'] for c in cases], 'repeats':args.repeats, **metadata,
                'dependency_versions':{p:importlib.metadata.version(p) for p in ['langchain','langgraph','pydantic']},
                'expected_run_count':len(cases)*args.repeats, 'complete':False, 'results':[],
                'scope':'Public synthetic corpus; real model; no live Bocha/Milvus/Embedding; memory cases inject fixed snapshots'}
    write_json(output, artifact)
    print(json.dumps({'evaluation_file':str(output), 'expected_runs':artifact['expected_run_count'], 'model':config.model},ensure_ascii=False),flush=True)
    failures = 0
    for attempt in range(1, args.repeats+1):
        for case in cases:
            row = capture(case, docs, agents, graph, attempt, metadata)
            artifact['results'].append(row)
            write_json(output, artifact)
            print(json.dumps({'case':case['id'], 'attempt':attempt, 'status':row['status'],
                              'model_calls':row['summary']['model_calls'], 'automated_pass':row['grading']['automated_pass']},ensure_ascii=False),flush=True)
            provider_error = any(e['code'] in {'MODEL_ERROR','AUTH_ERROR','QUOTA_EXCEEDED'} for e in row['summary']['errors'])
            failures = failures+1 if provider_error else 0
            if failures >= 2:
                artifact['stop_reason']='Two consecutive provider failures; stopped further paid calls'
                break
        if failures >= 2:
            break
    artifact['complete'] = len(artifact['results']) == artifact['expected_run_count']
    write_json(output, artifact)
    write_json(output.with_name(output.stem+'.reviews.json'), review_template(artifact))
    from evals.report import generate
    generate(artifact, output=output.with_name(output.stem+'.report.md'))
    if not artifact['complete']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
