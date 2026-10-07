"""Small owner-bound business rules shared by persistent and explicit test stores."""
from datetime import datetime, timezone


class IncidentError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(timezone.utc).isoformat()


def same_owner(row, principal):
    return row and (row["tenant_id"], row["user_id"]) == (principal.tenant_id, principal.user_id)


def interrupted_result(row):
    config = row.get("config") or {}
    if config.get("task_type") == "incident":
        return {"run_id": row["run_id"], "task_type": "incident", "incident_id": config["incident_id"],
            "status": "failed", "business_result": "needs_information", "review_status": "not_performed",
            "final": "后端进程中断，本次诊断未完成。请使用新启动键重新诊断；旧运行可继续回放。",
            "run_summary": {"termination_reason": "PROCESS_INTERRUPTED"}, "sources": [],
            "incident_snapshot": config["incident_snapshot"]}
    return {**{k: row[k] for k in ("run_id", "query", "user_id", "thread_id", "tenant_id")},
        "status": "failed", "final": "后端进程中断，本次研究未完成；请重新提交研究。",
        "run_summary": {"termination_reason": "PROCESS_INTERRUPTED"}, "sources": [], "route": "unknown"}
