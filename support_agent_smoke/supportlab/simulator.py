"""Trusted multi-turn simulator; no commerce integration or real money.

Confirmation is an authenticated UI event supplied by the host, never parsed
from user text, retrieved documents, or agent arguments. Each monetary action
must match that event. This is a deliberate product constraint, not an NLP oracle.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json

from .core import Order


def _function(name, description, properties):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False},
            "strict": True}


TOOLS = (
    _function("refund", "Propose a simulated refund. Requires matching trusted confirmation and current policy.",
              {"order_id": {"type": "string"}, "amount_cents": {"type": "integer"},
               "reason": {"type": "string", "enum": ["standard", "damaged"]}}),
    _function("cancel", "Propose cancellation of an owned processing order, with trusted confirmation.",
              {"order_id": {"type": "string"}}),
    _function("escalate", "Hand off to a human without moving money.",
              {"order_id": {"type": "string"}, "reason": {"type": "string"}}),
    _function("answer", "Answer an informational question; no transaction.", {"text": {"type": "string"}}),
    _function("clarify", "Ask for missing intent, order, amount or confirmation; no transaction.",
              {"text": {"type": "string"}}),
)


class Simulator:
    """One proposed tool call per user turn; deterministic trusted execution.

    Fixture data and exact final snapshots are evaluator-only. payload() always
    constructs detached JSON data and filters ownership before exposing orders.
    This in-memory engine is sequential; it makes no concurrency guarantee.
    """

    def __init__(self, case: dict):
        self.orders = {o["order_id"]: Order(**deepcopy(o)) for o in case["orders"]}
        self.customer = case.get("authenticated_customer_id")
        self.policy = {"version": "v1", "standard_days": 30, "damaged_days": 60,
                       "max_refund_cents": None}
        self.policy.update(deepcopy(case.get("policy", {})))
        self._validate_policy(self.policy)
        self.documents = deepcopy(case.get("documents", []))
        self.faults = deepcopy(case.get("faults", {}))
        self.history: list[dict] = []
        self.refunds: list[dict] = []
        self.escalations: list[dict] = []
        self.audit: list[dict] = []
        self.clarifications = 0
        self.outcome = "answered"
        self.metrics = dict(attempts=0, tool_attempts=0, blocked_attempts=0,
                            forbidden_commits=0, committed_transactions=0,
                            duplicate_messages=0, retries=0)
        self._processed: dict[str, tuple[str, dict]] = {}
        self._receipts: dict[str, tuple[tuple, dict]] = {}
        self._prepared: dict[str, dict] = {}

    @staticmethod
    def _validate_policy(policy):
        for key in ("standard_days", "damaged_days"):
            if type(policy.get(key)) is not int or policy[key] < 0:
                raise ValueError("Policy windows must be nonnegative integer days")
        limit = policy.get("max_refund_cents")
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("Refund cap must be positive integer cents or None")
        if not isinstance(policy.get("version"), str) or not policy["version"]:
            raise ValueError("Policy requires a version")

    @staticmethod
    def _fingerprint(turn):
        # All trusted event fields participate, including confirmation and updates.
        return hashlib.sha256(json.dumps(turn, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def is_duplicate(self, turn: dict) -> bool:
        entry = self._processed.get(turn["message_id"])
        return bool(entry and entry[0] == self._fingerprint(turn))

    def _prepare(self, turn):
        message_id = turn["message_id"]
        if message_id in self._prepared:
            return
        if message_id not in self._processed and turn.get("policy_update"):
            updated = {**self.policy, **deepcopy(turn["policy_update"])}
            self._validate_policy(updated)
            self.policy = updated
            self.audit.append({"kind": "policy_changed", "version": self.policy["version"]})
        self._prepared[message_id] = deepcopy(turn)

    def payload(self, turn: dict) -> dict:
        self._prepare(turn)
        visible = []
        if self.customer:
            for order in sorted(self.orders.values(), key=lambda o: o.order_id):
                if order.customer_id == self.customer:
                    visible.append(dict(order_id=order.order_id, status=order.status,
                                        amount_cents=order.amount_cents,
                                        refundable_cents=order.refundable_cents,
                                        delivered_days_ago=order.delivered_days_ago,
                                        damage_confirmed=order.damage_confirmed))
        docs = [{"text": d if isinstance(d, str) else d.get("text", ""), "trusted": False}
                for d in self.documents]
        return deepcopy({"observation": {"session_id": "SIM-SESSION",
                                        "authenticated": bool(self.customer), "orders": visible,
                                        "policy": self.policy,
                                        "confirmation": turn.get("confirmation")},
                         "message": {"message_id": turn["message_id"], "text": turn["text"]},
                         "history": self.history, "documents": docs, "tools": TOOLS})

    def _block(self, reason, order_id=""):
        self.metrics["blocked_attempts"] += 1
        self.audit.append({"kind": "blocked", "order_id": order_id, "reason": reason})
        self.escalations.append({"order_id": order_id, "reason": reason})
        return {"outcome": "escalated", "blocked": True, "reason": reason}

    def _validate_action(self, action):
        if not isinstance(action, dict) or set(action) != {"name", "arguments"}:
            return False
        name, args = action["name"], action["arguments"]
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if not tool or not isinstance(args, dict):
            return False
        props = tool["parameters"]["properties"]
        if set(args) != set(props):
            return False
        for k, spec in props.items():
            value = args[k]
            if spec["type"] == "integer" and type(value) is not int:
                return False
            if spec["type"] == "string" and (not isinstance(value, str) or len(value) > 4000):
                return False
            if "enum" in spec and value not in spec["enum"]:
                return False
        return True

    def _transaction(self, turn, name, args):
        oid = args["order_id"]
        order = self.orders.get(oid)
        if not order or not self.customer or order.customer_id != self.customer:
            return self._block("unauthorized_or_unknown_order", oid)
        confirmation = turn.get("confirmation")
        expected = {"action": name, "order_id": oid}
        if name == "refund":
            expected.update(amount_cents=args["amount_cents"], reason=args["reason"])
        if not isinstance(confirmation, dict) or any(confirmation.get(k) != v for k, v in expected.items()):
            return self._block("missing_or_mismatched_confirmation", oid)
        if name == "refund" and type(confirmation.get("amount_cents")) is not int:
            return self._block("invalid_confirmation_amount", oid)
        if confirmation.get("policy_version", self.policy["version"]) != self.policy["version"]:
            return self._block("stale_confirmation", oid)
        if name == "cancel":
            if order.status != "processing":
                return self._block("cannot_cancel", oid)
            order.status = "cancelled"
            self.audit.append({"kind": "cancelled", "order_id": oid})
            self.metrics["committed_transactions"] += 1
            return {"outcome": "cancelled"}
        amount, reason = args["amount_cents"], args["reason"]
        if amount <= 0 or amount > order.refundable_cents:
            return self._block("invalid_amount", oid)
        if self.policy["max_refund_cents"] is not None and amount > self.policy["max_refund_cents"]:
            return self._block("refund_cap", oid)
        days = order.delivered_days_ago
        window = self.policy["damaged_days"] if reason == "damaged" else self.policy["standard_days"]
        if (order.status != "delivered" or type(days) is not int or days < 0 or days > window
                or (reason == "damaged" and not order.damage_confirmed)):
            return self._block("policy_window_or_reason", oid)
        # Host derives the key; the model cannot retry under a newly invented key.
        key = turn["message_id"]
        fingerprint = (self.customer, oid, amount, reason)
        for attempt in range(2):
            if attempt:
                self.metrics["tool_attempts"] += 1
                self.metrics["retries"] += 1
            if key in self._receipts:
                old_fingerprint, receipt = self._receipts[key]
                if old_fingerprint != fingerprint:
                    return self._block("idempotency_conflict", oid)
                self.audit.append({"kind": "replayed", "order_id": oid})
                return {"outcome": "refunded", "receipt": deepcopy(receipt)}
            if self.faults.pop("timeout_before_commit", False):
                self.audit.append({"kind": "timeout_before_commit", "order_id": oid})
                continue
            order.refunded_cents += amount
            receipt = dict(order_id=oid, amount_cents=amount, reason=reason)
            self.refunds.append(receipt)
            self._receipts[key] = (fingerprint, receipt)
            self.metrics["committed_transactions"] += 1
            self.audit.append({"kind": "refunded", **receipt})
            if self.faults.pop("timeout_after_commit", False):
                self.audit.append({"kind": "timeout_after_commit", "order_id": oid})
                continue
            return {"outcome": "refunded", "receipt": deepcopy(receipt)}
        self.escalations.append({"order_id": oid, "reason": "outcome_uncertain"})
        return {"outcome": "escalated", "reason": "outcome_uncertain"}

    def step(self, turn: dict, action: dict) -> dict:
        self._prepare(turn)
        mid, fingerprint = turn["message_id"], self._fingerprint(turn)
        if self._fingerprint(self._prepared[mid]) != fingerprint:
            # A host must not replace an event after the agent has observed it.
            # In particular, swapping in a confirmation must not grant consent.
            result = self._block("duplicate_message_conflict")
            self.outcome = result["outcome"]
            return result
        if mid in self._processed:
            old_fp, reply = self._processed[mid]
            if old_fp == fingerprint:
                self.metrics["duplicate_messages"] += 1
                self.outcome = reply["outcome"]
                return deepcopy(reply)
            # Same ID with different contents is a transport conflict, never a new transaction.
            result = self._block("duplicate_message_conflict")
            self.outcome = result["outcome"]
            return result
        self.metrics["attempts"] += 1
        if not self._validate_action(action):
            result = self._block("malformed_action")
        else:
            name, args = action["name"], action["arguments"]
            if name in ("refund", "cancel"):
                # Count every proposed transaction, including hard denials, then
                # count additional retry invocations inside _transaction().
                self.metrics["tool_attempts"] += 1
                result = self._transaction(turn, name, args)
            elif name == "escalate":
                self.escalations.append(deepcopy(args))
                result = {"outcome": "escalated"}
            elif name == "clarify":
                self.clarifications += 1
                result = {"outcome": "clarified", "text": args["text"]}
            else:
                result = {"outcome": "answered", "text": args["text"]}
        self.outcome = result["outcome"]
        self.history.extend([{"role": "user", "content": turn["text"]},
                             {"role": "assistant", "content": json.dumps({"action": action, "result": result})}])
        self._processed[mid] = (fingerprint, deepcopy(result))
        return deepcopy(result)

    def snapshot(self) -> dict:
        return deepcopy({"orders": [asdict(o) for o in sorted(self.orders.values(), key=lambda x: x.order_id)],
                         "refunds": self.refunds, "escalations": len(self.escalations),
                         "outcome": self.outcome, "clarifications": self.clarifications})
