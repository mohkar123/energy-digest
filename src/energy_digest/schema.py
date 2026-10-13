"""JSON schema export compatible with ``claude --json-schema`` (draft-07)."""

from typing import Any

from pydantic import BaseModel

_REF_PREFIX = '#/$defs/'


def _inline(node: Any, defs: dict[str, Any], stack: tuple[str, ...]) -> Any:  # noqa: ANN401
    if isinstance(node, list):
        return [_inline(v, defs, stack) for v in node]
    if not isinstance(node, dict):
        return node
    ref = node.get('$ref')
    if isinstance(ref, str):
        if not ref.startswith(_REF_PREFIX):
            msg = f'unsupported $ref: {ref}'
            raise ValueError(msg)
        name = ref.removeprefix(_REF_PREFIX)
        if name in stack:
            msg = f'recursive $ref: {name}'
            raise ValueError(msg)
        siblings = {k: v for k, v in node.items() if k != '$ref'}
        resolved = _inline(defs[name], defs, (*stack, name))
        return {**resolved, **_inline(siblings, defs, stack)}
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ('$defs', 'title'):
            # Property names live under 'properties' (handled below), so a
            # field literally named 'title' is never dropped here.
            continue
        new_key = 'items' if key == 'prefixItems' else key
        out[new_key] = (
            {k: _inline(v, defs, stack) for k, v in value.items()}
            if key == 'properties'
            else _inline(value, defs, stack)
        )
    return out


def claude_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return a self-contained draft-07 schema for ``model``.

    ``$ref``/``$defs`` are inlined and ``title`` keys removed, because
    ``claude --json-schema`` validates against draft-07 and a self-contained
    schema avoids reference-resolution problems.
    """
    raw = model.model_json_schema()
    defs = raw.get('$defs', {})
    return _inline(raw, defs, ())
