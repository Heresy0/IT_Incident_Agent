"""Pure publication of reviewed claims and source observations from explicit inputs."""
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Mapping

from agents.contracts import DiagnosisDraft, ReviewDecision
from agents.investigation import validate_output
from evidence.contracts import Evidence, EvidenceRef, Finding, InvestigationOutput
from evidence.model_view import substantive
from evidence.observations import gap_message, source_statement
from workflow.review_outcome import action_projection, readiness_gaps


@dataclass(frozen=True)
class PublicationInput:
    draft: DiagnosisDraft | None
    review: ReviewDecision | None
    purpose: str
    status: str
    stop: str
    missing_information: list[str]
    team: str | None
    coverage_gaps: list[str]


@dataclass(frozen=True)
class Publication:
    findings: list[dict]
    hypotheses: list[dict]
    actions: list[dict]
    pending_actions: list[dict]
    used_evidence_ids: list[str]
    missing_information: list[str]
    escalation_team: str | None
    business_result: str
    report_status: str


@dataclass(frozen=True)
class ReportSnapshot:
    """Serialized audit metadata plus the publication; contains no live coordinator."""
    metadata: Mapping[str, Any]
    publication: Publication


def publish_diagnosis(view: PublicationInput) -> Publication:
    gaps, team = list(view.missing_information), view.team
    approved = {a.target_id for a in view.review.assessments if a.verdict == "supported"} if view.review else set()
    findings, hypotheses, actions, pending_actions, used = [], [], [], [], set()
    if view.draft:
        for n, finding in enumerate(view.draft.findings, 1):
            if f"F{n}" in approved:
                findings.append(finding.model_dump(mode="json"))
                used.update(r.evidence_id for r in finding.refs)
        verdicts = {a.target_id: a.verdict for a in view.review.assessments} if view.review else {}
        for h in view.draft.hypotheses:
            verdict = verdicts.get(h.hypothesis_id)
            status = {"supported": "supported", "not_supported": "refuted", "uncertain": "unresolved"}.get(verdict, "tentative")
            hypotheses.append(h.model_copy(update={"status": status}).model_dump(mode="json"))
            if status == "supported":
                used.update(r.evidence_id for r in [*h.support_refs, *h.counter_refs])
        actions, pending_actions = action_projection(view.draft, view.review)
        gaps.extend(view.draft.missing_information)
        team = view.draft.escalation_team or team
    if view.review:
        gaps.extend(view.review.missing_information)
        for assessment in view.review.need_assessments:
            if assessment.verdict == 'supported':
                used.update(assessment.evidence_ids)
        for a in view.review.assessments:
            if a.verdict == "supported":
                used.update(a.evidence_ids)
            else:
                gaps.append(f"{a.target_id}: {a.reason}")
    if view.status != "completed":
        if view.draft and view.review:
            gaps.extend(readiness_gaps(view.draft, view.review, view.purpose))
        if view.coverage_gaps:
            gaps.append(gap_message(view.coverage_gaps))
        gaps.append(f"协作尚有缺口：{view.stop}；需要补充信息或人工升级。")
    completed = view.status == 'completed'
    business_result = ('status_checked' if view.purpose == 'status_check' else 'diagnosis_available') if completed else 'escalation_recommended' if team else 'needs_information'
    return Publication(findings, hypotheses, actions, pending_actions, sorted(used), gaps,
        team, business_result,
        ('reviewed_with_limitations' if gaps else 'reviewed') if completed else 'incomplete')


def build_report(snapshot: ReportSnapshot) -> dict:
    """Build an independent response; publication never writes workflow state."""
    report = deepcopy(dict(snapshot.metadata))
    published = snapshot.publication
    report.update(business_result=published.business_result,
        pending_actions=deepcopy(published.pending_actions),
        used_evidence_ids=list(published.used_evidence_ids),
        output={'findings': deepcopy(published.findings), 'hypotheses': deepcopy(published.hypotheses),
                'recommended_actions': deepcopy(published.actions),
                'missing_information': list(dict.fromkeys(published.missing_information)),
                'escalation_team': published.escalation_team})
    report['diagnosis_readiness']['report_status'] = published.report_status
    return report


