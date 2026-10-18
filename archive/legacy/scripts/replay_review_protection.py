"""Free deterministic replay of saved valid reviews and declared scope perturbations."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'app'), str(ROOT)]
from agents.contracts import DiagnosisDraft, EvidenceNeed, ReviewDecision
from evidence.contracts import Evidence
from evidence.diagnosis import ProtocolError
from evals.common import write_json
from evals.diagnostic_packs.experiment import cases, SnapshotProvider, P
from evals.diagnostic_packs.reporting import label
from runtime.version import implementation_version
from workflow.coordinator import Collaboration
from workflow.evidence_needs import reviewed_needs


class NoCalls:
    json_output = True
    def invoke(self, messages):
        raise AssertionError('离线回放不调用模型')


def replay(case, report, review, revision, mode):
    c = Collaboration({role: NoCalls() for role in ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer')},
                      SnapshotProvider(case['snapshot']), P, review_protection=mode)
    c.draft = DiagnosisDraft.model_validate_json(json.dumps(next(r['draft'] for r in report['drafts'] if r['revision'] == revision)))
    c.evidence_needs = {r['need_id']: EvidenceNeed.model_validate_json(json.dumps(r)) for r in report['evidence_needs']}
    boundary = next(e['seq'] for e in report['events'] if e['type'] == 'review_completed' and e['revision'] == revision)
    events = [e for e in report['events'] if e['seq'] <= boundary]
    reads = [e for e in events if e['type'] == 'tool_result']
    ids = {eid for e in reads for eid in e['evidence_ids']}
    c.evidence = {r['evidence_id']: Evidence.model_validate_json(json.dumps(r)) for r in report['evidence'] if r['evidence_id'] in ids}
    completed_tasks = {e['task_id'] for e in events if e['type'] == 'task_coverage'}
    c.tasks = [deepcopy(r) for r in report['task_results'] if r['task_id'] in completed_tasks]
    for index, e in enumerate(reads):
        ex = c.executors[e['role']]
        ex.evidence.update({eid: c.evidence[eid] for eid in e['evidence_ids']})
        ex.check_results[index] = {'tool': e['name'], 'args': e['query_profile'], 'status': e['status'],
            'truncated': e['truncated'], 'evidence_ids': e['evidence_ids'], 'error': (e.get('error') or {}).get('code')}
    original = ReviewDecision.model_validate_json(json.dumps(review))
    try:
        c.review = c.validate_review(original)
        c.evidence_needs = reviewed_needs(c.evidence_needs, c.review.need_assessments)
        c.finish_review()
        outcome = {'accepted': True, 'status': c.status(), 'reason': c.stop,
                   'verdicts': {a.target_id: a.verdict for a in c.review.assessments},
                   'needs': [a.model_dump(mode='json') for a in c.review.need_assessments],
                   'review': c.review.model_dump(mode='json')}
    except ProtocolError as exc:
        outcome = {'accepted': False, 'status': 'protocol_rejected', 'reason': exc.code}
    assert c.context.counts == {'model_calls': 0, 'tool_calls': 0, 'web_calls': 0}
    outcome['adjustments'] = [e for e in c.events if e['type'] in
        {'review_protection_applied', 'review_scope_limitation', 'review_scope_reclassified'}]
    outcome['model_calls'] = outcome['tool_calls'] = 0
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'output/review-protection-replay')
    args = parser.parse_args(argv)
    summary = json.loads((args.reports/'summary.json').read_text(encoding='utf-8'))
    lookup = {c['case_id']: c for c in cases()}
    frames = []
    for row in summary['runs']:
        path = args.reports/row['report_file']
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['report_sha256']:
            raise ValueError('历史报告哈希不一致')
        report = json.loads(raw)
        for saved in report.get('reviews', []):
            frames.append({'case_id': row['case_id'], 'arm': row['arm'], 'revision': saved['revision'],
                'variant': 'saved_valid_review', 'report_sha256': row['report_sha256'], 'report': report,
                'review': deepcopy(saved['review'])})
            if row['case_id'] == 'connection_wait' and row['arm'] == 'pack' and saved['revision'] == 1:
                # Rejected raw reviews were not retained. Label this as a
                # constructed mutation, never claim to replay their exact text.
                for core in (False, True):
                    review = deepcopy(saved['review'])
                    assessment = next(a for a in review['need_assessments'] if a['need_id'] == ('N2' if core else 'N3'))
                    assessment.update(verdict='uncertain', blocking=None if core else True)
                    assessment['blocker'] = {'cause_level': 'trigger', 'affected_targets': ['H2', 'A1'],
                        'missing_observation': '尚未取得触发历史及已支持人工待办的后续结果',
                        'decision_impact': '解释为何开始发生，当前机制与只读待办保留', 'resolution': 'external_source'}
                    frames.append({'case_id': row['case_id'], 'arm': row['arm'], 'revision': saved['revision'],
                        'variant': 'constructed_core_scope' if core else 'constructed_trigger_scope',
                        'report_sha256': row['report_sha256'], 'report': report, 'review': review})
    directory = args.output/str(uuid4())
    write_json(directory/'inputs.json', [{k: v for k, v in f.items() if k != 'report'} for f in frames])
    rows = []
    for frame in frames:
        row = {k: v for k, v in frame.items() if k not in {'report', 'review'}}
        for mode in ('strict_v1', 'conservative_v1'):
            row[mode] = replay(lookup[frame['case_id']], frame['report'], frame['review'], frame['revision'], mode)
        rows.append(row)
    result = {'source_version': implementation_version(), 'method': 'saved_review_replay_with_declared_mutations_v1',
              'paid_model_calls': 0, 'tool_calls': 0, 'rows': rows,
              'note': '原始失败复核未保存完整内容；仅回放已接受复核和明确标注的范围扰动。不评价模型准确率。'}
    write_json(directory/'summary.json', result)
    variants = {'saved_valid_review': '已保存的有效复核', 'constructed_trigger_scope': '构造的触发范围错挂',
                'constructed_core_scope': '构造的核心问题错挂'}
    def outcome(r):
        return ('复核接受；' + label(r['status'])) if r['accepted'] else '复核被拒'
    lines = ['# 复核保护免费回放', '', '未调用模型或工具；业务语义质量不由本回放评分。', '',
             '| 场景 | 材料 | 调整前 | 调整后 | 调整后保护记录数 |', '| --- | --- | --- | --- | --- |']
    for row in rows:
        lines.append(f"| {label(row['case_id'])} | {variants[row['variant']]}（第{row['revision']}版） | "
            f"{outcome(row['strict_v1'])} | {outcome(row['conservative_v1'])} | {len(row['conservative_v1']['adjustments'])} |")
    lines += ['', result['note']]
    (directory/'summary.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(directory/'summary.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
