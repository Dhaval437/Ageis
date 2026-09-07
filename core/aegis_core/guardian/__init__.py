"""The policy engine every tool call passes through.

Risk tiers, scope enforcement, and approvals. DANGEROUS never runs unattended,
approval timeouts deny, and the FORBIDDEN list is compiled in and unreachable
from the UI (invariants 4, 5, 6)."""
