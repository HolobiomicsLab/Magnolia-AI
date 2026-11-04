"""Router / Work Item control-plane prototype (dsh-inspired vertical slice).

PROVISIONAL: every design choice in this package is subject to change.
See docs/router-prototype-decisions.md and docs/dsh-magnolia-v2-router-assessment.md.

Scope: deterministic event intake, admission, content-bound human approval,
Work Item lifecycle, fake campaign adapter, durable effects, evidence links.
No LLM Eval, no proactive automation, no real job submission, no KG projection.
"""

__version__ = "0.1.0"
