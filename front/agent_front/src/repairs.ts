export type RepairIncident = {
  id: string; revision: number; latest_run_id: string | null; business_status: string
  purpose: 'diagnosis' | 'status_check'; demo_case_id: string | null
}
export type RepairEvidence = { evidence_id: string; kind: string; excerpt: string }
export type RepairIntent = { action: string; target: string; parameters: Record<string, unknown>; evidence_ids: string[] }
export type RepairApprovalDetails = { condition: string; risk: string; expected_result: string }
export type RepairRecommendation = {
  action: string; condition: string; expected_result: string; risk: string; requires_approval: boolean
  repair?: RepairIntent | null
}
export type RepairResult = {
  run_id: string; status: string; review_status: string; execution_mode?: string; purpose?: string
  incident_snapshot?: { revision: number }
  output?: { recommended_actions?: RepairRecommendation[] }
  evidence?: RepairEvidence[]
}
export type SymptomCheck = { metric: string; operator: string; threshold: number; max_age_seconds: number; unit?: string }
export type RepairCapability = {
  action: string; target: string; impact: string; rollback: string; success_condition: string; symptom_checks: SymptomCheck[]
}
export type RepairEvent = {
  seq: number; type: string; at: string; code?: string; attempt?: number; passed?: boolean
  observation?: unknown; symptoms?: unknown
}
export type RepairPlan = {
  id: string; incident_id: string; status: string; effective_status?: string; digest: string; live: boolean
  proposal: RepairIntent & { revision: number; run_id: string; reason: string; request_key: string;
    approval_details?: RepairApprovalDetails | null }
  recommendation_source?: { run_id: string; action_index: number }
  impact: string; rollback: string; success_condition: string; symptom_checks: SymptomCheck[]
  expires_at: string; created_at: string; approved_by?: string; execution_key?: string
  verification_budget: { attempts: number; interval_seconds: number; execution_seconds: number }
  events: RepairEvent[]
}
export const repairLabels: Record<string, string> = {
  restart_service: '重启服务', pending_approval: '待审批', approved: '已批准，待执行', rejected: '已拒绝',
  executing: '执行与验证中', verified: '恢复验证通过', blocked: '执行前已阻止', manual_required: '需要人工处理',
  expired: '方案已过期', source_changed: '工单或诊断已变化', configuration_changed: '服务配置已变化',
  proposed: '方案已创建', execution_claimed: '已领取执行', preflight_passed: '执行预检完成',
  action_started: '开始重启', action_completed: '重启请求完成', verification: '恢复验证', execution_error: '执行检查异常',
  interrupted: '执行中断，转人工处理',
}
export const repairErrors: Record<string, string> = {
  OPERATOR_REQUIRED: '审批和执行需要本工单所属的 operator 身份。',
  REPAIR_APPROVAL_REQUIRED: '方案尚未批准，请先审批。', REPAIR_EXPIRED: '方案已过期，请创建新方案并重新审批。',
  REPAIR_SOURCE_CHANGED: '工单或最新诊断已变化，请刷新并创建新方案。',
  REPAIR_CONFIGURATION_CHANGED: '服务配置已变化，请创建新方案并重新审批。',
  REPAIR_IN_PROGRESS: '本工单已有修复正在执行，请等待并刷新结果。',
  REPAIR_REQUIRES_REVIEWED_DIAGNOSIS: '需要通过复核且与本工单匹配的完整诊断。',
  REPAIR_REQUIRES_INCIDENT_DIAGNOSIS: '状态核查不能作为修复来源，请使用故障诊断。',
  REPAIR_RECOMMENDATION_INCOMPLETE: '该诊断建议缺少适用条件、风险或预期验证，不能直接采用；请核对报告后填写人工提案。',
  SYNTHETIC_DIAGNOSIS_CANNOT_REPAIR_LIVE_TARGET: '合成演示不能用于真实修复。',
  REPAIR_TARGET_DENIED: '该目标或动作未登记，请核对服务配置。',
  REPAIR_ALREADY_ATTEMPTED: '此方案已尝试执行，请读取结果，不要再次发起新动作。',
  REPAIR_STATE_CONFLICT: '方案状态已变化，请刷新后操作。', SERVICE_NOT_REGISTERED: '该服务尚未登记修复来源。',
  SYMPTOM_ALREADY_HEALTHY: '当前指标已满足恢复条件，没有执行重启。',
}
export function sourceMatches(incident: RepairIncident, result: RepairResult | null): boolean {
  return incident.business_status === 'awaiting_confirmation' && incident.purpose !== 'status_check'
    && !incident.demo_case_id && !!result && result.purpose !== 'status_check'
    && result.status === 'completed' && result.review_status === 'passed' && result.execution_mode === 'live'
    && result.run_id === incident.latest_run_id && result.incident_snapshot?.revision === incident.revision
}
