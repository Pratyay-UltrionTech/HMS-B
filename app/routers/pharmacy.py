"""Pharmacy Management — walk-in counter sales, inventory, purchases."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    BillingSourceType,
    Medicine,
    MedicineBatch,
    MedicineCategory,
    MedicineInventory,
    Patient,
    PharmacyPaymentStatus,
    PharmacyPurchase,
    PharmacyPurchaseItem,
    PharmacyReturn,
    PharmacySale,
    PharmacySaleItem,
    PharmacySaleStatus,
    PharmacySupplier,
    StockAdjustment,
    StockAdjustmentType,
    StockTransactionType,
)
from app.schemas_pharmacy import (
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
    PurchaseItemResponse,
    PurchaseReturnCreate,
    ReturnResponse,
    SaleCreate,
    SaleItemResponse,
    SaleResponse,
    SearchHit,
    SupplierCreate,
    SupplierResponse,
    SupplierUpdate,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user
from app.utils.pharmacy_stock import (
    allocate_fifo,
    apply_stock_change,
    inventory_status,
    next_doc_number,
    refresh_medicine_inventory,
)

router = APIRouter(prefix="/pharmacy", tags=["pharmacy"])


def _actor_name(user: dict) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _line_amounts(qty: float, unit_price: float, gst_percent: float, discount: float = 0.0) -> tuple[float, float, float]:
    taxable = max(0.0, round(qty * unit_price - discount, 2))
    gst = round(taxable * float(gst_percent or 0) / 100.0, 2)
    net = round(taxable + gst, 2)
    return taxable, gst, net


def _category_resp(c: MedicineCategory) -> CategoryResponse:
    return CategoryResponse.model_validate(c)


def _supplier_resp(s: PharmacySupplier) -> SupplierResponse:
    return SupplierResponse.model_validate(s)


def _medicine_resp(m: Medicine, inv: MedicineInventory | None = None) -> MedicineResponse:
    data = MedicineResponse.model_validate(m)
    data.category_name = m.category.name if m.category else None
    if inv:
        data.current_stock = float(inv.current_stock or 0)
        data.available_stock = float(inv.available_stock or 0)
    elif m.inventory:
        data.current_stock = float(m.inventory.current_stock or 0)
        data.available_stock = float(m.inventory.available_stock or 0)
    return data


def _batch_resp(b: MedicineBatch, today: date | None = None) -> BatchResponse:
    today = today or date.today()
    data = BatchResponse.model_validate(b)
    data.medicine_name = b.medicine.medicine_name if b.medicine else None
    data.is_expired = b.expiry_date < today
    data.expiring_soon = (not data.is_expired) and (b.expiry_date - today).days <= 30
    return data


def _sale_resp(sale: PharmacySale, warnings: list[str] | None = None) -> SaleResponse:
    items = []
    for it in sale.items or []:
        row = SaleItemResponse.model_validate(it)
        row.medicine_name = it.medicine.medicine_name if it.medicine else None
        items.append(row)
    data = SaleResponse.model_validate(sale)
    data.items = items
    data.expiry_warnings = warnings or []
    return data


def _purchase_resp(p: PharmacyPurchase) -> PurchaseResponse:
    items = []
    for it in p.items or []:
        row = PurchaseItemResponse.model_validate(it)
        row.medicine_name = it.medicine.medicine_name if it.medicine else None
        items.append(row)
    data = PurchaseResponse.model_validate(p)
    data.supplier_name = p.supplier.company_name if p.supplier else None
    data.items = items
    return data


# ── Dashboard ─────────────────────────────────────────────────────────────────
@router.get("/dashboard", response_model=PharmacyDashboard)
def dashboard(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = date.today()
    sales_today = (
        db.query(PharmacySale)
        .filter(
            PharmacySale.hospital_id == hospital_id,
            PharmacySale.sale_date == today,
            PharmacySale.status != PharmacySaleStatus.cancelled,
        )
        .all()
    )
    today_sales_amount = round(sum(float(s.net_amount or 0) for s in sales_today), 2)
    today_profit = 0.0
    for s in sales_today:
        for it in s.items or []:
            today_profit += (float(it.unit_price or 0) - float(it.purchase_price or 0)) * float(it.quantity or 0)
    today_profit = round(today_profit, 2)

    purchases_today = (
        db.query(func.coalesce(func.sum(PharmacyPurchase.net_amount), 0.0))
        .filter(PharmacyPurchase.hospital_id == hospital_id, PharmacyPurchase.received_date == today)
        .scalar()
        or 0
    )

    inv_rows = (
        db.query(MedicineInventory, Medicine)
        .join(Medicine, Medicine.id == MedicineInventory.medicine_id)
        .filter(MedicineInventory.hospital_id == hospital_id, Medicine.is_active.is_(True))
        .all()
    )
    low = out = expired = expiring = 0
    alerts: list[dict] = []
    for inv, med in inv_rows:
        st = inventory_status(inv, med, today)
        if st == "out_of_stock":
            out += 1
        elif st == "expired":
            expired += 1
        elif st == "expiring_soon":
            expiring += 1
        elif st == "low_stock":
            low += 1
        if st != "ok":
            alerts.append(
                {
                    "medicine_id": str(med.id),
                    "medicine_name": med.medicine_name,
                    "status": st,
                    "stock": float(inv.current_stock or 0),
                    "nearest_expiry": inv.nearest_expiry.isoformat() if inv.nearest_expiry else None,
                }
            )

    pending_bills = (
        db.query(func.count(PharmacySale.id))
        .filter(
            PharmacySale.hospital_id == hospital_id,
            PharmacySale.payment_status != PharmacyPaymentStatus.paid,
            PharmacySale.status != PharmacySaleStatus.cancelled,
        )
        .scalar()
        or 0
    )

    top = (
        db.query(
            Medicine.medicine_name,
            func.sum(PharmacySaleItem.quantity).label("qty"),
            func.sum(PharmacySaleItem.net_amount).label("amt"),
        )
        .join(PharmacySaleItem, PharmacySaleItem.medicine_id == Medicine.id)
        .join(PharmacySale, PharmacySale.id == PharmacySaleItem.sale_id)
        .filter(
            PharmacySale.hospital_id == hospital_id,
            PharmacySale.status != PharmacySaleStatus.cancelled,
            PharmacySale.sale_date >= today.replace(day=1),
        )
        .group_by(Medicine.medicine_name)
        .order_by(func.sum(PharmacySaleItem.quantity).desc())
        .limit(5)
        .all()
    )

    latest_purchases = (
        db.query(PharmacyPurchase)
        .options(joinedload(PharmacyPurchase.supplier))
        .filter(PharmacyPurchase.hospital_id == hospital_id)
        .order_by(PharmacyPurchase.created_at.desc())
        .limit(5)
        .all()
    )
    recent_sales = (
        db.query(PharmacySale)
        .filter(PharmacySale.hospital_id == hospital_id)
        .order_by(PharmacySale.created_at.desc())
        .limit(8)
        .all()
    )

    return PharmacyDashboard(
        today_sales_amount=today_sales_amount,
        today_sales_count=len(sales_today),
        today_purchases_amount=round(float(purchases_today), 2),
        today_profit=today_profit,
        low_stock_count=low,
        out_of_stock_count=out,
        expired_count=expired,
        expiring_soon_count=expiring,
        pending_bills_count=int(pending_bills),
        pending_rx_count=0,
        top_selling=[{"name": n, "qty": float(q or 0), "amount": float(a or 0)} for n, q, a in top],
        latest_purchases=[
            {
                "id": str(p.id),
                "invoice_number": p.invoice_number,
                "supplier": p.supplier.company_name if p.supplier else "",
                "net_amount": float(p.net_amount or 0),
                "received_date": p.received_date.isoformat(),
            }
            for p in latest_purchases
        ],
        recent_sales=[
            {
                "id": str(s.id),
                "invoice_number": s.invoice_number,
                "customer_name": s.customer_name,
                "net_amount": float(s.net_amount or 0),
                "sale_date": s.sale_date.isoformat(),
                "status": s.status.value,
            }
            for s in recent_sales
        ],
        stock_alerts=alerts[:20],
        monthly_sales=[],
    )


# ── Categories ────────────────────────────────────────────────────────────────
@router.get("/categories", response_model=list[CategoryResponse])
def list_categories(
    active_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = db.query(MedicineCategory).filter(MedicineCategory.hospital_id == hospital_id)
    if active_only:
        q = q.filter(MedicineCategory.is_active.is_(True))
    return [_category_resp(c) for c in q.order_by(MedicineCategory.name.asc()).all()]


@router.post("/categories", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    exists = (
        db.query(MedicineCategory)
        .filter(MedicineCategory.hospital_id == hospital_id, MedicineCategory.name == payload.name.strip())
        .first()
    )
    if exists:
        raise HTTPException(status_code=400, detail="Category already exists")
    row = MedicineCategory(
        hospital_id=hospital_id,
        name=payload.name.strip(),
        description=payload.description,
        is_active=payload.is_active,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="medicine_category",
        entity_id=row.id,
        summary=f"Added medicine category {row.name}",
    )
    db.commit()
    db.refresh(row)
    return _category_resp(row)


@router.patch("/categories/{category_id}", response_model=CategoryResponse)
def update_category(
    category_id: UUID,
    payload: CategoryUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = (
        db.query(MedicineCategory)
        .filter(MedicineCategory.id == category_id, MedicineCategory.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Category not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(row, k, v.strip() if isinstance(v, str) and k == "name" else v)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="medicine_category",
        entity_id=row.id,
        summary=f"Updated medicine category {row.name}",
    )
    db.commit()
    db.refresh(row)
    return _category_resp(row)


# ── Suppliers ─────────────────────────────────────────────────────────────────
@router.get("/suppliers", response_model=list[SupplierResponse])
def list_suppliers(
    active_only: bool = Query(default=False),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = db.query(PharmacySupplier).filter(PharmacySupplier.hospital_id == hospital_id)
    if active_only:
        q = q.filter(PharmacySupplier.is_active.is_(True))
    if search and search.strip():
        s = f"%{search.strip()}%"
        q = q.filter(or_(PharmacySupplier.company_name.ilike(s), PharmacySupplier.phone.ilike(s)))
    return [_supplier_resp(r) for r in q.order_by(PharmacySupplier.company_name.asc()).all()]


@router.post("/suppliers", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
def create_supplier(
    payload: SupplierCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = PharmacySupplier(hospital_id=hospital_id, **payload.model_dump())
    row.company_name = payload.company_name.strip()
    db.add(row)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="pharmacy_supplier",
        entity_id=row.id,
        summary=f"Added supplier {row.company_name}",
    )
    db.commit()
    db.refresh(row)
    return _supplier_resp(row)


@router.patch("/suppliers/{supplier_id}", response_model=SupplierResponse)
def update_supplier(
    supplier_id: UUID,
    payload: SupplierUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = (
        db.query(PharmacySupplier)
        .filter(PharmacySupplier.id == supplier_id, PharmacySupplier.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Supplier not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v.strip() if isinstance(v, str) and k == "company_name" else v)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="pharmacy_supplier",
        entity_id=row.id,
        summary=f"Updated supplier {row.company_name}",
    )
    db.commit()
    db.refresh(row)
    return _supplier_resp(row)


# ── Medicines ─────────────────────────────────────────────────────────────────
@router.get("/medicines", response_model=list[MedicineResponse])
def list_medicines(
    active_only: bool = Query(default=False),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(Medicine)
        .options(joinedload(Medicine.category), joinedload(Medicine.inventory))
        .filter(Medicine.hospital_id == hospital_id)
    )
    if active_only:
        q = q.filter(Medicine.is_active.is_(True))
    if search and search.strip():
        s = f"%{search.strip()}%"
        q = q.filter(
            or_(
                Medicine.medicine_name.ilike(s),
                Medicine.brand_name.ilike(s),
                Medicine.generic_name.ilike(s),
                Medicine.barcode_value.ilike(s),
            )
        )
    return [_medicine_resp(m) for m in q.order_by(Medicine.medicine_name.asc()).all()]


@router.post("/medicines", response_model=MedicineResponse, status_code=status.HTTP_201_CREATED)
def create_medicine(
    payload: MedicineCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    data = payload.model_dump()
    row = Medicine(hospital_id=hospital_id, **data)
    db.add(row)
    db.flush()
    refresh_medicine_inventory(db, hospital_id, row.id)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="medicine",
        entity_id=row.id,
        summary=f"Added medicine {row.brand_name} {row.strength}",
    )
    db.commit()
    row = (
        db.query(Medicine)
        .options(joinedload(Medicine.category), joinedload(Medicine.inventory))
        .filter(Medicine.id == row.id)
        .first()
    )
    return _medicine_resp(row)


@router.get("/medicines/{medicine_id}", response_model=MedicineResponse)
def get_medicine(
    medicine_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = (
        db.query(Medicine)
        .options(joinedload(Medicine.category), joinedload(Medicine.inventory))
        .filter(Medicine.id == medicine_id, Medicine.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Medicine not found")
    return _medicine_resp(row)


@router.patch("/medicines/{medicine_id}", response_model=MedicineResponse)
def update_medicine(
    medicine_id: UUID,
    payload: MedicineUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = (
        db.query(Medicine)
        .filter(Medicine.id == medicine_id, Medicine.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Medicine not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="medicine",
        entity_id=row.id,
        summary=f"Updated medicine {row.brand_name}",
    )
    db.commit()
    row = (
        db.query(Medicine)
        .options(joinedload(Medicine.category), joinedload(Medicine.inventory))
        .filter(Medicine.id == medicine_id)
        .first()
    )
    return _medicine_resp(row)


@router.get("/medicines/{medicine_id}/batches", response_model=list[BatchResponse])
def list_batches(
    medicine_id: UUID,
    active_only: bool = Query(default=True),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(MedicineBatch)
        .options(joinedload(MedicineBatch.medicine))
        .filter(MedicineBatch.hospital_id == hospital_id, MedicineBatch.medicine_id == medicine_id)
    )
    if active_only:
        q = q.filter(MedicineBatch.is_active.is_(True))
    return [_batch_resp(b) for b in q.order_by(MedicineBatch.expiry_date.asc()).all()]


# ── Inventory / Search ────────────────────────────────────────────────────────
@router.get("/inventory", response_model=list[InventoryRow])
def list_inventory(
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = date.today()
    q = (
        db.query(Medicine, MedicineInventory)
        .outerjoin(MedicineInventory, MedicineInventory.medicine_id == Medicine.id)
        .options(joinedload(Medicine.category))
        .filter(Medicine.hospital_id == hospital_id, Medicine.is_active.is_(True))
    )
    if search and search.strip():
        s = f"%{search.strip()}%"
        q = q.filter(or_(Medicine.medicine_name.ilike(s), Medicine.brand_name.ilike(s), Medicine.generic_name.ilike(s)))
    rows: list[InventoryRow] = []
    for med, inv in q.order_by(Medicine.medicine_name.asc()).all():
        st = inventory_status(inv, med, today)
        if status_filter and status_filter != st:
            continue
        rows.append(
            InventoryRow(
                medicine_id=med.id,
                medicine_name=med.medicine_name,
                brand_name=med.brand_name,
                generic_name=med.generic_name,
                category_name=med.category.name if med.category else None,
                manufacturer=med.manufacturer,
                strength=med.strength,
                medicine_type=med.medicine_type,
                current_stock=float(inv.current_stock if inv else 0),
                reserved_stock=float(inv.reserved_stock if inv else 0),
                available_stock=float(inv.available_stock if inv else 0),
                minimum_stock=float(med.minimum_stock or 0),
                nearest_expiry=inv.nearest_expiry if inv else None,
                batch_count=int(inv.batch_count if inv else 0),
                status=st,
                selling_price=float(med.selling_price or 0),
                requires_prescription=bool(med.requires_prescription),
            )
        )
    return rows


@router.get("/search", response_model=list[SearchHit])
def search_medicines(
    q: str = Query(min_length=1),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Name / barcode / batch / QR search — scanner-ready."""
    term = q.strip()
    like = f"%{term}%"
    today = date.today()
    hits: list[SearchHit] = []

    meds = (
        db.query(Medicine)
        .options(joinedload(Medicine.inventory))
        .filter(
            Medicine.hospital_id == hospital_id,
            Medicine.is_active.is_(True),
            or_(
                Medicine.medicine_name.ilike(like),
                Medicine.brand_name.ilike(like),
                Medicine.generic_name.ilike(like),
                Medicine.barcode_value == term,
                Medicine.qr_code == term,
            ),
        )
        .limit(30)
        .all()
    )
    for m in meds:
        match = "barcode" if m.barcode_value == term else ("qr" if m.qr_code == term else "name")
        hits.append(
            SearchHit(
                medicine_id=m.id,
                medicine_name=m.medicine_name,
                brand_name=m.brand_name,
                strength=m.strength,
                available_quantity=float(m.inventory.available_stock if m.inventory else 0),
                selling_price=float(m.selling_price or 0),
                match_field=match,
            )
        )

    batches = (
        db.query(MedicineBatch)
        .options(joinedload(MedicineBatch.medicine))
        .filter(
            MedicineBatch.hospital_id == hospital_id,
            MedicineBatch.is_active.is_(True),
            MedicineBatch.available_quantity > 0,
            or_(
                MedicineBatch.batch_number.ilike(like),
                MedicineBatch.barcode_value == term,
                MedicineBatch.scanner_reference == term,
            ),
        )
        .limit(20)
        .all()
    )
    for b in batches:
        if not b.medicine:
            continue
        match = "batch"
        if b.barcode_value == term:
            match = "barcode"
        hits.append(
            SearchHit(
                medicine_id=b.medicine_id,
                medicine_name=b.medicine.medicine_name,
                brand_name=b.medicine.brand_name,
                strength=b.medicine.strength,
                batch_id=b.id,
                batch_number=b.batch_number,
                available_quantity=float(b.available_quantity or 0),
                expiry_date=b.expiry_date,
                selling_price=float(b.selling_price or b.medicine.selling_price or 0),
                match_field=match,
            )
        )
    return hits


