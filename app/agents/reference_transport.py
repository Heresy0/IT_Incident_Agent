"""Request-local short selectors; canonical evidence and validation stay unchanged."""
from copy import deepcopy
import json
from types import MappingProxyType

from evidence.diagnosis import ProtocolError, diagnosis_reference_options

POLICY = 'diagnosis_short_selectors_v1'
GUIDANCE = """
本次字段引用接口使用短标签R001等，取代需要抄写的REF_长编号。reference_id逐字选择
input.evidence.reference_options或owner_options中的标签，不构造编号、不使用EV_编号。
每个标签绑定一个来源字段；选择与陈述匹配的字段，同一列表内不重复。程序会精确恢复原引用，
标签不是新证据，不提升结论支持程度。escalation_ref只选owner_options中的team字段标签。
查询失败、尚未查询、查空是collection台账限制，没有对应观测时不能写finding并填refs=[]；
将其写入missing_information。有真实观测才能写finding；不能为凑引用选择无关标签。
"""


class ReferenceTransport:
    """Frozen per ask, reused for its bounded correction and revision template."""
    def __init__(self, registry):
        options = sorted(diagnosis_reference_options(registry), key=lambda row: row['reference_id'])
        if len(options) > 999:
            raise ProtocolError('REFERENCE_TRANSPORT_CAPACITY')
        self.rows = tuple({**row, 'alias': f'R{index:03d}'} for index, row in enumerate(options, 1))
        self.to_alias = MappingProxyType({row['reference_id']: row['alias'] for row in self.rows})
        self.to_canonical = MappingProxyType({row['alias']: row['reference_id'] for row in self.rows})

    def encode(self, value):
        """Only identifier fields, including const revision selections; no prose rewrites."""
        if isinstance(value, list):
            return [self.encode(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key == 'reference_id' and isinstance(item, str):
                if item not in self.to_alias:
                    raise ProtocolError('REFERENCE_TRANSPORT_STALE')
                result[key] = self.to_alias[item]
            elif key == 'reference_id' and isinstance(item, dict):
                definition = deepcopy(item)
                definition['pattern'] = r'^R[0-9]{3}$'
                if 'enum' in definition:
                    definition['enum'] = [self.to_alias[rid] for rid in definition['enum']]
                result[key] = definition
            else:
                result[key] = self.encode(item)
        return result

    def decode(self, content):
        """Decode only typed selection positions, never arbitrary nested prose/metadata."""
        data = json.loads(content)
        if not isinstance(data, dict):
            return content  # Original Pydantic contract rejects the wrong shape.
        selections = []
        for index, finding in enumerate(data.get('findings', []) or []):
            if isinstance(finding, dict):
                selections.extend(self._list(finding, 'refs', f'findings.{index}'))
        for index, hypothesis in enumerate(data.get('hypotheses', []) or []):
            selections.extend(self._hypothesis(hypothesis, f'hypotheses.{index}'))
        for index, concern in enumerate(data.get('revision_concerns', []) or []):
            if isinstance(concern, dict):
                selections.extend(self._hypothesis(concern.get('candidate'), f'revision_concerns.{index}.candidate'))
        owner = data.get('escalation_ref')
        if isinstance(owner, dict):
            selections.append((owner, 'escalation_ref.reference_id'))
        errors = []
        for item, path in selections:
            if 'reference_id' not in item:
                continue  # Let the original contract report a missing field.
            token = item['reference_id']
            if not isinstance(token, str) or token not in self.to_canonical:
                errors.append({'path': path, 'type': 'unknown_reference_alias',
                    'instruction': '逐字选择当前来源reference_options中的R标签；不能抄写或猜测长编号。'})
            else:
                item['reference_id'] = self.to_canonical[token]
        if errors:
            raise ProtocolError('UNKNOWN_REFERENCE_ALIAS', errors[:5])
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def _list(item, field, path):
        values = item.get(field, [])
        return [(value, f'{path}.{field}.{index}.reference_id') for index, value in enumerate(values)
                if isinstance(value, dict)] if isinstance(values, list) else []

    def _hypothesis(self, item, path):
        return [*self._list(item, 'support_refs', path), *self._list(item, 'counter_refs', path)] if isinstance(item, dict) else []

    def correction(self, details):
        return ('。本次reference_id必须使用短标签，保持原input和映射不变；当前允许标签：'
            + json.dumps(list(self.to_canonical), ensure_ascii=False)
            + '。按错误path重新选择与陈述匹配的来源字段，不得机械复用先前错误编号。'
              '没有对应观测的查询失败/空结果只放missing_information，不能用空refs伪装事实；'
              '真实finding须有至少一个相关标签，不得为通过选择无关字段。')
