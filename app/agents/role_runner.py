"""Bounded structured model calls; workflow follow-up and publication use explicit hooks."""
from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError
from agents.investigation import validation_details
from agents.protocol import correction_instruction
from evidence.diagnosis import ProtocolError
from runtime.context import RunContext, ExecutionError, invoke_chat_model
from workflow.model_input import input_sizes, rebuild_correction
from agents.contracts import ReviewDecision


@dataclass(frozen=True)
class RoleRequest:
    role: str
    schema: type[BaseModel]
    output_schema: dict
    model_input: dict
    prompt: str
    terminal: bool = False
    reject_truncated: bool = False
    validation_fields: tuple[str, ...] = ()


class RoleRunner:
    """Own request-local messages only; RunContext owns accounting and correction claims."""
    def __init__(self, context: RunContext, repairs: dict, *, reviewer_repair_mode: str,
                 role_corrections: bool, purpose: str):
        self.context, self.repairs = context, repairs
        self.reviewer_repair_mode, self.role_corrections = reviewer_repair_mode, role_corrections
        self.purpose = purpose

    def run(self, request: RoleRequest, model, *, emit: Callable[..., None],
            parse: Callable[[str], Any], validate=None, follow_up=None, exhausted=None, correction=None):
        role, schema = request.role, request.schema
        output_schema, model_input = request.output_schema, request.model_input
        prompt, terminal = request.prompt, request.terminal
        messages = [SystemMessage(content=prompt), HumanMessage(content=json.dumps({
            "input": model_input, "output_schema": output_schema}, ensure_ascii=False, separators=(',', ':')))]
        attempt = 0
        value = None
        schema_version = hashlib.sha256(json.dumps(schema.model_json_schema(), sort_keys=True).encode()).hexdigest()[:12]
        while True:
            attempt += 1
            emit('model_input', role, schema_name=schema.__name__, attempt=attempt,
                **input_sizes(messages, model_input, output_schema))
            response = invoke_chat_model(model, messages, role, terminal=terminal)
            deadline = self.context.limits.seconds - (0 if terminal else self.context.limits.reserve_seconds)
            if time.monotonic() - self.context.started >= deadline:
                raise ExecutionError("TIME_BUDGET_EXCEEDED")
            emit("model_output", role, output_chars=len(str(response.content)))
            try:
                if request.reject_truncated and getattr(response,'response_metadata',{}).get('finish_reason')=='length':
                    raise ProtocolError('MODEL_OUTPUT_TRUNCATED',[{'path':'response',
                        'instruction':'响应触及输出上限，不能作为完整复核；在原额度内重写精简且完整的JSON。'}])
                if response.tool_calls or getattr(response, "invalid_tool_calls", None) or len(str(response.content)) > 12000:
                    raise ProtocolError("ROLE_OUTPUT_INVALID")
                content = response.content
                value = parse(content)
                result = validate(value) if validate else value
                instruction = follow_up(result) if follow_up else None
                if instruction is not None:
                    messages.extend([response, HumanMessage(content=instruction)])
                    continue
                return result
            except (ValidationError, ValueError, TypeError) as exc:
                details = {"reason": exc.code, "details": exc.details} if isinstance(exc, ProtocolError) else validation_details(exc, schema, extra_fields=request.validation_fields)
                emit("validation_failure", role, **details, attempt=attempt,
                           model_call_index=self.context.counts['model_calls'],
                           repair_count=self.repairs['repairs'], schema_name=schema.__name__,
                           schema_version=schema_version)
                stage = 'ReviewDecision' if issubclass(schema, ReviewDecision) else schema.__name__
                review_stage = stage if role == 'reviewer' and self.reviewer_repair_mode == 'split' else None
                repair_role = role if self.role_corrections else None
                if not self.context.claim_repair(self.repairs, reviewer=role == 'reviewer', review_stage=review_stage, repair_role=repair_role):
                    preserved = exhausted(exc, details, value) if exhausted else None
                    if preserved is not None:
                        return preserved
                    raise ExecutionError("MODEL_OUTPUT_INVALID") from None
                emit("validation_repair", role, review_stage=review_stage, repair_role=repair_role)
                instruction = correction_instruction(role, schema, details, self.purpose)
                if correction:
                    instruction += correction(details)
                messages, rebuilt = rebuild_correction(messages, response,
                    instruction + '。仅返回 JSON，不调用工具，不增加身份字段。',
                    details.get('details', []), self.context.limits.input_chars)
                emit('correction_input_rebuilt', role, schema_name=schema.__name__, **rebuilt)
