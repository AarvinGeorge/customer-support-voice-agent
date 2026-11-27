"""What: the strict Pydantic base class every contract model inherits.
Use when: defining any contract model.
Inputs: none.
Output: StrictModel (extra="forbid"; dumps use the wire alias by default; input only by alias).
Side effects: none.
Role: enforces doc 07's rule "every model uses extra=forbid" in one place."""
from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """What: base model that rejects unknown keys.
    Use when: subclassing for any contract shape.
    Inputs: field values.
    Output: a validated model.
    Side effects: none.
    Role: doc 07 notation rule (extra="forbid"); wire names such as "class" are emitted by every
    dump, so no caller has to remember by_alias=True (review PR4 I2)."""

    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)
