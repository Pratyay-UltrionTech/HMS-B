"""
Pharmacy API endpoints adhering to UltrionTech-Backend-Template and legacy route parity.

Prefix: /pharmacy
Tags: ["pharmacy"]
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.pharmacy.actions.pharmacy_actions import PharmacyActions
from hms_migration.modules.pharmacy.contracts.pharmacy_contracts import (
    AdjustmentCreate,
    AdjustmentResponse,
    BatchResponse,
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
    CustomerReturnCreate,
    InventoryRow,
    MedicineCreate,
    MedicineResponse,
    MedicineUpdate,
    PharmacyDashboard,
    PurchaseCreate,
    PurchaseResponse,
    PurchaseReturnCreate,
    ReturnResponse,
    SaleCreate,
    SaleResponse,
    SearchHit,
    SupplierCreate,
    SupplierResponse,
    SupplierUpdate,
)
from hms_migration.modules.pharmacy.entities.pharmacy_entities import (
    PharmacyPaymentStatus,
    PharmacySaleType,
)
from hms_migration.shared.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/pharmacy", tags=["pharmacy"])


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_model=PharmacyDashboard)
def dashboard(
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PharmacyDashboard:
    return PharmacyActions(db, hospital_id, user).get_dashboard()


# ── Categories ────────────────────────────────────────────────────────────────

@router.get("/categories", response_model=list[CategoryResponse])
def list_categories(
    active_only: bool | None = Query(None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[CategoryResponse]:
    return PharmacyActions(db, hospital_id, user).list_categories(active_only)


@router.post("/categories", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> CategoryResponse:
    return PharmacyActions(db, hospital_id, user).create_category(payload)


@router.patch("/categories/{category_id}", response_model=CategoryResponse)
def update_category(
    category_id: UUID,
    payload: CategoryUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> CategoryResponse:
    return PharmacyActions(db, hospital_id, user).update_category(category_id, payload)


# ── Suppliers ─────────────────────────────────────────────────────────────────

@router.get("/suppliers", response_model=list[SupplierResponse])
def list_suppliers(
    active_only: bool | None = Query(None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[SupplierResponse]:
    return PharmacyActions(db, hospital_id, user).list_suppliers(active_only)


@router.post("/suppliers", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
def create_supplier(
    payload: SupplierCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SupplierResponse:
    return PharmacyActions(db, hospital_id, user).create_supplier(payload)


@router.patch("/suppliers/{supplier_id}", response_model=SupplierResponse)
def update_supplier(
    supplier_id: UUID,
    payload: SupplierUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SupplierResponse:
    return PharmacyActions(db, hospital_id, user).update_supplier(supplier_id, payload)


# ── Medicines ─────────────────────────────────────────────────────────────────

@router.get("/medicines", response_model=list[MedicineResponse])
def list_medicines(
    search: str | None = Query(None),
    category_id: UUID | None = Query(None),
    active_only: bool | None = Query(None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[MedicineResponse]:
    return PharmacyActions(db, hospital_id, user).list_medicines(
        search=search,
        category_id=category_id,
        active_only=active_only,
    )


@router.post("/medicines", response_model=MedicineResponse, status_code=status.HTTP_201_CREATED)
def create_medicine(
    payload: MedicineCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> MedicineResponse:
    return PharmacyActions(db, hospital_id, user).create_medicine(payload)


@router.get("/medicines/{medicine_id}", response_model=MedicineResponse)
def get_medicine(
    medicine_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> MedicineResponse:
    return PharmacyActions(db, hospital_id, user).get_medicine(medicine_id)


@router.patch("/medicines/{medicine_id}", response_model=MedicineResponse)
def update_medicine(
    medicine_id: UUID,
    payload: MedicineUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> MedicineResponse:
    return PharmacyActions(db, hospital_id, user).update_medicine(medicine_id, payload)


@router.get("/medicines/{medicine_id}/batches", response_model=list[BatchResponse])
def list_batches(
    medicine_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[BatchResponse]:
    return PharmacyActions(db, hospital_id, user).list_batches(medicine_id)


# ── Inventory & Search ────────────────────────────────────────────────────────

@router.get("/inventory", response_model=list[InventoryRow])
def list_inventory(
    search: str | None = Query(None),
    status: str | None = Query(None),
    category_id: UUID | None = Query(None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[InventoryRow]:
    return PharmacyActions(db, hospital_id, user).list_inventory(
        search=search,
        status_filter=status,
        category_id=category_id,
    )


@router.get("/search", response_model=list[SearchHit])
def search_items(
    q: str = Query(..., min_length=1),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[SearchHit]:
    return PharmacyActions(db, hospital_id, user).search(q, limit)


# ── Purchases ─────────────────────────────────────────────────────────────────

@router.get("/purchases", response_model=list[PurchaseResponse])
def list_purchases(
    supplier_id: UUID | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[PurchaseResponse]:
    return PharmacyActions(db, hospital_id, user).list_purchases(
        supplier_id=supplier_id,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
    )


@router.get("/purchases/{purchase_id}", response_model=PurchaseResponse)
def get_purchase(
    purchase_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PurchaseResponse:
    return PharmacyActions(db, hospital_id, user).get_purchase(purchase_id)


@router.post("/purchases", response_model=PurchaseResponse, status_code=status.HTTP_201_CREATED)
def create_purchase(
    payload: PurchaseCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PurchaseResponse:
    return PharmacyActions(db, hospital_id, user).create_purchase(payload)


# ── Sales ─────────────────────────────────────────────────────────────────────

@router.get("/sales", response_model=list[SaleResponse])
def list_sales(
    search: str | None = Query(None),
    sale_type: PharmacySaleType | None = Query(None),
    payment_status: PharmacyPaymentStatus | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    patient_id: UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[SaleResponse]:
    return PharmacyActions(db, hospital_id, user).list_sales(
        search=search,
        sale_type=sale_type,
        payment_status=payment_status,
        from_date=from_date,
        to_date=to_date,
        patient_id=patient_id,
        limit=limit,
    )


@router.get("/sales/{sale_id}", response_model=SaleResponse)
def get_sale(
    sale_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SaleResponse:
    return PharmacyActions(db, hospital_id, user).get_sale(sale_id)


@router.post("/sales", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
def create_sale(
    payload: SaleCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SaleResponse:
    return PharmacyActions(db, hospital_id, user).create_sale(payload)


@router.post("/sales/{sale_id}/cancel", response_model=SaleResponse)
def cancel_sale(
    sale_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SaleResponse:
    return PharmacyActions(db, hospital_id, user).cancel_sale(sale_id)


# ── Adjustments ───────────────────────────────────────────────────────────

@router.get("/adjustments", response_model=list[AdjustmentResponse])
def list_adjustments(
    medicine_id: UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[AdjustmentResponse]:
    return PharmacyActions(db, hospital_id, user).list_adjustments(medicine_id, limit)


@router.post("/adjustments", response_model=AdjustmentResponse, status_code=status.HTTP_201_CREATED)
def create_adjustment(
    payload: AdjustmentCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> AdjustmentResponse:
    return PharmacyActions(db, hospital_id, user).create_adjustment(payload)


# ── Returns ───────────────────────────────────────────────────────────────

@router.post("/returns/customer", response_model=ReturnResponse, status_code=status.HTTP_201_CREATED)
def customer_return(
    payload: CustomerReturnCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> ReturnResponse:
    return PharmacyActions(db, hospital_id, user).customer_return(payload)


@router.post("/returns/purchase", response_model=ReturnResponse, status_code=status.HTTP_201_CREATED)
def purchase_return(
    payload: PurchaseReturnCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> ReturnResponse:
    return PharmacyActions(db, hospital_id, user).purchase_return(payload)


@router.get("/returns", response_model=list[ReturnResponse])
def list_returns(
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[ReturnResponse]:
    return PharmacyActions(db, hospital_id, user).list_returns(limit)


# ── Reports ───────────────────────────────────────────────────────────────

@router.get("/reports")
def pharmacy_reports(
    report: str = Query(...),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> dict[str, Any]:
    return PharmacyActions(db, hospital_id, user).get_reports(
        report=report,
        from_date=from_date,
        to_date=to_date,
    )
