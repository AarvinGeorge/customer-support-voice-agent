"""What: the one error type for any ingest rule violation.
Use when: parsing, rendering or chunking must fail the run loudly (draft A s2.2 step 8).
Inputs: message naming file, line or key. Output: IngestError. Side effects: none.
Role: the ingest CLI maps it to a non-zero exit."""


class IngestError(Exception):
    """What: an article cannot be ingested. Use when: any rule check fails. Inputs: message.
    Output: exception. Side effects: none. Role: loud failure, never a silent skip."""
