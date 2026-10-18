"""Shared schema presentation; no agent or workflow dependency."""

def compact_schema(schema):
    """Remove presentation metadata, preserving validation constraints."""
    if isinstance(schema, list):
        return [compact_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    return {key: compact_schema(value) for key, value in schema.items()
            if key not in {'title', 'examples'}}
