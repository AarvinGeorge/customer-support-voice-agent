"""What: the strict Pydantic base class every contract model inherits.
Use when: defining any contract model.
Inputs: none.
Output: StrictModel (extra="forbid", aliases usable by name).
Side effects: none.
Role: enforces doc 07's rule "every model uses extra=forbid" in one place."""
from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """What: base model that rejects unknown keys.
    Use when: subclassing for any contract shape.
    Inputs: field values.
    Output: a validated model.
    Side effects: none.
    Role: doc 07 notation rule (extra="forbid")."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
