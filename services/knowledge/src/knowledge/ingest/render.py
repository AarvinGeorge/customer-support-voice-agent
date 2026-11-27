"""What: render {policy.<dotted.key>[|dollars|days]} placeholders in article text from data/policy.yaml.
Use when: the chunker renders each block (doc 07 s13.1 last bullet).
Inputs: text, PolicyConfig, file name, first line number. Output: (rendered text, keys used in order).
Side effects: none. Role: an unknown or non-scalar key fails ingest naming key, file and line; the value lookup is
contracts.policy.render_placeholder so core and knowledge agree."""
import re

from contracts.policy import PolicyConfig, PolicyError, render_placeholder
from knowledge.ingest.errors import IngestError

_PLACEHOLDER = re.compile(r"\{policy\.([A-Za-z0-9_.]+(?:\|[A-Za-z]+)?)\}")


def render_policy(text: str, policy: PolicyConfig, filename: str, first_line: int = 1) -> tuple[str, list[str]]:
    """What: replace every placeholder in text. Use when: rendering one block.
    Inputs: text, policy, filename, line of the text's first line.
    Output: (text, keys) where keys are the bare dotted keys without formatter. Side effects: none.
    Role: raises IngestError("<file>:<line> ... <key>") on any bad placeholder."""
    keys: list[str] = []

    def sub(m: re.Match) -> str:
        """What: resolve one match. Use when: re.sub calls it. Inputs: match. Output: value text.
        Side effects: appends to keys. Role: per-placeholder lookup with a located error."""
        expr = m.group(1)
        line = first_line + text.count("\n", 0, m.start())
        try:
            value = render_placeholder(policy, expr)
        except PolicyError as exc:
            raise IngestError(f"{filename}:{line}: {{policy.{expr}}}: {exc}") from exc
        keys.append(expr.split("|")[0])
        return value

    return _PLACEHOLDER.sub(sub, text), keys
