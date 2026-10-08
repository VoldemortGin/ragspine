"""The historical file-layout pointer writer, kept verbatim as test infrastructure.

``ProcessingStore._write_pointer`` moved behind the backend seam (``FileBackend.put_stage_entry``
/ ``set_pointer``, sqlite object store PR-2); the tests that simulate a *rival writer* or an
*older release* keep this byte-exact copy so what they pin stays the historical format, not
whatever the current write path produces.
"""

import os
import tempfile
from pathlib import Path

from ragspine.common.evidence.file_placement import link_new_file


def write_pointer(
    target: Path,
    digest: str,
    *,
    immutable: bool = False,
    inline: bytes = b"",
    artifact: bytes = b"",
) -> None:
    """Write ``digest`` (then ``inline``, if any) as one line each, then ``artifact``'s raw
    bytes (only after an ``inline`` envelope). An immutable pointer is first-writer-wins:
    an existing one is accepted only when its first line is ``digest``."""
    if artifact and not inline:
        raise ValueError("An inline stage output follows its envelope")
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(digest.encode() + b"\n" + (inline + b"\n" if inline else b"") + artifact)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if immutable:
            try:
                link_new_file(temporary, target)
            except FileExistsError:
                if target.read_bytes().partition(b"\n")[0].strip() != digest.encode():
                    raise ValueError("Conflicting immutable stage cache entry") from None
        else:
            os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
