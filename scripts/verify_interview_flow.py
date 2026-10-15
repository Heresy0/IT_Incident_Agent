"""Three fixed, unseeded standard-flow checks; plan-only unless explicitly executed."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'app'),str(ROOT)]
from agents.models import build_roles
from api.auth import Principal
from config.incident import IncidentConfig
from evals.independent.provider import IndependentProvider
from scripts.compare_incident import capture
from runtime.context import Limits, ExecutionError
from runtime.version import implementation_version

CASES=('closeout_001','closeout_002','closeout_003')
LIMITS=Limits(model_calls=16,tool_calls=8,seconds=180,reserve_model_calls=3,reserve_seconds=20,input_chars=48000)
LOCK=ROOT/'evals/interview_consolidation/freeze.json'


def read(path):return json.loads(path.read_text(encoding='utf-8'))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


class ValidationProvider(IndependentProvider):
    name='interview_standard_synthetic_fixture'
    data_root=ROOT/'fixtures/incident_closeout_unseen'


class PaidRecorder:
    """Actual request accounting is separate from workflow reservation counters."""
    json_output=True
    blind_review=False

    def __init__(self,model,role,ledger,path):
        self.model,self.role,self.ledger,self.path=model,role,ledger,path

    def invoke(self,messages):
        if self.ledger['paid_attempts']>=16:raise ExecutionError('BUDGET_EXCEEDED')
        self.ledger['paid_attempts']+=1
        row={'attempt':self.ledger['paid_attempts'],'role':self.role,'token_usage':None,
             'input_chars':sum(len(str(m.content)) for m in messages),
             'messages':[{'role':m.type,'content':m.content} for m in messages],'state':'in_flight'}
        self.ledger['records'].append(row);write(self.path,self.ledger)
        started=time.monotonic()
        try:
            response=self.model.invoke(messages)
            usage=getattr(response,'usage_metadata',None) or getattr(response,'response_metadata',{}).get('token_usage',{})
            if isinstance(usage,dict):
                inp=usage.get('input_tokens',usage.get('prompt_tokens'))
                out=usage.get('output_tokens',usage.get('completion_tokens'))
                if type(inp) is int and type(out) is int:
                    row['token_usage']={'input_tokens':inp,'output_tokens':out}
            row.update(content=response.content,tool_calls=response.tool_calls,
                invalid_tool_calls=getattr(response,'invalid_tool_calls',[]),
                finish_reason=getattr(response,'response_metadata',{}).get('finish_reason'),state='completed')
            return response
        except Exception as exc:
            row.update(error_code=getattr(exc,'code',type(exc).__name__),state='error')
            raise
        finally:
            row['duration_ms']=round((time.monotonic()-started)*1000);write(self.path,self.ledger)


def execution_plan(config):
    lock=read(LOCK)
    if implementation_version()!=lock['source_version'] or config.model!=lock['model']:
        raise ValueError('Application/model changed after freeze')
    for relative,expected in lock['files'].items():
        path=(ROOT/relative).resolve()
        if not path.is_relative_to(ROOT.resolve()) or sha(path)!=expected:
            raise ValueError('Frozen data/driver/history changed')
    previous=0
    for row in lock['previous_batches']:
        path=ROOT/row['summary_file']
        if sha(path)!=row['summary_sha256']:raise ValueError('Old failed batch changed')
        summary=read(path)
        if len(summary['runs'])!=row['runs']:raise ValueError('Old batch incomplete')
        previous+=summary['paid_attempts']
    if previous+16*len(CASES)>336:raise ValueError('Cumulative paid cap exceeded')
    return {'suite':'interview_standard','application_commit':lock['application_commit'],
        'baseline':lock['baseline'],'source_version':implementation_version(),'freeze_sha256':sha(LOCK),
        'files':lock['files'],'cases':list(CASES),'model':config.model,'limits':asdict(LIMITS),
        'model_settings':{'output_tokens':2200,'timeout':30,'temperature':0,'sdk_retries':0},
        'effective_settings_hash':hashlib.sha256(json.dumps({'base_url':config.base_url,'extra_body':config.extra_body},sort_keys=True).encode()).hexdigest(),
        'previous_paid_attempts':previous,'maximum_new_paid_attempts':48,'cumulative_paid_cap':336,
        'maximum_cumulative_paid_attempts':previous+48,'seeded_turns':0,'replayed_turns':0,
        'flow':'standard_programmatic','independent_blind_holdout':False,
        'purpose':'Small seen-case regression, not proof of causal correctness or superiority.',
        'rubric_limitation':'Deployment completion does not establish deployment start/effect time.'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live',action='store_true');args=parser.parse_args(argv)
    config=IncidentConfig.from_file();plan=execution_plan(config)
    principal=Principal('synthetic_demo','cli_reader')
    for case in CASES:
        provider=ValidationProvider(case,principal);provider.authorize(principal,provider.ticket().scope)
    if not args.execute_live:
        print(json.dumps({'plan_only':True,**plan},ensure_ascii=False,indent=2));return 0
    if not config.api_key or config.api_key=='test-only':parser.error('Credential required; no requests sent')
    folder=ROOT/'output/interview-consolidation'/str(uuid4());folder.mkdir(exist_ok=False)
    write(folder/'execution-plan.json',plan)
    bundle={'plan':plan,'runs':[],'paid_attempts':0}
    for case in CASES:
        if execution_plan(config)!=plan:raise ValueError('Frozen application/evaluation changed')
        print(json.dumps({'starting':case,'directory':str(folder)}),flush=True)
        ledger={'paid_attempts':0,'records':[]};ledger_path=folder/f'{case}-requests.json';write(ledger_path,ledger)
        def models(_):
            roles=build_roles(config.api_key,config.model,base_url=config.base_url,extra_body=config.extra_body)
            return {role:PaidRecorder(model,role,ledger,ledger_path) for role,model in roles.items()}
        report=capture(case,'multi',models,principal,LIMITS,provider_factory=ValidationProvider,
            investigation_mode='programmatic',knowledge_mode='programmatic',blind_review=False)
        report.update(case_id=case,model=config.model,source_version=plan['source_version'],execution_mode='live')
        path=folder/f'{case}.json';write(path,report)
        row={'case_id':case,'status':report['status'],'review_status':report['review_status'],
             'paid_attempts':ledger['paid_attempts'],'report_file':path.relative_to(ROOT).as_posix(),
             'report_sha256':sha(path),'requests_file':ledger_path.relative_to(ROOT).as_posix(),
             'requests_sha256':sha(ledger_path)}
        bundle['runs'].append(row);bundle['paid_attempts']+=ledger['paid_attempts'];write(folder/'summary.json',bundle)
        print(json.dumps({'completed':row},ensure_ascii=False),flush=True)
        if execution_plan(config)!=plan:raise ValueError('Frozen application/evaluation changed')
    print(json.dumps({'summary_file':str(folder/'summary.json'),'paid_attempts':bundle['paid_attempts'],
        'cumulative_paid_attempts':plan['previous_paid_attempts']+bundle['paid_attempts']}),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
