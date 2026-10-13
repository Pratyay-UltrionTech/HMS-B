"""Diagnostic Domain Workflow Summary Contracts.

Shared semantic representations for Laboratory and Radiology encounters.
Enables cross-module consumers (Appointments, Inpatient) to interpret
diagnostic fulfillment without declaring raw internal database tables or enums.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DiagnosticDomainSummary:
    """Domain-level semantic lifecycle summary for encounter investigations."""

    domain: str = "general"  # "laboratory" | "radiology" | "general"
    requested_count: int = 0
    open_request_count: int = 0
    active_order_count: int = 0
    finalized_result_count: int = 0
    report_pending_count: int = 0
    cancelled_count: int = 0
    unavailable_count: int = 0
    has_open_work: bool = False
    has_final_result: bool = False
    requires_doctor_attention: bool = False
    blocker_descriptions: list[str] = field(default_factory=list)

    # Compatibility aliases
    total_orders: int = 0
    open_orders: int = 0
    completed_orders: int = 0
    cancelled_orders: int = 0
    has_open_investigations: bool = False

    def __post_init__(self) -> None:
        if self.open_orders > 0 and not self.has_open_work:
            self.has_open_work = True
        if self.has_open_investigations and not self.has_open_work:
            self.has_open_work = True
        if self.cancelled_orders > 0 and self.cancelled_count == 0:
            self.cancelled_count = self.cancelled_orders
        if self.total_orders > 0:
            if self.requested_count == 0 and self.active_order_count == 0 and self.finalized_result_count == 0:
                if self.has_open_work:
                    self.active_order_count = self.open_orders or self.total_orders
                elif self.has_final_result:
                    self.finalized_result_count = self.completed_orders or self.total_orders