# ── Purchases ─────────────────────────────────────────────────────────────────
@router.get("/purchases", response_model=list[PurchaseResponse])
def list_purchases(
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(PharmacyPurchase)
        .options(
            joinedload(PharmacyPurchase.supplier),
            joinedload(PharmacyPurchase.items).joinedload(PharmacyPurchaseItem.medicine),
        )
        .filter(PharmacyPurchase.hospital_id == hospital_id)
    )
    if search and search.strip():
        s = f"%{search.strip()}%"
        q = q.filter(PharmacyPurchase.invoice_number.ilike(s))
    return [_purchase_resp(p) for p in q.order_by(PharmacyPurchase.created_at.desc()).limit(200).all()]


@router.get("/purchases/{purchase_id}", response_model=PurchaseResponse)
def get_purchase(
    purchase_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    p = (
        db.query(PharmacyPurchase)
        .options(
            joinedload(PharmacyPurchase.supplier),
            joinedload(PharmacyPurchase.items).joinedload(PharmacyPurchaseItem.medicine),
        )
        .filter(PharmacyPurchase.id == purchase_id, PharmacyPurchase.hospital_id == hospital_id)
        .first()
    )
    if not p:
        raise HTTPException(status_code=404, detail="Purchase not found")
    return _purchase_resp(p)


@router.post("/purchases", response_model=PurchaseResponse, status_code=status.HTTP_201_CREATED)
def create_purchase(
    payload: PurchaseCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    supplier = (
        db.query(PharmacySupplier)
        .filter(PharmacySupplier.id == payload.supplier_id, PharmacySupplier.hospital_id == hospital_id)
        .first()
    )
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")

    dup = (
        db.query(PharmacyPurchase)
        .filter(PharmacyPurchase.hospital_id == hospital_id, PharmacyPurchase.invoice_number == payload.invoice_number)
        .first()
    )
    if dup:
        raise HTTPException(status_code=400, detail="Purchase invoice number already exists")

    actor = _actor_name(user)
    purchase = PharmacyPurchase(
        hospital_id=hospital_id,
        supplier_id=payload.supplier_id,
        invoice_number=payload.invoice_number.strip(),
        invoice_date=payload.invoice_date,
        received_date=payload.received_date,
        payment_status=payload.payment_status,
        notes=payload.notes,
        created_by_name=actor,
    )
    db.add(purchase)
    db.flush()

    subtotal = discount_total = gst_total = 0.0
    for line in payload.items:
        med = (
            db.query(Medicine)
            .filter(Medicine.id == line.medicine_id, Medicine.hospital_id == hospital_id)
            .first()
        )
        if not med:
            raise HTTPException(status_code=404, detail=f"Medicine not found: {line.medicine_id}")

        qty_total = float(line.quantity) + float(line.free_quantity or 0)
        line_gross = round(float(line.quantity) * float(line.purchase_price), 2)
        line_disc = round(line_gross * float(line.discount_percent or 0) / 100.0, 2)
        taxable = round(line_gross - line_disc, 2)
        line_gst = round(taxable * float(line.gst_percent or 0) / 100.0, 2)
        line_net = round(taxable + line_gst, 2)

        batch = (
            db.query(MedicineBatch)
            .filter(
                MedicineBatch.hospital_id == hospital_id,
                MedicineBatch.medicine_id == line.medicine_id,
                MedicineBatch.batch_number == line.batch_number.strip(),
            )
            .first()
        )
        if batch:
            batch.expiry_date = line.expiry_date
            batch.manufacture_date = line.manufacture_date
            batch.purchase_price = float(line.purchase_price)
            batch.selling_price = float(line.selling_price)
            batch.mrp = float(line.mrp)
            batch.supplier_id = payload.supplier_id
            batch.initial_quantity = float(batch.initial_quantity or 0) + qty_total
            batch.is_active = True
        else:
            batch = MedicineBatch(
                hospital_id=hospital_id,
                medicine_id=line.medicine_id,
                supplier_id=payload.supplier_id,
                batch_number=line.batch_number.strip(),
                manufacture_date=line.manufacture_date,
                expiry_date=line.expiry_date,
                purchase_price=float(line.purchase_price),
                selling_price=float(line.selling_price),
                mrp=float(line.mrp),
                initial_quantity=qty_total,
                available_quantity=0.0,
            )
            db.add(batch)
            db.flush()

        item = PharmacyPurchaseItem(
            hospital_id=hospital_id,
            purchase_id=purchase.id,
            medicine_id=line.medicine_id,
            batch_id=batch.id,
            batch_number=line.batch_number.strip(),
            manufacture_date=line.manufacture_date,
            expiry_date=line.expiry_date,
            purchase_price=float(line.purchase_price),
            mrp=float(line.mrp),
            selling_price=float(line.selling_price),
            quantity=float(line.quantity),
            free_quantity=float(line.free_quantity or 0),
            gst_percent=float(line.gst_percent or 0),
            discount_percent=float(line.discount_percent or 0),
            discount_amount=line_disc,
            gst_amount=line_gst,
            net_amount=line_net,
        )
        db.add(item)
        db.flush()

        apply_stock_change(
            db,
            hospital_id=hospital_id,
            medicine_id=line.medicine_id,
            batch_id=batch.id,
            quantity_delta=qty_total,
            transaction_type=StockTransactionType.purchase,
            reference_type="pharmacy_purchase",
            reference_id=purchase.id,
            notes=f"Purchase {purchase.invoice_number}",
            created_by_name=actor,
            allow_expired=True,
        )

        # Keep master selling/purchase hints fresh
        med.purchase_price = float(line.purchase_price)
        med.selling_price = float(line.selling_price)
        med.mrp = float(line.mrp)

        subtotal += line_gross
        discount_total += line_disc
        gst_total += line_gst

    purchase.subtotal = round(subtotal, 2)
    purchase.discount_amount = round(discount_total, 2)
    purchase.gst_amount = round(gst_total, 2)
    purchase.net_amount = round(subtotal - discount_total + gst_total, 2)

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="pharmacy_purchase",
        entity_id=purchase.id,
        summary=f"Pharmacy purchase {purchase.invoice_number} — ₹{purchase.net_amount}",
    )
    db.commit()
    p = (
        db.query(PharmacyPurchase)
        .options(
            joinedload(PharmacyPurchase.supplier),
            joinedload(PharmacyPurchase.items).joinedload(PharmacyPurchaseItem.medicine),
        )
        .filter(PharmacyPurchase.id == purchase.id)
        .first()
    )
    return _purchase_resp(p)


# ── Sales (walk-in counter) ───────────────────────────────────────────────────
@router.get("/sales", response_model=list[SaleResponse])
def list_sales(
    search: str | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(PharmacySale)
        .options(joinedload(PharmacySale.items).joinedload(PharmacySaleItem.medicine))
        .filter(PharmacySale.hospital_id == hospital_id)
    )
    if search and search.strip():
        s = f"%{search.strip()}%"
        q = q.filter(
            or_(
                PharmacySale.invoice_number.ilike(s),
                PharmacySale.customer_name.ilike(s),
                PharmacySale.customer_phone.ilike(s),
            )
        )
    if from_date:
        q = q.filter(PharmacySale.sale_date >= from_date)
    if to_date:
        q = q.filter(PharmacySale.sale_date <= to_date)
    return [_sale_resp(s) for s in q.order_by(PharmacySale.created_at.desc()).limit(200).all()]


@router.get("/sales/{sale_id}", response_model=SaleResponse)
def get_sale(
    sale_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    sale = (
        db.query(PharmacySale)
        .options(joinedload(PharmacySale.items).joinedload(PharmacySaleItem.medicine))
        .filter(PharmacySale.id == sale_id, PharmacySale.hospital_id == hospital_id)
        .first()
    )
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found")
    return _sale_resp(sale)


@router.post("/sales", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
def create_sale(
    payload: SaleCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    actor = _actor_name(user)
    today = date.today()
    sale_date = payload.sale_date or today

    if payload.patient_id:
        patient = (
            db.query(Patient)
            .filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

    invoice_number = next_doc_number(db, hospital_id, PharmacySale, "invoice_number", "PH")
    sale = PharmacySale(
        hospital_id=hospital_id,
        invoice_number=invoice_number,
        sale_type=payload.sale_type,
        status=PharmacySaleStatus.completed,
        customer_name=payload.customer_name.strip(),
        customer_phone=payload.customer_phone.strip(),
        patient_id=payload.patient_id,
        prescription_id=payload.prescription_id,
        pharmacy_rx_request_id=payload.pharmacy_rx_request_id,
        doctor_name=payload.doctor_name,
        sale_date=sale_date,
        discount_amount=float(payload.discount_amount or 0),
        payment_method=payload.payment_method or "cash",
        notes=payload.notes,
        created_by_name=actor,
    )
    db.add(sale)
    db.flush()

    warnings: list[str] = []
    subtotal = gst_total = 0.0
    item_discount_bucket = float(payload.discount_amount or 0)

    for line in payload.items:
        med = (
            db.query(Medicine)
            .filter(Medicine.id == line.medicine_id, Medicine.hospital_id == hospital_id, Medicine.is_active.is_(True))
            .first()
        )
        if not med:
            raise HTTPException(status_code=404, detail=f"Medicine not found: {line.medicine_id}")

        if line.batch_id:
            allocations = []
            batch = (
                db.query(MedicineBatch)
                .filter(
                    MedicineBatch.id == line.batch_id,
                    MedicineBatch.hospital_id == hospital_id,
                    MedicineBatch.medicine_id == line.medicine_id,
                )
                .with_for_update()
                .first()
            )
            if not batch:
                raise HTTPException(status_code=404, detail="Batch not found")
            allocations = [(batch, float(line.quantity))]
        else:
            allocations = allocate_fifo(
                db, hospital_id=hospital_id, medicine_id=line.medicine_id, quantity=float(line.quantity)
            )

        remaining_line_disc = float(line.discount_amount or 0)
        for batch, qty in allocations:
            if batch.expiry_date < today:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot sell expired batch {batch.batch_number}",
                )
            if (batch.expiry_date - today).days <= 30:
                warnings.append(
                    f"{med.medicine_name} batch {batch.batch_number} expires on {batch.expiry_date.isoformat()}"
                )

            unit_price = float(line.unit_price if line.unit_price is not None else (batch.selling_price or med.selling_price or 0))
            disc_share = remaining_line_disc if remaining_line_disc and batch is allocations[-1][0] else 0.0
            if remaining_line_disc and len(allocations) > 1 and batch is not allocations[-1][0]:
                disc_share = round(remaining_line_disc * (qty / float(line.quantity)), 2)
                remaining_line_disc = round(remaining_line_disc - disc_share, 2)
            elif remaining_line_disc:
                disc_share = remaining_line_disc
                remaining_line_disc = 0.0

            taxable, gst_amt, net = _line_amounts(qty, unit_price, float(med.gst_percent or 0), disc_share)

            apply_stock_change(
                db,
                hospital_id=hospital_id,
                medicine_id=line.medicine_id,
                batch_id=batch.id,
                quantity_delta=-qty,
                transaction_type=StockTransactionType.sale,
                reference_type="pharmacy_sale",
                reference_id=sale.id,
                notes=f"Sale {invoice_number}",
                created_by_name=actor,
            )

            item = PharmacySaleItem(
                hospital_id=hospital_id,
                sale_id=sale.id,
                medicine_id=line.medicine_id,
                batch_id=batch.id,
                batch_number=batch.batch_number,
                quantity=qty,
                unit_price=unit_price,
                purchase_price=float(batch.purchase_price or med.purchase_price or 0),
                gst_percent=float(med.gst_percent or 0),
                discount_amount=disc_share,
                gst_amount=gst_amt,
                net_amount=net,
            )
            db.add(item)
            subtotal += taxable
            gst_total += gst_amt

    # Header-level discount already applied to lines if passed on items;
    # residual header discount reduces net only when not distributed.
    sale.subtotal = round(subtotal, 2)
    sale.gst_amount = round(gst_total, 2)
    sale.discount_amount = round(item_discount_bucket, 2)
    sale.net_amount = round(subtotal + gst_total, 2)

    amount_paid = payload.amount_paid if payload.amount_paid is not None else sale.net_amount
    sale.amount_paid = round(float(amount_paid), 2)
    if sale.amount_paid >= sale.net_amount - 0.01:
        sale.payment_status = PharmacyPaymentStatus.paid
        sale.amount_paid = sale.net_amount
    elif sale.amount_paid > 0:
        sale.payment_status = PharmacyPaymentStatus.partial
    else:
        sale.payment_status = PharmacyPaymentStatus.unpaid

    # Patient-linked sales flow into hospital billing ledger
    if payload.patient_id and sale.net_amount > 0:
        from app.utils.billing import ensure_charge

        charge = ensure_charge(
            db,
            hospital_id=hospital_id,
            patient_id=payload.patient_id,
            source_type=BillingSourceType.pharmacy,
            source_id=sale.id,
            description=f"Pharmacy {invoice_number} — {sale.customer_name}"[:512],
            charge_amount=sale.net_amount,
            created_by_name=actor,
        )
        sale.billing_charge_id = charge.id

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="pharmacy_sale",
        entity_id=sale.id,
        summary=f"Pharmacy sale {invoice_number} — {sale.customer_name} ₹{sale.net_amount}",
    )
    db.commit()
    sale = (
        db.query(PharmacySale)
        .options(joinedload(PharmacySale.items).joinedload(PharmacySaleItem.medicine))
        .filter(PharmacySale.id == sale.id)
        .first()
    )
    return _sale_resp(sale, warnings)


@router.post("/sales/{sale_id}/cancel", response_model=SaleResponse)
def cancel_sale(
    sale_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    sale = (
        db.query(PharmacySale)
        .options(joinedload(PharmacySale.items))
        .filter(PharmacySale.id == sale_id, PharmacySale.hospital_id == hospital_id)
        .first()
    )
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found")
    if sale.status == PharmacySaleStatus.cancelled:
        raise HTTPException(status_code=400, detail="Sale already cancelled")

    actor = _actor_name(user)
    for it in sale.items or []:
        refundable = float(it.quantity or 0) - float(it.returned_quantity or 0)
        if refundable <= 0:
            continue
        apply_stock_change(
            db,
            hospital_id=hospital_id,
            medicine_id=it.medicine_id,
            batch_id=it.batch_id,
            quantity_delta=refundable,
            transaction_type=StockTransactionType.return_in,
            reference_type="pharmacy_sale_cancel",
            reference_id=sale.id,
            notes=f"Cancel {sale.invoice_number}",
            created_by_name=actor,
            allow_expired=True,
        )
        it.returned_quantity = float(it.quantity or 0)

    sale.status = PharmacySaleStatus.cancelled
    if sale.billing_charge_id or sale.patient_id:
        from app.utils.billing import cancel_charge_for_source

        cancel_charge_for_source(db, hospital_id, BillingSourceType.pharmacy, sale.id)

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="cancel",
        entity_type="pharmacy_sale",
        entity_id=sale.id,
        summary=f"Cancelled pharmacy sale {sale.invoice_number}",
    )
    db.commit()
    sale = (
        db.query(PharmacySale)
        .options(joinedload(PharmacySale.items).joinedload(PharmacySaleItem.medicine))
        .filter(PharmacySale.id == sale_id)
        .first()
    )
    return _sale_resp(sale)


# ── Adjustments / Returns ─────────────────────────────────────────────────────
@router.get("/adjustments", response_model=list[AdjustmentResponse])
def list_adjustments(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    rows = (
        db.query(StockAdjustment)
        .options(joinedload(StockAdjustment.medicine), joinedload(StockAdjustment.batch))
        .filter(StockAdjustment.hospital_id == hospital_id)
        .order_by(StockAdjustment.created_at.desc())
        .limit(100)
        .all()
    )
    out: list[AdjustmentResponse] = []
    for r in rows:
        item = AdjustmentResponse.model_validate(r)
        item.medicine_name = r.medicine.medicine_name if r.medicine else None
        item.batch_number = r.batch.batch_number if r.batch else None
        out.append(item)
    return out


@router.post("/adjustments", response_model=AdjustmentResponse, status_code=status.HTTP_201_CREATED)
def create_adjustment(
    payload: AdjustmentCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    actor = _actor_name(user)
    txn_map = {
        StockAdjustmentType.damage: StockTransactionType.damage,
        StockAdjustmentType.expired: StockTransactionType.expired,
        StockAdjustmentType.lost: StockTransactionType.adjustment,
        StockAdjustmentType.manual_correction: StockTransactionType.manual_correction,
    }
    apply_stock_change(
        db,
        hospital_id=hospital_id,
        medicine_id=payload.medicine_id,
        batch_id=payload.batch_id,
        quantity_delta=-float(payload.quantity),
        transaction_type=txn_map[payload.adjustment_type],
        reference_type="stock_adjustment",
        notes=payload.reason,
        created_by_name=actor,
        allow_expired=True,
    )
    row = StockAdjustment(
        hospital_id=hospital_id,
        medicine_id=payload.medicine_id,
        batch_id=payload.batch_id,
        adjustment_type=payload.adjustment_type,
        quantity=float(payload.quantity),
        reason=payload.reason,
        approved_by_name=actor,
        created_by_name=actor,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="stock_adjustment",
        entity_id=row.id,
        summary=f"Stock adjustment {payload.adjustment_type.value} qty {payload.quantity}",
    )
    db.commit()
    row = (
        db.query(StockAdjustment)
        .options(joinedload(StockAdjustment.medicine), joinedload(StockAdjustment.batch))
        .filter(StockAdjustment.id == row.id)
        .first()
    )
    resp = AdjustmentResponse.model_validate(row)
    resp.medicine_name = row.medicine.medicine_name if row.medicine else None
    resp.batch_number = row.batch.batch_number if row.batch else None
    return resp


@router.post("/returns/customer", response_model=ReturnResponse, status_code=status.HTTP_201_CREATED)
def customer_return(
    payload: CustomerReturnCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    actor = _actor_name(user)
    sale = (
        db.query(PharmacySale)
        .options(joinedload(PharmacySale.items))
        .filter(PharmacySale.id == payload.sale_id, PharmacySale.hospital_id == hospital_id)
        .first()
    )
    if not sale or sale.status == PharmacySaleStatus.cancelled:
        raise HTTPException(status_code=404, detail="Sale not found")
    item = next((i for i in sale.items if i.id == payload.sale_item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Sale item not found")
    remaining = float(item.quantity or 0) - float(item.returned_quantity or 0)
    if payload.quantity > remaining + 0.0001:
        raise HTTPException(status_code=400, detail=f"Return qty exceeds remaining ({remaining})")

    unit_net = float(item.net_amount or 0) / max(float(item.quantity or 1), 0.0001)
    refund = round(unit_net * float(payload.quantity), 2)

    apply_stock_change(
        db,
        hospital_id=hospital_id,
        medicine_id=item.medicine_id,
        batch_id=item.batch_id,
        quantity_delta=float(payload.quantity),
        transaction_type=StockTransactionType.return_in,
        reference_type="pharmacy_return",
        reference_id=sale.id,
        notes=payload.reason,
        created_by_name=actor,
        allow_expired=True,
    )
    item.returned_quantity = float(item.returned_quantity or 0) + float(payload.quantity)
    all_returned = all(
        float(i.returned_quantity or 0) + 0.0001 >= float(i.quantity or 0) for i in sale.items
    )
    sale.status = PharmacySaleStatus.returned if all_returned else PharmacySaleStatus.partially_returned

    row = PharmacyReturn(
        hospital_id=hospital_id,
        return_type="customer",
        sale_id=sale.id,
        medicine_id=item.medicine_id,
        batch_id=item.batch_id,
        quantity=float(payload.quantity),
        refund_amount=refund,
        reason=payload.reason,
        created_by_name=actor,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="pharmacy_return",
        entity_id=row.id,
        summary=f"Customer return on {sale.invoice_number} qty {payload.quantity}",
    )
    db.commit()
    db.refresh(row)
    return ReturnResponse.model_validate(row)


@router.post("/returns/purchase", response_model=ReturnResponse, status_code=status.HTTP_201_CREATED)
def purchase_return(
    payload: PurchaseReturnCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    actor = _actor_name(user)
    purchase = (
        db.query(PharmacyPurchase)
        .options(joinedload(PharmacyPurchase.items))
        .filter(PharmacyPurchase.id == payload.purchase_id, PharmacyPurchase.hospital_id == hospital_id)
        .first()
    )
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")
    item = next((i for i in purchase.items if i.id == payload.purchase_item_id), None)
    if not item or not item.batch_id:
        raise HTTPException(status_code=404, detail="Purchase item not found")

    apply_stock_change(
        db,
        hospital_id=hospital_id,
        medicine_id=item.medicine_id,
        batch_id=item.batch_id,
        quantity_delta=-float(payload.quantity),
        transaction_type=StockTransactionType.return_out,
        reference_type="pharmacy_purchase_return",
        reference_id=purchase.id,
        notes=payload.reason,
        created_by_name=actor,
        allow_expired=True,
    )
    row = PharmacyReturn(
        hospital_id=hospital_id,
        return_type="purchase",
        purchase_id=purchase.id,
        medicine_id=item.medicine_id,
        batch_id=item.batch_id,
        quantity=float(payload.quantity),
        refund_amount=0.0,
        reason=payload.reason,
        created_by_name=actor,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="pharmacy_return",
        entity_id=row.id,
        summary=f"Purchase return on {purchase.invoice_number} qty {payload.quantity}",
    )
    db.commit()
    db.refresh(row)
    return ReturnResponse.model_validate(row)


@router.get("/returns", response_model=list[ReturnResponse])
def list_returns(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    rows = (
        db.query(PharmacyReturn)
        .filter(PharmacyReturn.hospital_id == hospital_id)
        .order_by(PharmacyReturn.created_at.desc())
        .limit(100)
        .all()
    )
    return [ReturnResponse.model_validate(r) for r in rows]


# ── Reports ───────────────────────────────────────────────────────────────────
@router.get("/reports")
def pharmacy_reports(
    report: str = Query(...),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = date.today()
    if report in {"current_stock", "low_stock", "out_of_stock", "inventory_valuation"}:
        rows = list_inventory(None, None, db, _, hospital_id)
        if report == "low_stock":
            rows = [r for r in rows if r.status == "low_stock"]
        elif report == "out_of_stock":
            rows = [r for r in rows if r.status == "out_of_stock"]
        elif report == "inventory_valuation":
            return {
                "report": report,
                "rows": [
                    {
                        **r.model_dump(),
                        "valuation": round(r.current_stock * r.selling_price, 2),
                    }
                    for r in rows
                ],
            }
        return {"report": report, "rows": [r.model_dump() for r in rows]}

    if report in {"expiring_30", "expiring_60"}:
        days = 30 if report == "expiring_30" else 60
        end = date.fromordinal(today.toordinal() + days)
        batches = (
            db.query(MedicineBatch)
            .options(joinedload(MedicineBatch.medicine))
            .filter(
                MedicineBatch.hospital_id == hospital_id,
                MedicineBatch.is_active.is_(True),
                MedicineBatch.available_quantity > 0,
                MedicineBatch.expiry_date >= today,
                MedicineBatch.expiry_date <= end,
            )
            .order_by(MedicineBatch.expiry_date.asc())
            .all()
        )
        return {
            "report": report,
            "rows": [
                {
                    "medicine_name": b.medicine.medicine_name if b.medicine else "",
                    "batch_number": b.batch_number,
                    "expiry_date": b.expiry_date.isoformat(),
                    "available_quantity": float(b.available_quantity or 0),
                }
                for b in batches
            ],
        }

    if report == "sales":
        q = db.query(PharmacySale).filter(
            PharmacySale.hospital_id == hospital_id,
            PharmacySale.status != PharmacySaleStatus.cancelled,
        )
        if from_date:
            q = q.filter(PharmacySale.sale_date >= from_date)
        if to_date:
            q = q.filter(PharmacySale.sale_date <= to_date)
        sales = q.order_by(PharmacySale.sale_date.desc()).limit(500).all()
        return {
            "report": report,
            "rows": [
                {
                    "invoice_number": s.invoice_number,
                    "customer_name": s.customer_name,
                    "customer_phone": s.customer_phone,
                    "sale_date": s.sale_date.isoformat(),
                    "net_amount": float(s.net_amount or 0),
                    "payment_status": s.payment_status.value,
                }
                for s in sales
            ],
        }

    if report == "purchase":
        q = db.query(PharmacyPurchase).options(joinedload(PharmacyPurchase.supplier)).filter(
            PharmacyPurchase.hospital_id == hospital_id
        )
        if from_date:
            q = q.filter(PharmacyPurchase.received_date >= from_date)
        if to_date:
            q = q.filter(PharmacyPurchase.received_date <= to_date)
        purchases = q.order_by(PharmacyPurchase.received_date.desc()).limit(500).all()
        return {
            "report": report,
            "rows": [
                {
                    "invoice_number": p.invoice_number,
                    "supplier": p.supplier.company_name if p.supplier else "",
                    "received_date": p.received_date.isoformat(),
                    "net_amount": float(p.net_amount or 0),
                    "payment_status": p.payment_status.value,
                }
                for p in purchases
            ],
        }

    if report == "top_selling":
        q = (
            db.query(
                Medicine.medicine_name,
                func.sum(PharmacySaleItem.quantity).label("qty"),
                func.sum(PharmacySaleItem.net_amount).label("amt"),
            )
            .join(PharmacySaleItem, PharmacySaleItem.medicine_id == Medicine.id)
            .join(PharmacySale, PharmacySale.id == PharmacySaleItem.sale_id)
            .filter(PharmacySale.hospital_id == hospital_id, PharmacySale.status != PharmacySaleStatus.cancelled)
        )
        if from_date:
            q = q.filter(PharmacySale.sale_date >= from_date)
        if to_date:
            q = q.filter(PharmacySale.sale_date <= to_date)
        rows = q.group_by(Medicine.medicine_name).order_by(func.sum(PharmacySaleItem.quantity).desc()).limit(50).all()
        return {
            "report": report,
            "rows": [{"medicine_name": n, "qty": float(q or 0), "amount": float(a or 0)} for n, q, a in rows],
        }

    if report == "profit":
        items = (
            db.query(PharmacySaleItem)
            .join(PharmacySale, PharmacySale.id == PharmacySaleItem.sale_id)
            .filter(
                PharmacySale.hospital_id == hospital_id,
                PharmacySale.status != PharmacySaleStatus.cancelled,
            )
        )
        if from_date:
            items = items.filter(PharmacySale.sale_date >= from_date)
        if to_date:
            items = items.filter(PharmacySale.sale_date <= to_date)
        profit = 0.0
        revenue = 0.0
        for it in items.all():
            revenue += float(it.net_amount or 0)
            profit += (float(it.unit_price or 0) - float(it.purchase_price or 0)) * float(it.quantity or 0)
        return {"report": report, "revenue": round(revenue, 2), "profit": round(profit, 2)}

    # Fallback for remaining report keys — return inventory snapshot
    rows = list_inventory(None, None, db, _, hospital_id)
    return {"report": report, "rows": [r.model_dump() for r in rows]}
