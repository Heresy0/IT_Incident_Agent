"""Bounded correction messages; evidence, schemas and scope stay unchanged."""
import json
from langchain_core.messages import AIMessage, HumanMessage


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def message_chars(messages):
    return sum(len(str(message.content)) for message in messages)


def input_sizes(messages, payload, schema):
    # Counts only: never persist model text, evidence values or credentials here.
    return {'input_chars': message_chars(messages),
        'system_chars': len(str(messages[0].content)),
        'schema_chars': len(encoded(schema)), 'payload_chars': len(encoded(payload)),
        'follow_up_chars': message_chars(messages[2:]),
        'source_rows': len(payload.get('evidence', [])),
        'section_chars': {key: len(encoded(payload[key])) for key in
            ('evidence', 'collection', 'task_results', 'task_completion_gaps', 'evidence_needs',
             'read_only_tools', 'read_only_capabilities', 'repair_capabilities', 'challenge')
            if key in payload}}


def error_context(data, details):
    """Keep the object containing a rejected field, without truncating JSON."""
    selected = {}
    for detail in details:
        path = detail.get('path', '')
        if not isinstance(path, str) or not path:
            continue
        parts, value, traversed = path.split('.'), data, []
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
                traversed.append(part)
                selected['.'.join(traversed)] = value
                break
            else:
                break
            traversed.append(part)
        # The enclosing finding/assessment/action keeps the erroneous selection
        # and its surrounding claim together. Invalid paths cannot invent data.
        if not any(key.startswith(parts[0] + '.') for key in selected) and isinstance(data, dict) and parts[0] in data:
            selected[parts[0]] = data[parts[0]]
    return selected


def rebuild_correction(messages, response, instruction, details, input_limit):
    """Replace previous turns, retaining trusted guidance and one prior candidate.

    Full prior output is preferred. If it cannot fit, use intact error objects;
    if those cannot fit, regenerate from the unchanged base and error guidance.
    The caller's original hard input gate still applies to the rebuilt request.
    """
    base = list(messages[:2])
    guidance = list(dict.fromkeys(str(m.content) for m in messages[2:] if m.type == 'human'))
    suffix = [HumanMessage(content=text) for text in guidance]
    suffix.append(HumanMessage(content=instruction))
    raw = str(response.content)
    data = None
    if len(raw) <= 12000:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            pass
    candidate = encoded(data) if data is not None else raw
    rebuilt, mode = [*base, *suffix], 'omitted'
    full = [*base, AIMessage(content=candidate), *suffix]
    if len(raw) <= 12000 and message_chars(full) <= input_limit:
        # Rejected tool calls never become an API tool-call conversation.
        rebuilt, mode = full, 'full'
    elif isinstance(data, (dict, list)):
        fragments = error_context(data, details)
        if fragments:
            hint = HumanMessage(content='此前输出的出错条目，仅用于定位，不是完整输出；'
                '不要回传此包装，仍按原契约返回完整JSON：' + encoded(fragments))
            partial = [*base, hint, *suffix]
            if message_chars(partial) <= input_limit:
                rebuilt, mode = partial, 'error_context'
    return rebuilt, {'before_chars': message_chars([*messages, response, HumanMessage(content=instruction)]),
        'after_chars': message_chars(rebuilt), 'base_chars': message_chars(base),
        'previous_output_mode': mode, 'previous_output_chars': len(raw),
        'evidence_omitted': False, 'schema_changed': False}
