"""The tag AEGIS stamps on every synthetic input event.

`ARCHITECTURE.md § 8.3` inverts the usual question. Instead of asking "was this
event physical?" — which Windows will not reliably tell us — the low-level hooks
ask "did *we* send this?". Anything we did not send is, by definition, the human.

That inversion is only sound while **every** event AEGIS synthesises carries this
value in `dwExtraInfo`. `input.py` owns the single code path that builds `INPUT`
records and it always sets it; nothing else in the core may call `SendInput`.

Lives in its own module so the hook thread (`preempt.py`) and the input layer
(`input.py`) share one definition without either importing the other.
"""

from __future__ import annotations

from typing import Final

# "AE615" — AEGIS. The low three nibbles are zero and reserved: if a future
# change needs per-event tagging it can widen this into a masked comparison
# without colliding with events already in flight.
AEGIS_SIGNATURE: Final = 0xAE615000
