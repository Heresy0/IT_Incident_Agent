"""Free standard-flow rehearsal: scripted decisions and synthetic read-only tools."""
import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'app'),str(ROOT)]
from langchain_core.messages import AIMessage
from agents.scripted import scripted_models
from api.auth import Principal
from evidence.render import render
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.coordinator import collaborate


class PlannedSupervisor:
    """A visibly scripted plan, not an autonomous model or a production policy."""
    def __init__(self,model):self.model=model

    def invoke(self,messages):
        response=self.model.invoke(messages)
        data=json.loads(response.content);body=json.loads(messages[1].content)['input']
        window={field:body['ticket']['scope'][field] for field in ('start','end')}
        for task in data.get('tasks',[]):
            if task['role']=='investigation':
                task.update(checks=[{'tool':'get_service_logs','args':{**window,'category':'dependency'}},
                    {'tool':'get_service_metrics','args':{**window,'metrics':['dependency_error_rate']}}],
                    expected_value='取得真实合成观测供复核，不因查询完成自动确认原因')
            else:
                task.update(checks=[{'tool':'search_runbooks','args':{'query':'dependency'}}],
                    expected_value='取得历史背景，不能替代当前观测')
        return AIMessage(content=json.dumps(data,ensure_ascii=False))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'output/interview-consolidation/demo')
    args=parser.parse_args(argv)
    models=scripted_models();models['supervisor']=PlannedSupervisor(models['supervisor'])
    principal=Principal('synthetic_demo','cli_reader')
    report=collaborate(models,FixtureProvider('case_001',principal),principal,
        limits=Limits(model_calls=12,tool_calls=4,seconds=180))
    report.update(execution_mode='scripted_control_only',paid_attempts=0,
        demonstration_meaning='Actual standard graph and synthetic tools; decisions scripted, no diagnosis accuracy claim.')
    folder=args.output/str(uuid4());folder.mkdir(parents=True,exist_ok=False)
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (folder/'report.md').write_text(render(report),encoding='utf-8')
    summary={'output':str(folder),'status':report['status'],'review_status':report['review_status'],
        'paid_attempts':0,'model_calls':report['run_summary']['model_calls'],
        'tool_calls':report['run_summary']['tool_calls'],
        'collector_model_calls':sum(t['model_calls'] for t in report['task_results']),
        'collection_execution':report['collection_execution']}
    (folder/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0 if report['status']=='completed' and summary['collector_model_calls']==0 else 1


if __name__=='__main__':raise SystemExit(main())
