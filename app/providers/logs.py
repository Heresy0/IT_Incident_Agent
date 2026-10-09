"""Normalize registered log formats without forwarding whole event documents."""
import math
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


FieldName = Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')]
EventName = Annotated[str, Field(pattern=r'^[A-Za-z][A-Za-z0-9_.-]{0,119}$')]
SAFE_DETAIL_FIELDS = {
    'count', 'attempt_count', 'indexed_chunk_count', 'duration_ms', 'heartbeat_seconds',
    'metrics_port', 'status', 'outcome', 'error_type', 'queue_depth', 'active_workers',
}


class LogMapping(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    message_field: FieldName = 'message'
    level_field: FieldName = 'level'
    error_code_field: FieldName = 'error_code'
    event_field: FieldName = 'event'
    detail_fields: list[FieldName] = Field(default_factory=list, max_length=11)
    event_only: bool = False
    event_allowlist: list[EventName] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def safe_event_projection(self):
        if any(name not in SAFE_DETAIL_FIELDS for name in self.detail_fields):
            raise ValueError('Only operational scalar fields are allowed')
        if self.event_only and not self.event_allowlist:
            raise ValueError('Event-only mapping requires registered event names')
        return self

    def normalize(self, record):
        """Return message/level/code; None means no usable registered log content."""
        if not isinstance(record, dict):
            return None
        event = record.get(self.event_field)
        valid_event = (isinstance(event, str)
                       and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,119}', event)
                       and (not self.event_allowlist or event in self.event_allowlist))
        message = record.get(self.message_field)
        if self.event_only or not isinstance(message, str) or not message.strip():
            if not valid_event:
                return None
            parts = [f'event={event}']
            for field in self.detail_fields:
                value = record.get(field)
                if (type(value) is int and abs(value) <= 10**12
                        or type(value) is float and math.isfinite(value) and abs(value) <= 10**12
                        or type(value) is bool
                        or isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value)):
                    parts.append(f'{field}={value}')
            message = '; '.join(parts)
        level = str(record.get(self.level_field, 'UNKNOWN')).upper()
        if level == 'WARNING':
            level = 'WARN'
        if level not in {'INFO', 'WARN', 'ERROR', 'DEBUG', 'CRITICAL'}:
            level = 'UNKNOWN'
        code = record.get(self.error_code_field)
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z0-9_]{1,64}', code):
            code = None
        return message, level, code
