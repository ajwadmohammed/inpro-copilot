"""Shared data shapes used across the pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class LineItem:
    description: str = ""
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


@dataclass
class InvoiceFields:
    """What we pull out of an invoice. Every field is optional because real
    documents often lack some of them, and *missing* is itself a finding."""
    vendor: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None     # ISO yyyy-mm-dd
    currency: str | None = None
    subtotal: float | None = None
    tax_amount: float | None = None
    total: float | None = None
    tax_id: str | None = None           # seller's tax / VAT / GSTIN
    po_number: str | None = None
    bank_account: str | None = None     # IBAN, or "account / IFSC", printed for payment
    line_items: list[LineItem] = field(default_factory=list)
    extractor: str = "rules"            # "rules" or "llm"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InvoiceFields":
        d = dict(d)
        items = [LineItem(**li) for li in d.pop("line_items", []) or []]
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(line_items=items, **known)


@dataclass
class CheckResult:
    name: str
    status: str            # "pass" | "warn" | "fail" | "skip"
    message: str           # one plain-English sentence
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    outcome: str           # "auto_approve" | "needs_review" | "reject"
    confidence: str        # "high" | "medium" | "low"
    reasons: list[str]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
