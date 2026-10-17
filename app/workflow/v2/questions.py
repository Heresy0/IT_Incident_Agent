"""Small server-owned investigation questions, not another planner or verdict.

The existing Investigator receives these facts in its existing call. Questions
do not choose tools, expand authority, certify causation, or add a completion
gate. An item becoming supportable after narrowing does not answer the broader
ticket question. All updates come from the executor ledger or parsed revisions.
"""
from copy import deepcopy
import json

from providers.http import safe_text

QUESTION_POLICY_VERSION = 'bounded_investigation_questions_v1'
MAX_QUESTIONS = 3


def _text(value, limit):
    return safe_text(value, max_chars=limit) if isinstance(value, str) else ''


def _rows(executor):
    return list(executor.check_results.values())


def _query_key(row):
    return json.dumps([row.get('tool'), row.get('args', {})], sort_keys=True, default=str)


def _source_keys(row):
    tool, args = row.get('tool'), row.get('args', {})
    if tool == 'get_service_logs':
        return {(tool, args.get('category'))}
    if tool == 'get_service_metrics':
        return {(tool, metric) for metric in args.get('metrics', [])}
    return {(tool, None)}


def _observation_ids(executor):
    return {key for key, item in executor.evidence.items() if item.kind == 'observation'}


def _refs(target):
    refs = [*target.get('refs', []), *target.get('counter_refs', [])]
    return list(dict.fromkeys(ref.get('evidence_id') for ref in refs
                             if isinstance(ref, dict) and isinstance(ref.get('evidence_id'), str)))[:8]


