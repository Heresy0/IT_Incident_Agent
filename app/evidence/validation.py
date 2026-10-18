"""Validate immutable source references; no model invocation."""
from evidence.ownership import observed_owner_team


class ReferenceValidationError(ValueError):
    """Only fixed reasons and server-generated paths; never rejected model values."""
    def __init__(self, reason, path):
        self.reason, self.path = reason, path
        super().__init__(reason)


def validate_output(output, evidence, allowed_kinds=("observation",)):
    for fi, finding in enumerate(output.findings):
        for ri, ref in enumerate(finding.refs):
            path = f"findings.{fi}.refs.{ri}"
            item = evidence.get(ref.evidence_id)
            if item is None:
                raise ReferenceValidationError("unknown_evidence", path + ".evidence_id")
            if item.kind not in allowed_kinds:
                raise ReferenceValidationError("non_current_evidence", path + ".evidence_id")
            value = item.payload
            for part in ref.field_path.split("."):
                if not isinstance(value, dict) or part not in value:
                    raise ReferenceValidationError("unknown_field", path + ".field_path")
                value = value[part]
            if type(value) is not type(ref.value) or value != ref.value:
                raise ReferenceValidationError("value_mismatch", path + ".value")
            for field, matches in (("unit", ref.unit == item.payload.get("unit", "")),
                    ("observed_at", ref.observed_at == item.observed_from),
                    ("data_version", ref.data_version == item.data_version),
                    ("quote", ref.quote in item.excerpt)):
                if not matches:
                    raise ReferenceValidationError(field + "_mismatch", path + "." + field)
        if not any(ref.field_path in {"value", "message", "error_code", "summary", "team", "escalation", "text", "resolution"}
                   or ref.field_path.startswith("config_summary.") for ref in finding.refs):
            raise ReferenceValidationError("metadata_only_reference", f"findings.{fi}.refs")
    teams = {team for item in evidence.values() if (team := observed_owner_team(item)) is not None}
    if output.escalation_team is not None and output.escalation_team not in teams:
        raise ReferenceValidationError("unobserved_team", "escalation_team")
