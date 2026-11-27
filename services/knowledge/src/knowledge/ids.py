"""What: uuid7 generator (time-ordered ids) for trace ids.
Use when: a request arrives without X-Trace-Id (doc 07 s1.2 says a fresh uuid7).
Inputs: none. Output: uuid7 string. Side effects: reads the clock and os.urandom.
Role: Python 3.12 has no uuid7, so this is the small RFC 9562 layout: 48-bit ms timestamp, version 7, random rest."""
import os
import time
import uuid


def uuid7() -> str:
    """What: make a uuid7 string. Use when: minting a trace id. Inputs: none. Output: 36-char uuid text.
    Side effects: reads the clock and os.urandom. Role: trace id source."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big") & ((1 << 74) - 1)
    value = (ms << 80) | (0x7 << 76) | ((rand >> 62) << 64) | (0b10 << 62) | (rand & ((1 << 62) - 1))
    return str(uuid.UUID(int=value))