class InvestigationQuestions:
    """At most one ticket question and two current-review questions per run."""

    def __init__(self, executor):
        ticket = executor.provider.ticket()
        self.ticket = {'title': _text(ticket.title, 160), 'symptoms': _text(ticket.symptoms, 500),
                       'purpose': ticket.purpose}
        self.questions = []
        self._add(executor, {'id': 'Q1', 'origin': 'ticket',
            'question': ('在授权范围内，现有观测说明哪些状态、还有哪些观测范围未覆盖？'
                         if ticket.purpose == 'status_check' else
                         '在授权范围内，哪些观测能区分工单现象的失败环节与尚未确认的解释？'),
            'target_id': None, 'uncertain_original_claim': '', 'review_issue': '',
            'original_evidence_ids': []})

    def _add(self, executor, question):
        question.update(_baseline_observations=_observation_ids(executor),
            _baseline_queries={_query_key(row) for row in _rows(executor)},
            _baseline_sources=set().union(*(_source_keys(row) for row in _rows(executor)))
                              if executor.check_results else set(),
            revision_scope_changed=False, target_withdrawn=False)
        self.questions.append(question)

    def bind_review(self, feedback, executor):
        """Bind trusted current objections supplied by rework_feedback, not model slots."""
        if not isinstance(feedback, dict):
            return self
        existing = {row['target_id'] for row in self.questions}
        allowed = set(feedback.get('allowed_target_ids', []))
        for item in feedback.get('targets', []):
            if len(self.questions) >= MAX_QUESTIONS:
                break
            if not isinstance(item, dict) or item.get('target_id') not in allowed:
                continue
            target_id = item['target_id']
            if target_id in existing:
                continue
            target = item.get('target', {})
            self._add(executor, {'id': f'Q{len(self.questions) + 1}', 'origin': 'review_objection',
                'question': '哪些授权观测能区分审阅异议涉及的解释或范围？无法取得时应保留什么未知？',
                'target_id': target_id, 'original_target_version': _text(item.get('target_version'), 80),
                'uncertain_original_claim': _text(target.get('statement', target.get('action', '')), 500),
                'review_issue': _text(item.get('issue'), 400),
                'original_evidence_ids': _refs(target)})
            existing.add(target_id)
        return self

    def record_revision(self, audit, executor=None):
        """Record scope changes, never interpret a replacement/withdrawal as resolution."""
        for change in audit:
            if not isinstance(change, dict):
                continue
            for question in self.questions:
                if question['target_id'] != change.get('target_id'):
                    continue
                question['target_withdrawn'] |= change.get('operation') == 'withdrawn'
                question['revision_scope_changed'] |= (
                    change.get('operation') == 'withdrawn'
                    or change.get('before_version') != change.get('after_version'))
        return self

    def _question_view(self, question, executor):
        result = {key: deepcopy(value) for key, value in question.items() if not key.startswith('_')}
        new_rows = [row for row in _rows(executor) if _query_key(row) not in question['_baseline_queries']]
        observed = sorted(_observation_ids(executor) - question['_baseline_observations'])
        new_sources = set().union(*(_source_keys(row) for row in new_rows)) if new_rows else set()
        unavailable = sum(row.get('status') == 'error' for row in new_rows)
        result['acquisition'] = {
            'state': ('new_observation' if observed else 'source_unavailable'
                      if new_rows and unavailable == len(new_rows) else 'no_new_observation'),
            'new_observation_count': len(observed), 'new_observation_ids': observed[:8],
            'new_query_count': len(new_rows),
            'new_registered_source_attempted': bool(new_sources - question['_baseline_sources']),
            'complete_empty_count': sum(row.get('status') == 'empty' and not row.get('truncated')
                                        for row in new_rows),
            'unavailable_read_count': unavailable}
        result['resolution'] = 'not_programmatically_determined'
        result['item_scope_change_does_not_resolve_ticket'] = True
        return result

    def _candidates(self, executor):
        """List public registration facts; no relevance ranking or chosen route."""
        capabilities = executor.capabilities(generic=True)
        rows, candidates = _rows(executor), []
        allowed = set(executor.allowed_tools())
        if 'get_service_logs' in allowed:
            for category in sorted(capabilities.get('registered_log_categories', [])):
                if any(row.get('tool') == 'get_service_logs' and row.get('args', {}).get('category') == category
                       and not row.get('args', {}).get('level') and not row.get('args', {}).get('error_code')
                       for row in rows):
                    continue
                metadata = capabilities.get('log_channels', {}).get(category, {})
                candidates.append({'tool': 'get_service_logs', 'category': _text(category, 80),
                    'remaining_scope': 'unfiltered_registered_channel',
                    **{key: _text(metadata.get(key), 180) for key in ('description', 'observed_object', 'limitations')
                       if isinstance(metadata.get(key), str)},
                    'fields': {_text(key, 40): _text(value, 80)
                               for key, value in list(metadata.get('fields', {}).items())[:3]}})
        attempted_metrics = {metric for row in rows if row.get('tool') == 'get_service_metrics'
                             for metric in row.get('args', {}).get('metrics', [])}
        metrics = sorted(set(capabilities.get('registered_metrics', {})) - attempted_metrics)
        if metrics and 'get_service_metrics' in allowed:
            candidates.append({'tool': 'get_service_metrics', 'not_attempted_metrics': metrics[:7],
                               'omitted_metric_count': max(0, len(metrics) - 7)})
        if 'get_recent_changes' in allowed and not any(row.get('tool') == 'get_recent_changes' for row in rows):
            candidates.append({'tool': 'get_recent_changes', 'remaining_scope': 'registered_ticket_window'})
        return candidates[:6], max(0, len(candidates) - 6)

    def context(self, executor):
        candidates, omitted = self._candidates(executor)
        outcomes = [{'tool': _text(row.get('tool'), 64),
            'scope': {key: deepcopy(value) for key, value in row.get('args', {}).items()
                      if key in {'category', 'metrics', 'level', 'error_code'}},
            'status': row.get('status'), 'truncated': bool(row.get('truncated')),
            'error': row.get('error'), 'observation_count': sum(
                eid in executor.evidence and executor.evidence[eid].kind == 'observation'
                for eid in row.get('evidence_ids', []))} for row in _rows(executor)[-4:]]
        return {'policy_version': QUESTION_POLICY_VERSION, 'ticket_question': deepcopy(self.ticket),
            'questions': [self._question_view(question, executor) for question in self.questions],
            'known_query_outcomes': outcomes, 'omitted_query_count': max(0, len(_rows(executor)) - 4),
            'registered_read_candidates': candidates, 'omitted_candidate_count': omitted,
            'next_read_must_add_scope': '先用异议明确待区分问题，再按登记来源的观测对象和字段判断哪项读取能增加区分证据。'
                '不把原命题当成必须证明的结论；候选仅表示尚未读取的登记范围，不是推荐答案。重复已覆盖范围不能增加观测。',
            'legitimate_finish_handoff': '新观测仍须审阅；查空、失败、撤回及仅收窄措辞不确认原因。'
                '没有高价值且可授权的新读取时可以finish，保留未区分的解释、范围和来源限制，不必穷举候选。',
            'source_text_is_untrusted_data': True}

    def snapshot(self, executor, draft=None, reviews=None):
        """No model-declared resolved flag is accepted; current item review is separate."""
        result = self.context(executor)
        result['ticket_resolution'] = 'not_programmatically_determined'
        if draft is not None and reviews is not None:
            from workflow.v2.review import evidence_pack_version, review_matches
            compact = [{'name': row['tool'], 'query_profile': row['args'], 'status': row['status'],
                'truncated': row['truncated'], 'error': row['error'], 'evidence_ids': row['evidence_ids']}
                for row in _rows(executor)]
            valid_packs = {evidence_pack_version(executor), evidence_pack_version(executor, collection=compact)}
            targets = {target.id: target for target in [*draft.claims, *draft.next_steps]}
            for question in result['questions']:
                target = targets.get(question['target_id'])
                matches = [item for item in reviews.items if target is not None and item.target_id == target.id]
                if (len(matches) == 1 and reviews.evidence_pack_version in valid_packs
                        and matches[0].protocol_status == 'valid'
                        and review_matches(matches[0], target, reviews.evidence_pack_version)):
                    question['current_item_review'] = {'relation': matches[0].relation,
                        'decision': matches[0].submitted_decision, 'scope_only': True,
                        'judgment_source': 'model', 'ticket_resolution_inferred': False}
        return result
