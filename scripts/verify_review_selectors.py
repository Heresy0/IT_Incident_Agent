"""Six autonomous regressions and three explicitly seeded revision probes."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from langchain_core.messages import AIMessage
from scripts.compare_reviewer_recovery import UNSEEN_CASES, CAP_PER_RUN, digest, models_for as live_models, IncidentConfig, Principal, provider_for, implementation_version
from runtime.context import Limits
from evals.live_batch import run_batch

LIMITS = Limits(model_calls=16, tool_calls=8, seconds=600,
                reserve_model_calls=3, reserve_seconds=90, input_chars=48000)
LOCK = ROOT / 'evals/review_selectors/freeze.json'


class SeedDiagnosis:
    """One free bad draft, not model output/replay; all subsequent turns are live."""
    json_output = True

    def __init__(self, model, ledger):
        self.model, self.ledger, self.calls = model, ledger, 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls != 1:
            return self.model.invoke(messages)
        body = json.loads(messages[1].content)['input']
        findings, refs = [], []
        for row in body['evidence']:
            if row['kind'] != 'observation':
                continue
            option = next((o for o in row['reference_options'] if o['field_path'] in {'value','message','summary'}), None)
            if not option:
                continue
            ref = {'reference_id': option['reference_id']}
            payload = row['payload']; field = option['field_path']
            findings.append({'statement': f"当前来源{payload.get('metric', payload.get('category', '记录'))}.{field}={payload[field]}", 'refs':[ref]})
            refs.append(ref)
        if not refs:
            raise ValueError('Revision probe has no observed reference; do not fabricate a seed')
        data = {'findings':findings[:8], 'hypotheses':[{
            'cause':'已有观测已证明故障由本次发布引入的代码缺陷直接造成。',
            'level':'mechanism','support_refs':refs[:8],
            'evidence_explanation':'发布记录与工单故障窗口重合，因此可以确定发布代码缺陷为故障原因。'}],
            'recommended_actions':[{'action':'人工核查本服务故障窗口内的发布与实际失败记录',
                'condition':'已取得当前技术观测，业务影响或触发关系仍需核实',
                'expected_result':'取得可关联的实际记录以区分候选机制；无记录保留未知',
                'risk':'只读核查','requires_approval':False,'kind':'manual_check'}],
            'missing_information':['发布开始、生效时间及与失败记录的因果关联尚未确认。']}
        seed = {'role':'diagnosis','free_seed':True,'paid_attempt':False,
                'content':json.dumps(data,ensure_ascii=False),
                'input_chars':sum(len(str(m.content)) for m in messages)}
        path = self.ledger.path.with_name(self.ledger.path.stem.replace('-requests','') + '-seed.json')
        path.write_text(json.dumps(seed,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return AIMessage(content=seed['content'])


def models_for(config, job, ledger):
    models = live_models(config, job, ledger)
    if job['profile'] == 'seeded_revision':
        models['diagnosis'] = SeedDiagnosis(models['diagnosis'], ledger)
    return models


def execution_plan(config):
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    if implementation_version() != lock['source_version'] or config.model != lock['model']:
        raise ValueError('Application/model changed after freeze')
    for p, expected in {**lock['files'], **lock['previous_archive_hashes']}.items():
        path = (ROOT / p).resolve()
        if not path.is_relative_to(ROOT.resolve()) or digest(path) != expected:
            raise ValueError('Frozen data/driver/archive changed')
    previous_paid = 0
    for row in lock['previous_batches']:
        path = ROOT / row['summary_file']
        if digest(path) != row['summary_sha256']:
            raise ValueError('Previous failed batch changed')
        summary = json.loads(path.read_text(encoding='utf-8'))
        if len(summary['runs']) != row['runs']:
            raise ValueError('Previous batch incomplete')
        previous_paid += summary['paid_attempts']
    jobs = [{'case_id':case,'orchestration':'review_driven','repetition':rep,
             'profile':'autonomous_selectors','reviewer_output_tokens':4800,'seeded_turns':0}
            for rep in (1,2) for case in UNSEEN_CASES]
    jobs += [{'case_id':case,'orchestration':'review_driven','repetition':1,
              'profile':'seeded_revision','reviewer_output_tokens':4800,'seeded_turns':1}
             for case in UNSEEN_CASES]
    maximum = CAP_PER_RUN * len(jobs)
    if previous_paid + maximum > 336:
        raise ValueError('Cumulative paid cap exceeded')
    return {'suite':'review_selectors','source_version':implementation_version(),
        'application_commit':lock['application_commit'],'dataset_lock':digest(LOCK),
        'frozen_file_hashes':lock['files'],'previous_archive_hashes':lock['previous_archive_hashes'],
        'previous_batches':lock['previous_batches'],'previous_paid_attempts':previous_paid,
        'cumulative_paid_cap':336,'maximum_paid_attempts':maximum,
        'maximum_cumulative_paid_attempts':previous_paid+maximum,
        'model':config.model,'effective_settings_hash':hashlib.sha256(json.dumps(
            {'base_url':config.base_url,'extra_body':config.extra_body},sort_keys=True).encode()).hexdigest(),
        'model_settings':{'reviewer_output_tokens':4800,'reviewer_timeout':60,
            'other_output_tokens':2200,'other_timeout':30,'temperature':0,'max_retries':0},
        'limits':asdict(LIMITS),'paid_cap_per_run':CAP_PER_RUN,'execution_order':jobs,
        'replayed_turns':0,'synthetic_observations':True,'independent_blind_holdout':False,
        'seed_policy':'Six autonomous runs have no seeds. Three probes inject one bad draft, '
            'not review feedback. Seed consumes workflow call budget but is not a paid request. '
            'Reviewer, revision Diagnosis and final Reviewer remain live; probes are not autonomous accuracy evidence.',
        'rubric_defect_preserved':lock['rubric_defect_preserved']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live',action='store_true')
    args=parser.parse_args(argv)
    config=IncidentConfig.from_file(); plan=execution_plan(config)
    principal=Principal('synthetic_demo','cli_reader')
    for case in UNSEEN_CASES:
        provider=provider_for(case)(case,principal)
        provider.authorize(principal,provider.ticket().scope)
    if not args.execute_live:
        print(json.dumps({'plan_only':True,**plan},ensure_ascii=False,indent=2)); return 0
    if not config.api_key or config.api_key=='test-only':
        parser.error('Configured credential required; no requests sent')
    return run_batch(config,plan,execution_plan,models_for,LIMITS)


if __name__=='__main__':
    raise SystemExit(main())
