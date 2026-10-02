"""Step 4 of the pipeline: DECIDE.

The decision is a short, readable policy, not a black box:

  REJECT        - at least one hard failure (duplicate, arithmetic wrong,
                  spoofed vendor tax-ID, invalid GSTIN, over-billing a PO,
                  changed or invalid bank account, look-alike sender domain).
  AUTO-APPROVE  - every safety check passed AND the amount is below the
                  auto-approval limit. (Missing evidence is never enough.)
  NEEDS REVIEW  - everything else. A human decides, with our reasons in front
                  of them.

The AI never pays anything. Even "auto-approve" only moves the invoice to the
approved queue and writes an audit entry; a human can always override.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import CheckResult, Decision, InvoiceFields

HARD_FAIL_CHECKS = {"duplicate", "math", "vendor", "tax_id", "po_match", "bank", "sender"}
MUST_PASS_FOR_AUTO = {"completeness", "math", "duplicate", "vendor"}
MAY_SKIP_FOR_AUTO = {"tax_id", "po_match", "bank", "sender"}
# Which problem the one-line summary leads with: payment-fraud signals first, because they are the ones
# that cost money if someone skims the summary and pays anyway.
LEAD_ORDER = ["bank", "sender", "completeness", "vendor", "duplicate", "math", "po_match", "tax_id"]


def _lead(checks: list[CheckResult]) -> list[CheckResult]:
    rank = {name: i for i, name in enumerate(LEAD_ORDER)}
    return sorted(checks, key=lambda c: rank.get(c.name, len(rank)))


def _more(n: int) -> str:
    return f" (+{n} more problem{'s' if n > 1 else ''})" if n > 0 else ""


@dataclass
class Policy:
    auto_approve_limit: float = 50_000.0   # in the invoice's own currency


def decide(fields: InvoiceFields, checks: list[CheckResult], policy: Policy | None = None) -> Decision:
    policy = policy or Policy()
    by = {c.name: c for c in checks}

    hard = _lead([c for c in checks if c.status == "fail" and c.name in HARD_FAIL_CHECKS])
    if hard:
        return Decision(
            "reject", "high" if len(hard) > 1 or hard[0].name in {"duplicate", "math", "bank", "sender"} else "medium",
            [c.message for c in hard] + [c.message for c in _lead([c for c in checks if c.status == "warn"])],
            "Recommend REJECT: " + hard[0].message + _more(len(hard) - 1),
        )

    not_ok = [c for c in checks if c.status in ("fail", "warn")]
    blockers = [
        c for c in checks
        if (c.name in MUST_PASS_FOR_AUTO and c.status != "pass")
        or (c.name in MAY_SKIP_FOR_AUTO and c.status not in ("pass", "skip"))
    ]
    too_big = fields.total is not None and fields.total > policy.auto_approve_limit

    if not blockers and not too_big and not not_ok:
        ok = [c.message for c in checks if c.status == "pass"]
        return Decision("auto_approve", "high", ok,
                        "Recommend APPROVE: every safety check passed and the amount is within the auto-approval limit.")

    reasons = [c.message for c in _lead(blockers)]
    for c in _lead(not_ok):
        if c.message not in reasons:
            reasons.append(c.message)
    if too_big:
        reasons.append(f"Amount {fields.total:,.2f} is above the auto-approval limit of {policy.auto_approve_limit:,.0f}, so a person must sign off.")
    first = reasons[0] if reasons else "Some checks could not be completed."
    return Decision("needs_review", "low" if not_ok else "medium", reasons, "Needs HUMAN REVIEW: " + first)
