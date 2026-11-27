"""What: renders ts/contracts.ts (TypeScript types and constants) from the Python contract models.
Use when: scripts/gen_ts.py writes the file; tests/test_ts_export.py checks it is up to date.
Inputs: none (reads the contract modules).
Output: render_ts() -> the full TypeScript source as a string.
Side effects: none.
Role: keeps the web lane's types generated from, and identical to, the Python source of truth."""
import inspect
import json
from typing import Any

from pydantic.json_schema import models_json_schema

from contracts import VERSION, agent_api, core_api, errors, events, knowledge_api, limits, tools, trust
from contracts.base import StrictModel

_MODULES = (errors, core_api, knowledge_api, agent_api, events, tools)

HEADER = """/**
 * GENERATED FILE. Do not edit. Source: packages/contracts/src/contracts (doc 07 r4).
 * Regenerate: cd packages/contracts && uv run python scripts/gen_ts.py
 * What: TypeScript types and constants for every contract shape the web app sends or reads.
 * Use when: typing API bodies, SSE events and limits in apps/web.
 * Inputs: none.
 * Output: interfaces, the SseEvent union, literal unions and constants.
 * Side effects: none.
 * Role: web mirror of packages/contracts; CI fails if it drifts from the Python models.
 */
"""


def _models() -> list[type[StrictModel]]:
    """What: every StrictModel subclass defined in the contract modules, in a stable order.
    Use when: rendering. Inputs: none. Output: list of classes. Side effects: none. Role: collector."""
    found: dict[str, type[StrictModel]] = {}
    for mod in _MODULES:
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, StrictModel) and obj is not StrictModel and obj.__module__ == mod.__name__:
                found[name] = obj
    return [found[n] for n in sorted(found)]


def _ts(node: Any) -> str:
    """What: TypeScript type expression for one JSON schema node.
    Use when: rendering a property. Inputs: schema node. Output: TS type text. Side effects: none.
    Role: schema-to-TS converter (subset Pydantic emits for these models)."""
    if not isinstance(node, dict) or not node:
        return "unknown"
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    if "const" in node:
        return json.dumps(node["const"])
    if "enum" in node:
        return " | ".join(json.dumps(v) for v in node["enum"])
    for key in ("anyOf", "oneOf"):
        if key in node:
            parts: list[str] = []
            for sub in node[key]:
                t = _ts(sub)
                if t not in parts:
                    parts.append(t)
            return " | ".join(parts)
    typ = node.get("type")
    if isinstance(typ, list):
        return " | ".join(_ts({**node, "type": t}) for t in typ)
    if typ == "string":
        return "string"
    if typ in ("integer", "number"):
        return "number"
    if typ == "boolean":
        return "boolean"
    if typ == "null":
        return "null"
    if typ == "array":
        inner = _ts(node.get("items", {}))
        return f"Array<{inner}>"
    if typ == "object":
        if node.get("properties"):
            return "{ " + " ".join(_prop_lines(node, inline=True)) + " }"
        ap = node.get("additionalProperties")
        if isinstance(ap, dict) and ap:
            return f"Record<string, {_ts(ap)}>"
        return "Record<string, unknown>"
    return "unknown"


def _key(name: str) -> str:
    """What: a TS property key, quoted when not a plain identifier. Use when: rendering properties.
    Inputs: name. Output: key text. Side effects: none. Role: helper."""
    return name if name.isidentifier() and name not in ("class",) else json.dumps(name)


def _prop_lines(node: dict[str, Any], inline: bool = False) -> list[str]:
    """What: one "key?: Type;" line per property. Use when: rendering an object.
    Inputs: object schema, inline flag. Output: lines. Side effects: none. Role: helper."""
    required = set(node.get("required", []))
    out = []
    for name, prop in node.get("properties", {}).items():
        opt = "" if name in required else "?"
        out.append(f"{'' if inline else '  '}{_key(name)}{opt}: {_ts(prop)};")
    return out


def _what(description: str | None) -> str:
    """What: the "What:" line of a docstring for the JSDoc comment. Use when: rendering an interface.
    Inputs: description. Output: one line. Side effects: none. Role: descriptor carry-over (D-015)."""
    if not description:
        return "What: contract shape."
    first = description.strip().splitlines()[0].strip()
    return first if first.startswith("What:") else f"What: {first}"


def _const(name: str, value: Any) -> str:
    """What: an exported "as const" constant. Use when: rendering constants.
    Inputs: name, JSON-able value. Output: TS line. Side effects: none. Role: helper."""
    return f"export const {name} = {json.dumps(value, indent=2)} as const;\n"


def render_ts() -> str:
    """What: the complete ts/contracts.ts text.
    Use when: generating or checking the TS export. Inputs: none. Output: TS source string.
    Side effects: none. Role: the generator."""
    models = _models()
    _, top = models_json_schema([(m, "validation") for m in models], by_alias=True,
                                ref_template="#/$defs/{model}")
    defs = top.get("$defs", {})
    out = [HEADER]
    out.append(f'export const CONTRACT_VERSION = "{VERSION}";\n')
    for name in sorted(defs):
        node = defs[name]
        out.append(f"/** {_what(node.get('description'))} */")
        if node.get("type") == "object":
            out.append(f"export interface {name} {{")
            out.extend(_prop_lines(node))
            out.append("}\n")
        else:
            out.append(f"export type {name} = {_ts(node)};\n")
    event_names = [c.__name__ for c in events._EVENT_CLASSES]
    out.append("/** What: every SSE event, discriminated by `type` (doc 07 s6.2). */")
    out.append("export type SseEvent =\n  | " + "\n  | ".join(event_names) + ";\n")
    out.append("/** What: why a turn ended (doc 07 s6.2). */")
    out.append("export type EndReason = " + " | ".join(json.dumps(r) for r in events.END_REASONS) + ";\n")
    out.append("/** What: SSE event type names. */")
    out.append("export type EventType = SseEvent[\"type\"];\n")
    out.append(_const("EVENT_TYPES", list(events.EVENT_TYPES)))
    out.append(_const("END_REASONS", list(events.END_REASONS)))
    out.append(_const("ERROR_CLASSES", list(errors.ERROR_CLASSES)))
    out.append(_const("AGENT_ERROR_CODES", errors.AGENT_ERROR_CODES))
    out.append(_const("UNTRUSTED_WRAPPER_TAGS", trust.UNTRUSTED_WRAPPER_TAGS))
    out.append("/** What: timeouts, retries and limits (doc 07 s8.1), seconds unless named otherwise. */")
    out.append(_const("LIMITS", limits.as_dict()))
    out.append(_const("VISIBILITY", {k: list(v) for k, v in tools.VISIBILITY.items()}))
    out.append(_const("ASK_SUPPORT_AGENT_TOOL", tools.ASK_SUPPORT_AGENT_TOOL))
    return "\n".join(out)
