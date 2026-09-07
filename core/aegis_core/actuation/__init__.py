"""Input synthesis and preemption.

Own SendInput ctypes wrapper, not PyAutoGUI. Any real physical input pauses the
agent within 100 ms, and no held key survives a stop (invariants 1 and 3)."""