def unreviewed_observations(approved_findings, *, completed: bool, investigation_mode: str,
                           evidence: Mapping[str, Evidence], references: Mapping[str, EvidenceRef],
                           tasks: list[dict]) -> list[dict]:
    """Expose validated source facts separately, without claiming semantic review."""
    if completed:
        return []
    seen = {(r['evidence_id'], r['field_path']) for f in approved_findings for r in f['refs']}
    result = []
    if investigation_mode == 'programmatic':
        # Program reads intentionally have no model-authored findings. Use
        # the same server-owned field registry, never a causal summary.
        for item in evidence.values():
            if item.kind != 'observation':
                continue
            refs = [references[o.reference_id].model_copy(deep=True)
                    for o in item.reference_options
                    if substantive(o.field_path) and o.reference_id in references
                    and (item.evidence_id, o.field_path) not in seen]
            for offset in range(0, len(refs), 8):
                selected = refs[offset:offset + 8]
                finding = Finding(statement=source_statement(selected, evidence), refs=selected)
                validate_output(InvestigationOutput(findings=[finding]), evidence)
                result.append(finding.model_dump(mode='json'))
                seen.update((ref.evidence_id, ref.field_path) for ref in selected)
        return result
    for task in tasks:
        if task['role'] != 'investigation':
            continue
        for data in task['output']['findings']:
            finding = Finding.model_validate_json(json.dumps(data))
            validate_output(InvestigationOutput(findings=[finding]), evidence)
            keys = {(r.evidence_id, r.field_path) for r in finding.refs}
            if keys - seen:
                result.append(finding.model_dump(mode='json'))
                seen.update(keys)
            if len(result) >= 8:
                return result
    return result


def status_check_coverage(capabilities: dict, evidence: Mapping[str, Evidence], collection: list[dict]) -> dict:
    metrics = sorted({e.payload['metric'] for e in evidence.values()
                      if e.kind == 'observation' and 'metric' in e.payload})
    log_categories = sorted({e['query_profile']['category'] for e in collection
        if e['name'] == 'get_service_logs' and e['status'] in {'ok', 'empty'} and 'category' in e['query_profile']})
    notes = []
    missing_metrics = sorted(set(capabilities.get('registered_metrics', {})) - set(metrics))
    missing_logs = sorted(set(capabilities.get('registered_log_categories', [])) - set(log_categories))
    if missing_metrics:
        notes.append('尚未取得这些登记指标的样本：' + '、'.join(missing_metrics) + '。')
    if missing_logs:
        notes.append('尚未核查这些登记日志类别：' + '、'.join(missing_logs) + '。')
    for event in collection:
        if event['status'] == 'error':
            notes.append(event['name'] + ' 未完成，原因：' + (event.get('error') or {}).get('code', 'UNKNOWN') + '。')
        if event['truncated']:
            notes.append(event['name'] + ' 返回截断样本，不能代表完整查询窗口。')
        if event['status'] == 'empty':
            notes.append(event['name'] + ' 在所选窗口及过滤条件内无匹配记录，不能据此确认正常。')
    intervals = sorted({str(e.payload.get('granularity')) for e in evidence.values() if 'metric' in e.payload})
    if intervals:
        notes.append('指标实际采样间隔：' + '、'.join(intervals) + '；不能排除采样点之间的短暂异常。')
    notes.append('核查仅覆盖所列观测；未登记的健康、资源或业务指标不在本次覆盖范围内，整体健康尚未确认。')
    return {'checked_metrics': metrics, 'checked_log_categories': log_categories,
            'unchecked_metrics': missing_metrics, 'unchecked_log_categories': missing_logs,
            'limitations': notes}
