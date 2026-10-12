"""Free paired controls; optionally replay hash-verified saved model reviews."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'app'), str(ROOT)]
from evals.common import write_json
from evals.refutations.controls import run_controls
from evals.diagnostic_packs.experiment import cases
from runtime.version import implementation_version
from scripts.replay_review_protection import replay


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'output/bounded-refutation-offline')
    args = parser.parse_args(argv)
    result = {**run_controls(), 'source_version': implementation_version(), 'saved_reviews': []}
    if args.reports:
        summary = json.loads((args.reports/'summary.json').read_text(encoding='utf-8'))
        lookup = {c['case_id']: c for c in cases()}
        for row in summary['runs']:
            raw = (args.reports/row['report_file']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != row['report_sha256']:
                raise ValueError('历史报告哈希不一致')
            report = json.loads(raw)
            for saved in report.get('reviews', []):
                result['saved_reviews'].append({'case_id': row['case_id'], 'arm': row['arm'],
                    'revision': saved['revision'], 'report_sha256': row['report_sha256'],
                    'baseline': replay(lookup[row['case_id']], report, saved['review'], saved['revision'], 'conservative_v1'),
                    'bounded': replay(lookup[row['case_id']], report, saved['review'], saved['revision'], 'bounded_refutation_v1')})
        result['limitations'].append('旧复核没有反证类别，新模式将其保留为未知；此回放不补造分类，也不证明语义误判已解决。')
    directory = args.output/str(uuid4())
    write_json(directory/'summary.json', result)
    verdicts = {'not_supported': '保留否定', 'uncertain': '未确认'}
    lines = ['# 有限反证优化离线验证', '', '未调用模型或工具。反证类型由控制输入指定，结果只验证程序处理。', '',
        '| 控制场景 | 原保护 | 有限保护 | 符合预期 |', '| --- | --- | --- | --- |']
    for row in result['rows']:
        lines.append(f"| {row['title']} | {verdicts[row['baseline']['verdict']]} | {verdicts[row['bounded']['verdict']]} | 是 |")
    lines += ['', f"已回放 {len(result['saved_reviews'])} 个历史有效复核。", '']
    lines += ['- '+text for text in result['limitations']]
    (directory/'summary.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(directory/'summary.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
