"""Exact request-local EV/read selectors; no prose or verdict repair."""
import json
from types import MappingProxyType
from evidence.diagnosis import ProtocolError


class ReviewerReferences:
    def __init__(self, evidence, reads):
        self.evidence = MappingProxyType({f'E{i:03d}': key for i, key in enumerate(sorted(evidence), 1)})
        self.reads = MappingProxyType({f'Q{i:03d}': key for i, key in enumerate(sorted(reads), 1)})
        self.reverse = {value: key for mapping in (self.evidence, self.reads) for key, value in mapping.items()}

    def encode(self, value):
        if isinstance(value, list):
            return [self.encode(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            out_key = self.reverse.get(key, key)
            if key in {'evidence_id', 'read_id'} and isinstance(item, str):
                result[out_key] = self.reverse.get(item, item)
            elif key in {'evidence_ids', 'anchor_evidence_ids', 'enum'} and isinstance(item, list):
                result[out_key] = [self.reverse.get(v, v) if isinstance(v, str) else self.encode(v) for v in item]
            elif key == 'read_id' and isinstance(item, dict):
                result[out_key] = {k: self.encode(v) for k, v in item.items() if k != 'pattern'}
                result[out_key]['enum'] = list(self.reads)
            else:
                result[out_key] = self.encode(item)
        return result

    def decode(self, content):
        data = json.loads(content)
        if not isinstance(data, dict):
            return content
        errors = []

        def select(row, field, mapping, path, many=False):
            if not isinstance(row, dict) or field not in row:
                return
            values = row[field] if many else [row[field]]
            if not isinstance(values, list):
                return  # Original DTO reports the shape error.
            restored = []
            for index, token in enumerate(values):
                if not isinstance(token, str) or token not in mapping:
                    errors.append({'path': f'{path}.{field}' + (f'.{index}' if many else ''),
                        'type': 'unknown_evidence_selector' if mapping is self.evidence else 'unknown_read_selector',
                        'allowed': list(mapping),
                        'instruction': 'E仅表示观测证据；Q仅表示查询台账，不能互换。没有观测可写evidence_ids=[]，失败查询只放ledger_assertions/related_reads。'})
                else:
                    restored.append(mapping[token])
            if len(restored) == len(values):
                row[field] = restored if many else restored[0]

        def rows(field):
            value = data.get(field, [])
            return enumerate(value) if isinstance(value, list) else ()

        for field in ('assessments', 'need_assessments', 'scope_assessments'):
            for i, row in rows(field):
                select(row, 'evidence_ids', self.evidence, f'{field}.{i}', True)
        select(data.get('request_evidence'), 'evidence_ids', self.evidence, 'request_evidence', True)
        for i, row in rows('counterpoints'):
            if isinstance(row, dict) and isinstance(row.get('fields'), list):
                for j, anchor in enumerate(row['fields']):
                    select(anchor, 'evidence_id', self.evidence, f'counterpoints.{i}.fields.{j}')
        for i, row in rows('ledger_assertions'):
            select(row, 'read_id', self.reads, f'ledger_assertions.{i}')
        for i, row in rows('manual_tasks'):
            select(row, 'anchor_evidence_ids', self.evidence, f'manual_tasks.{i}', True)
            if isinstance(row, dict) and isinstance(row.get('related_reads'), list):
                for j, assertion in enumerate(row['related_reads']):
                    select(assertion, 'read_id', self.reads, f'manual_tasks.{i}.related_reads.{j}')
        if errors:
            raise ProtocolError('UNKNOWN_REVIEW_SELECTOR', errors[:5])
        return json.dumps(data, ensure_ascii=False)

    def mappings(self):
        return {'evidence': dict(self.evidence), 'reads': dict(self.reads)}


GUIDANCE = '''
当前证据编号使用E001等短标签，查询台账使用Q001等短标签；逐字从当前输入目录选择，
两种类型不能互换。evidence_ids/anchor_evidence_ids/fields.evidence_id只允许E；
ledger_assertions/related_reads.read_id只允许Q。引用恢复不代表语义支持。
反证claim_quote必须逐字截取当前H.cause，保留括号、编号及标点，不能改写或拼接；
弱化局部陈述不等于排除整条假设。时间前后或未记载不能单独证明无因果联系。
若既有证据仅允许删除过度确定的因果措辞，应请求reasoning修稿，使其降级为待验证候选；
修稿不补充新证据。需要新观测的缺口使用evidence，不同时把同一缺口标成revision。
'''
