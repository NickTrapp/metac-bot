"""Deterministic business-state simulator. No real payments or external APIs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


class ToolDenied(Exception):
    """A tool call was rejected by deterministic policy or access control."""


class TransientToolError(Exception):
    """The caller cannot know whether the operation committed; retry idempotently."""


@dataclass
class Order:
    order_id: str
    customer_id: str
    amount_cents: int
    status: Literal["processing", "delivered", "cancelled"]
    delivered_days_ago: int | None = None
    damage_confirmed: bool = False
    refunded_cents: int = 0

    @property
    def refundable_cents(self) -> int:
        return self.amount_cents - self.refunded_cents


@dataclass(frozen=True)
class Session:
    session_id: str
    authenticated_customer_id: str | None


@dataclass(frozen=True)
class OrderView:
    order_id: str
    status: str
    amount_cents: int
    refundable_cents: int
    delivered_days_ago: int | None
    damage_confirmed: bool


@dataclass(frozen=True)
class AgentObservation:
    session_id: str
    orders: tuple[OrderView, ...]


@dataclass(frozen=True)
class RefundReceipt:
    refund_id: str
    order_id: str
    amount_cents: int
    reason: str


@dataclass(frozen=True)
class AuditEvent:
    kind: str
    order_id: str
    detail: str


@dataclass
class World:
    orders: dict[str, Order]
    receipts: list[RefundReceipt] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    audit: list[AuditEvent] = field(default_factory=list)
    idempotency: dict[str, tuple[tuple[str, str, int, str], RefundReceipt]] = field(default_factory=dict)
    # For fault tests: simulate an API timing out *after* it has written the refund.
    timeout_after_commit_once_for: set[str] = field(default_factory=set)
    # For fault tests: timeout before writing anything.
    timeout_before_commit_once_for: set[str] = field(default_factory=set)

    def _deny(self, order_id: object, detail: str, message: str) -> None:
        # Invalid JSON values must not become unhashable dictionary keys or money.
        audit_id = order_id if type(order_id) is str else "<invalid-order-id>"
        self.audit.append(AuditEvent("blocked", audit_id, detail))
        raise ToolDenied(message)

    def _authorized_order(self, session: Session, order_id: str) -> Order:
        if type(order_id) is not str or not order_id.strip():
            self._deny(order_id, "invalid_order_id", "A nonempty string order ID is required")
        order = self.orders.get(order_id)
        if not order:
            self.audit.append(AuditEvent("blocked", order_id, "unknown_order"))
            raise ToolDenied("Order does not exist")
        if not session.authenticated_customer_id or session.authenticated_customer_id != order.customer_id:
            self.audit.append(AuditEvent("blocked", order_id, "unauthorized"))
            raise ToolDenied("Authenticated identity does not own this order")
        return order

    def visible_orders(self, session: Session) -> list[Order]:
        """An LLM may see order details only through this authorization filter."""
        if session.authenticated_customer_id is None:
            return []
        return [x for x in self.orders.values() if x.customer_id == session.authenticated_customer_id]

    def observe(self, session: Session) -> AgentObservation:
        """Give the agent an immutable authorized snapshot, never the mutable world."""
        return AgentObservation(session.session_id, tuple(
            OrderView(o.order_id, o.status, o.amount_cents,
                      o.refundable_cents, o.delivered_days_ago, o.damage_confirmed)
            for o in self.visible_orders(session)))

    def refund(self, session: Session, order_id: str, amount_cents: int,
               reason: str, idempotency_key: str) -> RefundReceipt:
        order = self._authorized_order(session, order_id)
        # Python annotations are not runtime validation. In particular, bool is
        # an int subclass, and NaN defeats ordinary <= and > amount checks.
        if type(amount_cents) is not int:
            self._deny(order_id, "invalid_amount_type", "Refund cents must be an integer, excluding booleans")
        if type(reason) is not str or reason not in ("standard", "damaged"):
            self._deny(order_id, "invalid_reason", "An applicable refund reason is required")
        if type(idempotency_key) is not str or not idempotency_key.strip():
            self._deny(order_id, "missing_idempotency_key", "A nonempty string idempotency key is required")
        fingerprint = (session.authenticated_customer_id or "", order_id, amount_cents, reason)
        if idempotency_key in self.idempotency:
            previous_fingerprint, receipt = self.idempotency[idempotency_key]
            if previous_fingerprint != fingerprint:
                self.audit.append(AuditEvent("blocked", order_id, "idempotency_key_conflict"))
                raise ToolDenied("Idempotency key reused for a different transaction")
            self.audit.append(AuditEvent("replayed", order_id, idempotency_key))
            return receipt

        if order.status != "delivered" or order.delivered_days_ago is None:
            self.audit.append(AuditEvent("blocked", order_id, "not_delivered"))
            raise ToolDenied("Only delivered orders can be refunded")
        within_standard_window = 0 <= order.delivered_days_ago <= 30 and reason == "standard"
        within_damaged_window = (0 <= order.delivered_days_ago <= 60
                                 and reason == "damaged" and order.damage_confirmed)
        if not (within_standard_window or within_damaged_window):
            self.audit.append(AuditEvent("blocked", order_id, "policy_window_or_reason"))
            raise ToolDenied("The refund does not satisfy the applicable policy")
        if amount_cents <= 0 or amount_cents > order.refundable_cents:
            self.audit.append(AuditEvent("blocked", order_id, "invalid_amount"))
            raise ToolDenied("Refund amount exceeds the remaining refundable balance")
        if idempotency_key in self.timeout_before_commit_once_for:
            self.timeout_before_commit_once_for.remove(idempotency_key)
            self.audit.append(AuditEvent("timeout_before_commit", order_id, idempotency_key))
            raise TransientToolError("Network timeout before commit")

        order.refunded_cents += amount_cents
        receipt = RefundReceipt(f"REF-{len(self.receipts) + 1:05d}", order_id, amount_cents, reason)
        self.receipts.append(receipt)
        self.idempotency[idempotency_key] = (fingerprint, receipt)
        self.audit.append(AuditEvent("refunded", order_id, str(amount_cents)))

        if idempotency_key in self.timeout_after_commit_once_for:
            self.timeout_after_commit_once_for.remove(idempotency_key)
            self.audit.append(AuditEvent("timeout_after_commit", order_id, idempotency_key))
            raise TransientToolError("Network timeout after commit; outcome unknown")
        return receipt

    def cancel(self, session: Session, order_id: str) -> None:
        order = self._authorized_order(session, order_id)
        if order.status != "processing":
            self.audit.append(AuditEvent("blocked", order_id, "cannot_cancel"))
            raise ToolDenied("Only orders awaiting fulfillment can be cancelled")
        order.status = "cancelled"
        self.audit.append(AuditEvent("cancelled", order_id, ""))

    def escalate(self, order_id: str, reason: str) -> None:
        # Escalation is always available; it is not equivalent to refund authorization.
        self.escalations.append(order_id)
        self.audit.append(AuditEvent("escalated", order_id, reason))
