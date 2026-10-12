"""
Pharmacy business action orchestrator.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from modules.billing.entities.billing_entities import (
    BillingChargeStatus,
    BillingPaymentMethod,
    BillingSourceType,
)
from modules.billing.services.billing_service import (
    create_payment,
    draw_down_account_deposits_for_charge,
    ensure_charge,
    recompute_charge_financials,
)
from modules.billing.services.charge_reversal_service import cancel_source_charges_or_400
from modules.pharmacy.contracts.pharmacy_contracts import (
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
    PurchaseItemResponse,
    PurchaseResponse,
    PurchaseReturnCreate,
    ReturnResponse,
    RxRequestCreate,
    RxRequestItemResponse,
    RxRequestResponse,
    SaleCreate,
    SaleItemResponse,
    SaleResponse,
    SearchHit,
    SupplierCreate,
    SupplierResponse,
    SupplierUpdate,
)
from modules.pharmacy.db.pharmacy_repository import PharmacyRepository
from modules.pharmacy.entities.pharmacy_entities import (
    Medicine,
    MedicineBatch,
    MedicineCategory,
    MedicineInventory,
    MedicineType,
    MedicineUnit,
    PharmacyPaymentStatus,
    PharmacyPurchase,
    PharmacyPurchaseItem,
    PharmacyReturn,
    PharmacyRxRequest,
    PharmacyRxRequestItem,
    PharmacyRxRequestStatus,
    PharmacySale,
    PharmacySaleItem,
    PharmacySaleStatus,
    PharmacySaleType,
    PharmacySupplier,
    PurchasePaymentStatus,
    StockAdjustment,
    StockTransaction,
    StockTransactionType,
)
from modules.pharmacy.services.pharmacy_stock_service import (
    actor_name,
    allocate_fifo,
    allocate_fifo_bulk,
    apply_stock_change,
    apply_stock_changes_bulk,
    inventory_status,
    line_amounts,
    next_doc_number,
    refresh_medicine_inventories_bulk,
    refresh_medicine_inventory,
)
from shared.audit import write_audit_log


class PharmacyActions:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = PharmacyRepository(db)

    # ── Formatting Helpers ────────────────────────────────────────────────────

    def _medicine_to_response(self, m: Medicine, inv: MedicineInventory | None = None) -> MedicineResponse:
        data = MedicineResponse.model_validate(m)
        data.category_name = m.category.name if m.category else None
        if inv:
            data.current_stock = float(inv.current_stock or 0)
            data.available_stock = float(inv.available_stock or 0)
        elif m.inventory:
            data.current_stock = float(m.inventory.current_stock or 0)
            data.available_stock = float(m.inventory.available_stock or 0)
        return data

    def _batch_to_response(self, b: MedicineBatch, today: date | None = None) -> BatchResponse:
        today = today or date.today()
        data = BatchResponse.model_validate(b)
        data.medicine_name = b.medicine.medicine_name if b.medicine else None
        data.is_expired = b.expiry_date < today
        data.expiring_soon = (not data.is_expired) and (b.expiry_date - today).days <= 30
        return data

    def _sale_to_response(self, sale: PharmacySale, warnings: list[str] | None = None) -> SaleResponse:
        items = []
        for it in sale.items or []:
            row = SaleItemResponse.model_validate(it)
            row.medicine_name = it.medicine.medicine_name if it.medicine else None
            items.append(row)
        data = SaleResponse.model_validate(sale)
        data.items = items
        data.expiry_warnings = warnings or []
        return data

    def _purchase_to_response(self, p: PharmacyPurchase) -> PurchaseResponse:
        items = []
        for it in p.items or []:
            row = PurchaseItemResponse.model_validate(it)
            row.medicine_name = it.medicine.medicine_name if it.medicine else None
            items.append(row)
        data = PurchaseResponse.model_validate(p)
        data.supplier_name = p.supplier.company_name if p.supplier else None
        data.items = items
        return data

    def _rx_request_to_response(self, rx: PharmacyRxRequest) -> RxRequestResponse:
        items = []
        for it in rx.items or []:
            items.append(RxRequestItemResponse.model_validate(it))
        data = RxRequestResponse.model_validate(rx)
        data.items = items
        return data

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def get_dashboard(self) -> PharmacyDashboard:
        today = date.today()
        sales_today = (
            self.db.query(PharmacySale)
            .filter(
                PharmacySale.hospital_id == self.hospital_id,
                PharmacySale.sale_date == today,
                PharmacySale.status != PharmacySaleStatus.cancelled,
            )
            .all()
        )
        today_sales_amount = round(sum(float(s.net_amount or 0) for s in sales_today), 2)
        today_sales_count = len(sales_today)

        purchases_today = (
            self.db.query(PharmacyPurchase)
            .filter(
                PharmacyPurchase.hospital_id == self.hospital_id,
                PharmacyPurchase.received_date == today,
            )
            .all()
        )
        today_purchases_amount = round(sum(float(p.net_amount or 0) for p in purchases_today), 2)

        sale_items = (
            self.db.query(PharmacySaleItem)
            .join(PharmacySale, PharmacySale.id == PharmacySaleItem.sale_id)
            .filter(
                PharmacySale.hospital_id == self.hospital_id,
                PharmacySale.sale_date == today,
                PharmacySale.status != PharmacySaleStatus.cancelled,
            )
            .all()
        )
        today_profit = round(
            sum(
                (float(it.unit_price or 0) - float(it.purchase_price or 0)) * float(it.quantity or 0)
                for it in sale_items
            ),
            2,
        )

        inv_rows = self.repo.list_inventory(self.hospital_id)
        low_stock_count = sum(1 for r in inv_rows if r.status == "low_stock")
        out_of_stock_count = sum(1 for r in inv_rows if r.status == "out_of_stock")
        expired_count = sum(1 for r in inv_rows if r.status == "expired")
        expiring_soon_count = sum(1 for r in inv_rows if r.status == "expiring_soon")

        pending_bills = (
            self.db.query(PharmacySale)
            .filter(
                PharmacySale.hospital_id == self.hospital_id,
                PharmacySale.payment_status != PharmacyPaymentStatus.paid,
                PharmacySale.status != PharmacySaleStatus.cancelled,
            )
            .count()
        )

        pending_rx = (
            self.db.query(PharmacyRxRequest)
            .filter(
                PharmacyRxRequest.hospital_id == self.hospital_id,
                PharmacyRxRequest.status.in_([PharmacyRxRequestStatus.pending, PharmacyRxRequestStatus.partially_dispensed]),
            )
            .count()
        )

        # Top selling items
        top_selling_rows = (
            self.db.query(
                Medicine.medicine_name,
                func.sum(PharmacySaleItem.quantity).label("total_qty"),
                func.sum(PharmacySaleItem.net_amount).label("total_amt"),
            )
            .join(PharmacySaleItem, PharmacySaleItem.medicine_id == Medicine.id)
            .join(PharmacySale, PharmacySale.id == PharmacySaleItem.sale_id)
            .filter(
                PharmacySale.hospital_id == self.hospital_id,
                PharmacySale.status != PharmacySaleStatus.cancelled,
            )
            .group_by(Medicine.medicine_name)
            .order_by(func.sum(PharmacySaleItem.quantity).desc())
            .limit(5)
            .all()
        )
        top_selling = [
            {"medicine_name": name, "quantity": float(qty or 0), "amount": round(float(amt or 0), 2)}
            for name, qty, amt in top_selling_rows
        ]

        latest_purchases_rows = (
            self.db.query(PharmacyPurchase)
            .options(joinedload(PharmacyPurchase.supplier))
            .filter(PharmacyPurchase.hospital_id == self.hospital_id)
            .order_by(PharmacyPurchase.created_at.desc())
            .limit(5)
            .all()
        )
        latest_purchases = [
            {
                "id": str(p.id),
                "invoice_number": p.invoice_number,
                "supplier_name": p.supplier.company_name if p.supplier else "",
                "net_amount": p.net_amount,
                "received_date": p.received_date.isoformat(),
            }
            for p in latest_purchases_rows
        ]

        recent_sales_rows = (
            self.db.query(PharmacySale)
            .filter(PharmacySale.hospital_id == self.hospital_id)
            .order_by(PharmacySale.created_at.desc())
            .limit(5)
            .all()
        )
        recent_sales = [
            {
                "id": str(s.id),
                "invoice_number": s.invoice_number,
                "customer_name": s.customer_name,
                "net_amount": s.net_amount,
                "sale_date": s.sale_date.isoformat(),
                "status": s.status.value,
            }
            for s in recent_sales_rows
        ]

        stock_alerts = [
            {
                "medicine_name": r.medicine_name,
                "status": r.status,
                "current_stock": r.current_stock,
                "minimum_stock": r.minimum_stock,
                "nearest_expiry": r.nearest_expiry.isoformat() if r.nearest_expiry else None,
            }
            for r in inv_rows
            if r.status in ("low_stock", "out_of_stock", "expired", "expiring_soon")
        ][:10]

        return PharmacyDashboard(
            today_sales_amount=today_sales_amount,
            today_sales_count=today_sales_count,
            today_purchases_amount=today_purchases_amount,
            today_profit=today_profit,
            low_stock_count=low_stock_count,
            out_of_stock_count=out_of_stock_count,
            expired_count=expired_count,
            expiring_soon_count=expiring_soon_count,
            pending_bills_count=pending_bills,
            pending_rx_count=pending_rx,
            top_selling=top_selling,
            latest_purchases=latest_purchases,
            recent_sales=recent_sales,
            stock_alerts=stock_alerts,
            monthly_sales=[],
        )

    # ── Categories ────────────────────────────────────────────────────────────

    def list_categories(self, active_only: bool | None = None) -> list[CategoryResponse]:
        rows = self.repo.list_categories(self.hospital_id, active_only)
        return [CategoryResponse.model_validate(r) for r in rows]

    def create_category(self, payload: CategoryCreate) -> CategoryResponse:
        name = payload.name.strip()
        existing = self.repo.get_category_by_name(self.hospital_id, name)
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Category already exists")

        row = MedicineCategory(
            hospital_id=self.hospital_id,
            name=name,
            description=payload.description.strip() if payload.description else None,
            is_active=payload.is_active,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="medicine_category",
            entity_id=row.id,
            summary=f"Added medicine category {name}",
        )
        self.db.commit()
        self.db.refresh(row)
        return CategoryResponse.model_validate(row)

    def update_category(self, category_id: UUID, payload: CategoryUpdate) -> CategoryResponse:
        row = self.repo.get_category_by_id(category_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

        data = payload.model_dump(exclude_unset=True)
        if "name" in data and data["name"]:
            data["name"] = data["name"].strip()
            dup = self.repo.get_category_by_name(self.hospital_id, data["name"])
            if dup and dup.id != category_id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Category name already in use")

        for k, v in data.items():
            setattr(row, k, v)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="medicine_category",
            entity_id=row.id,
            summary=f"Updated medicine category {row.name}",
        )
        self.db.commit()
        self.db.refresh(row)
        return CategoryResponse.model_validate(row)

    # ── Suppliers ─────────────────────────────────────────────────────────────

    def list_suppliers(self, active_only: bool | None = None) -> list[SupplierResponse]:
        rows = self.repo.list_suppliers(self.hospital_id, active_only)
        return [SupplierResponse.model_validate(r) for r in rows]

    def create_supplier(self, payload: SupplierCreate) -> SupplierResponse:
        name = payload.company_name.strip()
        existing = self.repo.get_supplier_by_company_name(self.hospital_id, name)
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Supplier already exists")

        row = PharmacySupplier(
            hospital_id=self.hospital_id,
            company_name=name,
            gstin=payload.gstin.strip() if payload.gstin else None,
            address=payload.address.strip() if payload.address else None,
            city=payload.city.strip() if payload.city else None,
            state=payload.state.strip() if payload.state else None,
            phone=payload.phone.strip() if payload.phone else None,
            email=payload.email.strip() if payload.email else None,
            contact_person=payload.contact_person.strip() if payload.contact_person else None,
            payment_terms=payload.payment_terms.strip() if payload.payment_terms else None,
            is_active=payload.is_active,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="pharmacy_supplier",
            entity_id=row.id,
            summary=f"Added supplier {name}",
        )
        self.db.commit()
        self.db.refresh(row)
        return SupplierResponse.model_validate(row)

    def update_supplier(self, supplier_id: UUID, payload: SupplierUpdate) -> SupplierResponse:
        row = self.repo.get_supplier_by_id(supplier_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

        data = payload.model_dump(exclude_unset=True)
        if "company_name" in data and data["company_name"]:
            data["company_name"] = data["company_name"].strip()
            dup = self.repo.get_supplier_by_company_name(self.hospital_id, data["company_name"])
            if dup and dup.id != supplier_id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Supplier name already in use")

        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip() or None
            setattr(row, k, v)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="pharmacy_supplier",
            entity_id=row.id,
            summary=f"Updated supplier {row.company_name}",
        )
        self.db.commit()
        self.db.refresh(row)
        return SupplierResponse.model_validate(row)

    # ── Medicines ─────────────────────────────────────────────────────────────

    def list_medicines(
        self,
        search: str | None = None,
        category_id: UUID | None = None,
        active_only: bool | None = None,
        limit: int = 300,
        offset: int = 0,
    ) -> list[MedicineResponse]:
        rows = self.repo.list_medicines(
            self.hospital_id, search, category_id, active_only, limit=limit, offset=offset
        )
        return [self._medicine_to_response(r) for r in rows]

    def create_medicine(self, payload: MedicineCreate) -> MedicineResponse:
        dup = self.repo.find_duplicate_medicine(
            self.hospital_id,
            payload.brand_name.strip(),
            payload.strength.strip(),
            payload.manufacturer.strip(),
        )
        if dup:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Medicine with same brand, strength and manufacturer already exists",
            )

        if payload.category_id:
            cat = self.repo.get_category_by_id(payload.category_id, self.hospital_id)
            if not cat:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

        med = Medicine(
            hospital_id=self.hospital_id,
            medicine_name=payload.medicine_name.strip(),
            generic_name=payload.generic_name.strip() if payload.generic_name else None,
            brand_name=payload.brand_name.strip(),
            manufacturer=payload.manufacturer.strip(),
            category_id=payload.category_id,
            medicine_type=payload.medicine_type,
            strength=payload.strength.strip(),
            unit=payload.unit,
            hsn_code=payload.hsn_code.strip() if payload.hsn_code else None,
            gst_percent=payload.gst_percent,
            selling_price=payload.selling_price,
            purchase_price=payload.purchase_price,
            mrp=payload.mrp,
            minimum_stock=payload.minimum_stock,
            maximum_stock=payload.maximum_stock,
            reorder_level=payload.reorder_level,
            storage_condition=payload.storage_condition.strip() if payload.storage_condition else None,
            is_schedule_drug=payload.is_schedule_drug,
            requires_prescription=payload.requires_prescription,
            barcode_value=payload.barcode_value.strip() if payload.barcode_value else None,
            qr_code=payload.qr_code.strip() if payload.qr_code else None,
            description=payload.description.strip() if payload.description else None,
            is_active=payload.is_active,
        )
        self.db.add(med)
        self.db.flush()

        inv = MedicineInventory(
            hospital_id=self.hospital_id,
            medicine_id=med.id,
            current_stock=0.0,
            reserved_stock=0.0,
            available_stock=0.0,
            batch_count=0,
        )
        self.db.add(inv)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="medicine",
            entity_id=med.id,
            summary=f"Added medicine {med.medicine_name}",
        )
        self.db.commit()
        created = self.repo.get_medicine_by_id(med.id, self.hospital_id)
        assert created is not None
        return self._medicine_to_response(created)

    def get_medicine(self, medicine_id: UUID) -> MedicineResponse:
        row = self.repo.get_medicine_by_id(medicine_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")
        return self._medicine_to_response(row)

    def update_medicine(self, medicine_id: UUID, payload: MedicineUpdate) -> MedicineResponse:
        row = self.repo.get_medicine_by_id(medicine_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")

        data = payload.model_dump(exclude_unset=True)
        brand = data.get("brand_name", row.brand_name)
        strength = data.get("strength", row.strength)
        manufacturer = data.get("manufacturer", row.manufacturer)
        dup = self.repo.find_duplicate_medicine(
            self.hospital_id, brand, strength, manufacturer, exclude_id=medicine_id
        )
        if dup:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Another medicine with same brand, strength and manufacturer exists",
            )

        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip() or None
            setattr(row, k, v)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="medicine",
            entity_id=row.id,
            summary=f"Updated medicine {row.medicine_name}",
        )
        self.db.commit()
        refreshed = self.repo.get_medicine_by_id(medicine_id, self.hospital_id)
        assert refreshed is not None
        return self._medicine_to_response(refreshed)

    def list_batches(self, medicine_id: UUID) -> list[BatchResponse]:
        med = self.repo.get_medicine_by_id(medicine_id, self.hospital_id)
        if not med:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")
        rows = self.repo.list_batches_for_medicine(self.hospital_id, medicine_id)
        return [self._batch_to_response(r) for r in rows]

    # ── Inventory & Search ────────────────────────────────────────────────────

    def list_inventory(
        self,
        search: str | None = None,
        status_filter: str | None = None,
        category_id: UUID | None = None,
    ) -> list[InventoryRow]:
        return self.repo.list_inventory(self.hospital_id, search, status_filter, category_id)

    def search(self, q: str, limit: int = 25) -> list[SearchHit]:
        if not q or not q.strip():
            return []
        return self.repo.search_hits(self.hospital_id, q.strip(), limit)

    # ── Purchases ─────────────────────────────────────────────────────────────

    def list_purchases(
        self,
        supplier_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 100,
    ) -> list[PurchaseResponse]:
        rows = self.repo.list_purchases(self.hospital_id, supplier_id, from_date, to_date, limit)
        return [self._purchase_to_response(r) for r in rows]

    def get_purchase(self, purchase_id: UUID) -> PurchaseResponse:
        row = self.repo.get_purchase_by_id(purchase_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")
        return self._purchase_to_response(row)

    def create_purchase(self, payload: PurchaseCreate) -> PurchaseResponse:
        supplier = self.repo.get_supplier_by_id(payload.supplier_id, self.hospital_id)
        if not supplier:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

        inv_num = payload.invoice_number.strip()
        existing = self.repo.get_purchase_by_invoice(self.hospital_id, inv_num)
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invoice number already exists")

        total_subtotal = 0.0
        total_discount = 0.0
        total_gst = 0.0
        total_net = 0.0

        medicine_ids = list({it.medicine_id for it in payload.items})
        meds = (
            self.db.query(Medicine)
            .filter(Medicine.hospital_id == self.hospital_id, Medicine.id.in_(medicine_ids))
            .all()
        )
        med_by_id = {m.id: m for m in meds}

        line_computations = []
        for it in payload.items:
            med = med_by_id.get(it.medicine_id)
            if not med:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Medicine {it.medicine_id} not found")

            gross = round(it.quantity * it.purchase_price, 2)
            disc = round(gross * float(it.discount_percent or 0) / 100.0, 2)
            taxable = max(0.0, gross - disc)
            gst = round(taxable * float(it.gst_percent or 0) / 100.0, 2)
            net = round(taxable + gst, 2)

            total_subtotal += gross
            total_discount += disc
            total_gst += gst
            total_net += net

            line_computations.append((it, med, gross, disc, gst, net))

        purchase = PharmacyPurchase(
            id=uuid4(),
            hospital_id=self.hospital_id,
            supplier_id=payload.supplier_id,
            invoice_number=inv_num,
            invoice_date=payload.invoice_date,
            received_date=payload.received_date,
            payment_status=payload.payment_status,
            subtotal=round(total_subtotal, 2),
            discount_amount=round(total_discount, 2),
            gst_amount=round(total_gst, 2),
            net_amount=round(total_net, 2),
            notes=payload.notes.strip() if payload.notes else None,
            created_by_name=actor_name(self.user),
        )
        self.db.add(purchase)

        # Bulk pre-fetch existing batches
        batch_numbers = list({it.batch_number.strip() for it in payload.items})
        existing_batches = (
            self.db.query(MedicineBatch)
            .filter(
                MedicineBatch.hospital_id == self.hospital_id,
                MedicineBatch.medicine_id.in_(medicine_ids),
                MedicineBatch.batch_number.in_(batch_numbers),
            )
            .with_for_update()
            .all()
        )
        batch_map: dict[tuple[UUID, str], MedicineBatch] = {
            (b.medicine_id, b.batch_number): b for b in existing_batches
        }

        # Helper to instantiate a clean MedicineBatch model from canonical spec
        def _build_batch_entity(spec: dict[str, Any]) -> MedicineBatch:
            return MedicineBatch(
                hospital_id=self.hospital_id,
                medicine_id=spec["medicine_id"],
                supplier_id=spec["supplier_id"],
                batch_number=spec["batch_number"],
                manufacture_date=spec["manufacture_date"],
                expiry_date=spec["expiry_date"],
                purchase_price=spec["purchase_price"],
                selling_price=spec["selling_price"],
                mrp=spec["mrp"],
                initial_quantity=spec["total_incoming"],
                available_quantity=0.0,
                is_active=True,
            )

        # Deduplicate requested unique batch keys across purchase lines
        missing_batch_specs: dict[tuple[UUID, str], dict[str, Any]] = {}
        for it, med, gross, disc, gst, net in line_computations:
            batch_num = it.batch_number.strip()
            key = (it.medicine_id, batch_num)
            total_incoming = it.quantity + it.free_quantity

            if key in batch_map:
                continue

            if key in missing_batch_specs:
                spec = missing_batch_specs[key]
                # Validate immutable metadata compatibility
                if spec["expiry_date"] != it.expiry_date:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Incompatible metadata for batch '{batch_num}': conflicting expiry dates within same purchase",
                    )
                if it.manufacture_date and spec["manufacture_date"] and spec["manufacture_date"] != it.manufacture_date:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Incompatible metadata for batch '{batch_num}': conflicting manufacture dates within same purchase",
                    )
                spec["total_incoming"] = round(spec["total_incoming"] + total_incoming, 2)
            else:
                missing_batch_specs[key] = {
                    "medicine_id": it.medicine_id,
                    "supplier_id": payload.supplier_id,
                    "batch_number": batch_num,
                    "manufacture_date": it.manufacture_date,
                    "expiry_date": it.expiry_date,
                    "purchase_price": it.purchase_price,
                    "selling_price": it.selling_price,
                    "mrp": it.mrp,
                    "total_incoming": total_incoming,
                }

        # Create missing unique batches safely
        if missing_batch_specs:
            # Fast path: attempt bulk insert within a savepoint (O(1) queries when no collision)
            staged_batches = {k: _build_batch_entity(spec) for k, spec in missing_batch_specs.items()}
            try:
                with self.db.begin_nested():
                    for b in staged_batches.values():
                        self.db.add(b)
                    self.db.flush()
                batch_map.update(staged_batches)
            except IntegrityError:
                # Collision race detected: outer savepoint rolled back cleanly.
                # Handle each missing unique key independently to guarantee that
                # non-conflicting batches are recreated and never disappear.
                for key, spec in missing_batch_specs.items():
                    # 1. Check if a concurrent transaction won the insert for this key
                    winner = (
                        self.db.query(MedicineBatch)
                        .filter(
                            MedicineBatch.hospital_id == self.hospital_id,
                            MedicineBatch.medicine_id == spec["medicine_id"],
                            MedicineBatch.batch_number == spec["batch_number"],
                        )
                        .with_for_update()
                        .first()
                    )
                    if winner:
                        batch_map[key] = winner
                    else:
                        # 2. Key did NOT collide: instantiate fresh model and insert in its own savepoint
                        try:
                            with self.db.begin_nested():
                                fresh_b = _build_batch_entity(spec)
                                self.db.add(fresh_b)
                                self.db.flush()
                            batch_map[key] = fresh_b
                        except IntegrityError:
                            # Concurrent transaction inserted in the interim: lock winner
                            w = (
                                self.db.query(MedicineBatch)
                                .filter(
                                    MedicineBatch.hospital_id == self.hospital_id,
                                    MedicineBatch.medicine_id == spec["medicine_id"],
                                    MedicineBatch.batch_number == spec["batch_number"],
                                )
                                .with_for_update()
                                .one()
                            )
                            batch_map[key] = w

        # Strict invariant verification: every requested key must exist in batch_map
        for it in payload.items:
            req_key = (it.medicine_id, it.batch_number.strip())
            assert req_key in batch_map, f"Invariant violation: batch key {req_key} missing from batch_map"

        actor = actor_name(self.user)
        for it, med, gross, disc, gst, net in line_computations:
            batch_num = it.batch_number.strip()
            batch = batch_map[(it.medicine_id, batch_num)]
            total_incoming = it.quantity + it.free_quantity

            p_item = PharmacyPurchaseItem(
                hospital_id=self.hospital_id,
                purchase_id=purchase.id,
                medicine_id=it.medicine_id,
                batch_id=batch.id,
                batch_number=batch_num,
                manufacture_date=it.manufacture_date,
                expiry_date=it.expiry_date,
                purchase_price=it.purchase_price,
                mrp=it.mrp,
                selling_price=it.selling_price,
                quantity=it.quantity,
                free_quantity=it.free_quantity,
                gst_percent=it.gst_percent,
                discount_percent=it.discount_percent,
                discount_amount=disc,
                gst_amount=gst,
                net_amount=net,
            )
            self.db.add(p_item)

            before = float(batch.available_quantity or 0)
            after = round(before + float(total_incoming), 2)
            batch.available_quantity = after

            txn = StockTransaction(
                hospital_id=self.hospital_id,
                medicine_id=it.medicine_id,
                batch_id=batch.id,
                transaction_type=StockTransactionType.purchase,
                quantity=round(float(total_incoming), 2),
                quantity_before=before,
                quantity_after=after,
                reference_type="pharmacy_purchase",
                reference_id=purchase.id,
                notes=f"Purchase {inv_num}",
                created_by_name=actor,
            )
            self.db.add(txn)

        self.db.flush()
        refresh_medicine_inventories_bulk(self.db, self.hospital_id, medicine_ids)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="pharmacy_purchase",
            entity_id=purchase.id,
            summary=f"Recorded purchase invoice {inv_num} (₹{purchase.net_amount})",
        )
        self.db.commit()
        created = self.repo.get_purchase_by_id(purchase.id, self.hospital_id)
        assert created is not None
        return self._purchase_to_response(created)

    # ── Sales ─────────────────────────────────────────────────────────────────

    def list_sales(
        self,
        search: str | None = None,
        sale_type: PharmacySaleType | None = None,
        payment_status: PharmacyPaymentStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        patient_id: UUID | None = None,
        limit: int = 100,
    ) -> list[SaleResponse]:
        rows = self.repo.list_sales(
            self.hospital_id,
            search=search,
            sale_type=sale_type,
            payment_status=payment_status,
            from_date=from_date,
            to_date=to_date,
            patient_id=patient_id,
            limit=limit,
        )
        return [self._sale_to_response(r) for r in rows]

    def get_sale(self, sale_id: UUID) -> SaleResponse:
        row = self.repo.get_sale_by_id(sale_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found")
        return self._sale_to_response(row)

    def create_sale(self, payload: SaleCreate) -> SaleResponse:
        try:
            return self._create_sale_internal(payload)
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _apply_sale_payment_status(sale: PharmacySale, paid: float, net: float) -> None:
        if paid >= net - 0.0001:
            sale.payment_status = PharmacyPaymentStatus.paid
        elif paid > 0:
            sale.payment_status = PharmacyPaymentStatus.partial
        else:
            sale.payment_status = PharmacyPaymentStatus.unpaid

    def _authorize_walk_in_override(
        self, sale: PharmacySale, payload: SaleCreate, paid: float, net: float
    ) -> None:
        """Emergency dispensing to an unregistered walk-in.

        Authorized through the canonical helper like every other override. There is no
        patient record to hang a FinancialClearanceException on, so the override is
        persisted as an attributable audit entry on the sale.
        """
        from modules.billing.services.financial_authorization import (
            OverrideScope,
            authorize_emergency_override,
        )

        scope = OverrideScope.service(BillingSourceType.pharmacy.value, sale.id, action_type="dispense")
        override_actor = authorize_emergency_override(
            self.db,
            self.hospital_id,
            self.user,
            scope=scope,
            reason=getattr(payload, "emergency_override_reason", None),
        )
        reason = (payload.emergency_override_reason or "").strip()
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=override_actor.claims,
            action="emergency_override",
            entity_type="pharmacy_sale",
            entity_id=sale.id,
            summary=f"Emergency walk-in dispensing override for sale {sale.invoice_number} by {override_actor.name}: {reason}",
            details={
                "sale_id": str(sale.id),
                "patient_id": None,
                "service_type": BillingSourceType.pharmacy.value,
                "source_type": BillingSourceType.pharmacy.value,
                "source_id": str(sale.id),
                "customer_name": sale.customer_name,
                "net_amount": round(net, 2),
                "amount_paid": round(paid, 2),
                "shortfall_amount": round(max(0.0, net - paid), 2),
                "reason": reason,
                **override_actor.audit_fields(),
            },
        )

    def _create_sale_internal(self, payload: SaleCreate) -> SaleResponse:
        actor = actor_name(self.user)
        today = payload.sale_date or date.today()

        rx_obj = None
        if payload.prescription_id:
            from modules.clinical_records.entities.clinical_record import Prescription
            rx_obj = (
                self.db.query(Prescription)
                .filter(Prescription.id == payload.prescription_id, Prescription.hospital_id == self.hospital_id)
                .with_for_update()
                .first()
            )
            if rx_obj and getattr(rx_obj, "status", None) == "draft":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot dispense medication for a draft prescription. Prescription must be issued.",
                )
            if rx_obj and getattr(rx_obj, "status", None) in ("dispensed", "fully_dispensed"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Prescription has already been fully dispensed.",
                )

        # Load the pharmacy Rx request (per-prescription fulfillment record) if dispensing against one.
        rx_request = None
        rx_item_by_medicine: dict[UUID, PharmacyRxRequestItem] = {}
        rx_dispense_map: dict[UUID, float] = {}
        if payload.pharmacy_rx_request_id:
            rx_request = self.repo.get_rx_request_by_id(
                payload.pharmacy_rx_request_id, self.hospital_id, for_update=True
            )
            if not rx_request:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Pharmacy Rx request not found"
                )
            if rx_request.status == PharmacyRxRequestStatus.cancelled:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Pharmacy Rx request has been cancelled and cannot be dispensed.",
                )
            for rit in rx_request.items or []:
                if rit.medicine_id:
                    rx_item_by_medicine[rit.medicine_id] = rit
            # Block if every item is already fully dispensed.
            if all(
                float(rit.quantity or 0) - float(rit.dispensed_quantity or 0) <= 0.0001
                for rit in rx_request.items or []
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Pharmacy Rx request is already fully dispensed.",
                )
            # Validate requested sale quantities against remaining per-item quantity.
            for it in payload.items:
                rit = rx_item_by_medicine.get(it.medicine_id)
                if rit is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Medicine {it.medicine_id} is not part of the linked pharmacy Rx request "
                            f"{payload.pharmacy_rx_request_id}."
                        ),
                    )
                remaining = float(rit.quantity or 0) - float(rit.dispensed_quantity or 0)
                if it.quantity > remaining + 0.0001:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Cannot dispense {it.quantity} of '{rit.medicine_name}'; only {remaining} "
                            f"remaining on the Rx request."
                        ),
                    )

        invoice_number = next_doc_number(self.db, self.hospital_id, PharmacySale, "invoice_number", "SAL")

        sale_id = uuid4()
        sale_admission_id = payload.admission_id or (rx_request.admission_id if rx_request else None) or (rx_obj.admission_id if rx_obj else None)

        total_subtotal = 0.0
        total_gst = 0.0
        total_net = 0.0
        warnings: list[str] = []
        stock_decrements: list[tuple[UUID, UUID, float]] = []

        med_ids = list(dict.fromkeys(it.medicine_id for it in payload.items))
        meds = (
            self.db.query(Medicine)
            .filter(Medicine.id.in_(med_ids), Medicine.hospital_id == self.hospital_id)
            .all()
        )
        med_by_id = {m.id: m for m in meds}

        batch_ids = [it.batch_id for it in payload.items if it.batch_id]
        batch_by_id: dict[UUID, MedicineBatch] = {}
        if batch_ids:
            batches = (
                self.db.query(MedicineBatch)
                .filter(
                    MedicineBatch.id.in_(list(dict.fromkeys(batch_ids))),
                    MedicineBatch.hospital_id == self.hospital_id,
                )
                .with_for_update()
                .all()
            )
            batch_by_id = {b.id: b for b in batches}

        fifo_item_indices = [idx for idx, it in enumerate(payload.items) if not it.batch_id]
        fifo_requests = [(payload.items[idx].medicine_id, payload.items[idx].quantity) for idx in fifo_item_indices]
        fifo_alloc_map: dict[int, list[tuple[MedicineBatch, float]]] = {}
        if fifo_requests:
            alloc_results = allocate_fifo_bulk(
                self.db,
                hospital_id=self.hospital_id,
                requests=fifo_requests,
                medicine_names={m.id: m.medicine_name for m in meds},
            )
            for idx, alloc in zip(fifo_item_indices, alloc_results):
                fifo_alloc_map[idx] = alloc
                for b, _ in alloc:
                    batch_by_id[b.id] = b

        sale_items: list[PharmacySaleItem] = []
        for item_idx, it in enumerate(payload.items):
            med = med_by_id.get(it.medicine_id)
            if not med:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Medicine {it.medicine_id} not found")

            allocations: list[tuple[MedicineBatch, float]] = []
            if it.batch_id:
                batch = batch_by_id.get(it.batch_id)
                if not batch or batch.medicine_id != it.medicine_id:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
                if batch.available_quantity < it.quantity:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Insufficient stock on batch {batch.batch_number}",
                    )
                if batch.expiry_date < today:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Batch {batch.batch_number} is expired",
                    )
                allocations = [(batch, it.quantity)]
            else:
                allocations = fifo_alloc_map.get(item_idx, [])
                if not allocations:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Insufficient non-expired stock for {med.medicine_name}",
                    )

            for b, qty in allocations:
                unit_pr = float(it.unit_price if it.unit_price is not None else b.selling_price or med.selling_price)
                taxable, gst, net = line_amounts(qty, unit_pr, med.gst_percent, it.discount_amount)
                total_subtotal += taxable
                total_gst += gst
                total_net += net

                if (b.expiry_date - today).days <= 30:
                    warnings.append(f"{med.medicine_name} batch {b.batch_number} expires in {(b.expiry_date - today).days} days")

                sale_item = PharmacySaleItem(
                    id=uuid4(),
                    hospital_id=self.hospital_id,
                    sale_id=sale_id,
                    medicine_id=it.medicine_id,
                    batch_id=b.id,
                    batch_number=b.batch_number,
                    quantity=qty,
                    unit_price=unit_pr,
                    purchase_price=b.purchase_price,
                    gst_percent=med.gst_percent,
                    discount_amount=it.discount_amount,
                    gst_amount=gst,
                    net_amount=net,
                )
                sale_items.append(sale_item)
                stock_decrements.append((it.medicine_id, b.id, qty))

            if rx_request:
                rx_dispense_map[it.medicine_id] = rx_dispense_map.get(it.medicine_id, 0.0) + float(it.quantity)

        # Apply dispensed quantity tracking against the linked pharmacy Rx request (FLAW-004)
        if rx_request:
            for medicine_id, qty in rx_dispense_map.items():
                rit = rx_item_by_medicine.get(medicine_id)
                if rit is not None:
                    rit.dispensed_quantity = float(rit.dispensed_quantity or 0) + qty
                    self._sync_item_status(rit)
            new_status = self._recompute_rx_status(rx_request)
            rx_request.status = new_status
            if rx_obj:
                if new_status == PharmacyRxRequestStatus.completed:
                    rx_obj.status = "fully_dispensed"
                elif new_status == PharmacyRxRequestStatus.partially_dispensed:
                    rx_obj.status = "partially_dispensed"
                else:
                    rx_obj.status = "issued"

        final_net = max(0.0, round(total_net - payload.discount_amount, 2))
        tendered = round(float(payload.amount_paid or 0), 2)
        paid = min(tendered, final_net)

        sale = PharmacySale(
            id=sale_id,
            hospital_id=self.hospital_id,
            invoice_number=invoice_number,
            sale_type=payload.sale_type,
            status=PharmacySaleStatus.completed,
            customer_name=payload.customer_name.strip(),
            customer_phone=payload.customer_phone.strip(),
            patient_id=payload.patient_id,
            admission_id=sale_admission_id,
            prescription_id=payload.prescription_id,
            pharmacy_rx_request_id=payload.pharmacy_rx_request_id,
            doctor_name=payload.doctor_name.strip() if payload.doctor_name else None,
            sale_date=today,
            subtotal=round(total_subtotal, 2),
            discount_amount=payload.discount_amount,
            gst_amount=round(total_gst, 2),
            net_amount=final_net,
            amount_paid=paid,
            payment_status=PharmacyPaymentStatus.paid,
            payment_method=payload.payment_method,
            notes=payload.notes.strip() if payload.notes else None,
            created_by_name=actor,
        )
        self._apply_sale_payment_status(sale, paid, final_net)
        self.db.add(sale)
        self.db.add_all(sale_items)

        # Integrate with target Billing domain if registered patient
        if payload.patient_id:
            account_id = None
            if sale_admission_id:
                from modules.billing.entities.billing_entities import FinancialAccountType
                from modules.billing.services.billing_service import get_or_create_financial_account
                acc = get_or_create_financial_account(
                    self.db,
                    hospital_id=self.hospital_id,
                    patient_id=payload.patient_id,
                    account_type=FinancialAccountType.ipd,
                    admission_id=sale_admission_id,
                    created_by_name=actor,
                )
                account_id = acc.id

            charge = ensure_charge(
                self.db,
                hospital_id=self.hospital_id,
                patient_id=payload.patient_id,
                source_type=BillingSourceType.pharmacy,
                source_id=sale.id,
                description=f"Pharmacy {invoice_number} — {sale.customer_name}"[:512],
                charge_amount=sale.net_amount,
                account_id=account_id,
                created_by_name=actor,
            )
            sale.billing_charge_id = charge.id

            # Real money collected at the pharmacy counter is recorded as a billing payment.
            if paid > 0:
                raw_method = getattr(payload, "payment_method", None) or "cash"
                method_val = str(getattr(raw_method, "value", raw_method))
                try:
                    pay_method = BillingPaymentMethod(method_val)
                except ValueError:
                    pay_method = BillingPaymentMethod.cash

                create_payment(
                    self.db,
                    hospital_id=self.hospital_id,
                    patient_id=payload.patient_id,
                    amount=paid,
                    payment_date=date.today(),
                    payment_method=pay_method,
                    account_id=account_id,
                    notes=f"Pharmacy Counter POS Collection: {invoice_number}",
                    received_by_name=actor,
                    allocate=True,
                    allocations_plan=[{"charge_id": charge.id, "amount": min(paid, float(charge.net_amount))}],
                )

            recompute_charge_financials(self.db, charge)
            if charge.status != BillingChargeStatus.paid:
                # Cover the remainder from this account's own deposits (oldest first).
                draw_down_account_deposits_for_charge(
                    self.db,
                    hospital_id=self.hospital_id,
                    charge=charge,
                    created_by_name=actor,
                )

                from modules.billing.services.service_financial_clearance import assert_service_financially_cleared
                assert_service_financially_cleared(
                    self.db,
                    hospital_id=self.hospital_id,
                    source_type=BillingSourceType.pharmacy,
                    source_id=sale.id,
                    action_description="dispense pharmacy items",
                    is_emergency_override=getattr(payload, "is_emergency_override", False),
                    actor=self.user,
                    emergency_reason=getattr(payload, "emergency_override_reason", None),
                    admission_id=sale_admission_id,
                    patient_id=payload.patient_id,
                )

            # Sale payment status mirrors the ledger (payments + deposit allocations).
            recompute_charge_financials(self.db, charge)
            sale.amount_paid = round(float(charge.amount_paid or 0), 2)
            self._apply_sale_payment_status(sale, sale.amount_paid, final_net)
        elif paid < final_net - 0.0001:
            if not getattr(payload, "is_emergency_override", False):
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=f"Retail pharmacy counter sales require full payment before dispensing. Shortfall: ₹{final_net - paid:.2f}",
                )
            self._authorize_walk_in_override(sale, payload, paid, final_net)

        # ── Stock Decrement (Only executed after financial clearance check passes) ──
        bulk_changes = [
            {
                "medicine_id": med_id,
                "batch_id": b_id,
                "quantity_delta": -qty,
                "transaction_type": StockTransactionType.sale,
                "reference_type": "pharmacy_sale",
                "reference_id": sale.id,
                "notes": f"Sale {invoice_number}",
                "created_by_name": actor,
            }
            for med_id, b_id, qty in stock_decrements
        ]
        apply_stock_changes_bulk(
            self.db,
            hospital_id=self.hospital_id,
            changes=bulk_changes,
            preloaded_batches=batch_by_id,
            auto_refresh=False,
        )

        unique_med_ids = list(dict.fromkeys(med_id for med_id, _, _ in stock_decrements))
        if unique_med_ids:
            refresh_medicine_inventories_bulk(self.db, self.hospital_id, unique_med_ids)

        # Transition prescription status (FLAW-004). When dispensing against a pharmacy
        # Rx request, the request-level recompute above already mirrored the clinical
        # Prescription status; only handle the legacy prescription-only path here.
        if rx_obj and not rx_request:
            rx_obj.status = "dispensed"

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="pharmacy_sale",
            entity_id=sale.id,
            summary=f"Pharmacy sale {invoice_number} — {sale.customer_name} ₹{sale.net_amount}",
        )
        self.db.commit()
        created = self.repo.get_sale_by_id(sale.id, self.hospital_id)
        assert created is not None
        return self._sale_to_response(created, warnings)

    def cancel_sale(self, sale_id: UUID) -> SaleResponse:
        sale = self.repo.get_sale_by_id(sale_id, self.hospital_id)
        if not sale:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found")
        if sale.status == PharmacySaleStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Sale already cancelled")

        actor = actor_name(self.user)
        cancel_changes = []
        for it in sale.items or []:
            refundable = float(it.quantity or 0) - float(it.returned_quantity or 0)
            if refundable <= 0:
                continue
            cancel_changes.append(
                {
                    "medicine_id": it.medicine_id,
                    "batch_id": it.batch_id,
                    "quantity_delta": refundable,
                    "transaction_type": StockTransactionType.return_in,
                    "reference_type": "pharmacy_sale_cancel",
                    "reference_id": sale.id,
                    "notes": f"Cancel {sale.invoice_number}",
                    "created_by_name": actor,
                    "allow_expired": True,
                }
            )
            it.returned_quantity = float(it.quantity or 0)

        if cancel_changes:
            apply_stock_changes_bulk(
                self.db,
                hospital_id=self.hospital_id,
                changes=cancel_changes,
                auto_refresh=True,
            )

        sale.status = PharmacySaleStatus.cancelled
        # All dispensed stock was returned above, so the service is no longer "performed".
        cancel_source_charges_or_400(
            self.db,
            self.hospital_id,
            BillingSourceType.pharmacy,
            sale.id,
            reason=f"Pharmacy sale {sale.invoice_number} cancelled",
            actor=self.user,
            performed=False,
        )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="cancel",
            entity_type="pharmacy_sale",
            entity_id=sale.id,
            summary=f"Cancelled pharmacy sale {sale.invoice_number}",
        )
        self.db.commit()
        refreshed = self.repo.get_sale_by_id(sale_id, self.hospital_id)
        assert refreshed is not None
        return self._sale_to_response(refreshed)

    # ── Adjustments ───────────────────────────────────────────────────────────

    def list_adjustments(self, medicine_id: UUID | None = None, limit: int = 100) -> list[AdjustmentResponse]:
        rows = self.repo.list_adjustments(self.hospital_id, medicine_id, limit)
        out: list[AdjustmentResponse] = []
        for r in rows:
            resp = AdjustmentResponse.model_validate(r)
            resp.medicine_name = r.medicine.medicine_name if r.medicine else None
            resp.batch_number = r.batch.batch_number if r.batch else None
            out.append(resp)
        return out

    def create_adjustment(self, payload: AdjustmentCreate) -> AdjustmentResponse:
        actor = actor_name(self.user)
        row = StockAdjustment(
            hospital_id=self.hospital_id,
            medicine_id=payload.medicine_id,
            batch_id=payload.batch_id,
            adjustment_type=payload.adjustment_type,
            quantity=payload.quantity,
            reason=payload.reason.strip(),
            approved_by_name=actor,
            created_by_name=actor,
        )
        self.db.add(row)
        self.db.flush()

        apply_stock_change(
            self.db,
            hospital_id=self.hospital_id,
            medicine_id=payload.medicine_id,
            batch_id=payload.batch_id,
            quantity_delta=-payload.quantity,
            transaction_type=StockTransactionType.adjustment,
            reference_type="stock_adjustment",
            reference_id=row.id,
            notes=payload.reason,
            created_by_name=actor,
            allow_expired=True,
        )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="stock_adjustment",
            entity_id=row.id,
            summary=f"Stock adjustment {payload.adjustment_type.value} qty {payload.quantity}",
        )
        self.db.commit()
        created = self.repo.get_adjustment_by_id(row.id, self.hospital_id)
        assert created is not None
        resp = AdjustmentResponse.model_validate(created)
        resp.medicine_name = created.medicine.medicine_name if created.medicine else None
        resp.batch_number = created.batch.batch_number if created.batch else None
        return resp

    # ── Returns ───────────────────────────────────────────────────────────────

    def customer_return(self, payload: CustomerReturnCreate) -> ReturnResponse:
        actor = actor_name(self.user)
        sale = self.repo.get_sale_by_id(payload.sale_id, self.hospital_id)
        if not sale or sale.status == PharmacySaleStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found")

        item = next((i for i in sale.items if i.id == payload.sale_item_id), None)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale item not found")

        remaining = float(item.quantity or 0) - float(item.returned_quantity or 0)
        if payload.quantity > remaining + 0.0001:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Return qty exceeds remaining ({remaining})",
            )

        unit_net = float(item.net_amount or 0) / max(float(item.quantity or 1), 0.0001)
        refund = round(unit_net * float(payload.quantity), 2)

        apply_stock_change(
            self.db,
            hospital_id=self.hospital_id,
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
            hospital_id=self.hospital_id,
            return_type="customer",
            sale_id=sale.id,
            medicine_id=item.medicine_id,
            batch_id=item.batch_id,
            quantity=float(payload.quantity),
            refund_amount=refund,
            reason=payload.reason,
            created_by_name=actor,
        )
        self.db.add(row)
        self.db.flush()

        # Reconcile with billing ledger if sale belongs to a registered patient (FLAW-005)
        if sale.patient_id and refund > 0:
            from modules.billing.entities.billing_entities import BillingCharge, BillingChargeStatus, BillingSourceType
            credit_charge = BillingCharge(
                hospital_id=self.hospital_id,
                patient_id=sale.patient_id,
                source_type=BillingSourceType.pharmacy,
                source_id=row.id,
                description=f"Pharmacy Return Credit - {sale.invoice_number} ({payload.reason or 'Return'})",
                charge_amount=-abs(refund),
                net_amount=-abs(refund),
                amount_paid=-abs(refund),
                status=BillingChargeStatus.paid,
                notes=f"Return credit for sale {sale.invoice_number} item {item.id}",
                created_by_name=actor or "Pharmacy Staff",
            )
            self.db.add(credit_charge)
            self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="pharmacy_return",
            entity_id=row.id,
            summary=f"Customer return on {sale.invoice_number} qty {payload.quantity}",
        )
        self.db.commit()
        self.db.refresh(row)
        return ReturnResponse.model_validate(row)

    def purchase_return(self, payload: PurchaseReturnCreate) -> ReturnResponse:
        actor = actor_name(self.user)
        purchase = self.repo.get_purchase_by_id(payload.purchase_id, self.hospital_id)
        if not purchase:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

        item = next((i for i in purchase.items if i.id == payload.purchase_item_id), None)
        if not item or not item.batch_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase item not found")

        apply_stock_change(
            self.db,
            hospital_id=self.hospital_id,
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
            hospital_id=self.hospital_id,
            return_type="purchase",
            purchase_id=purchase.id,
            medicine_id=item.medicine_id,
            batch_id=item.batch_id,
            quantity=float(payload.quantity),
            refund_amount=0.0,
            reason=payload.reason,
            created_by_name=actor,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="pharmacy_return",
            entity_id=row.id,
            summary=f"Purchase return on {purchase.invoice_number} qty {payload.quantity}",
        )
        self.db.commit()
        self.db.refresh(row)
        return ReturnResponse.model_validate(row)

    def list_returns(self, limit: int = 100) -> list[ReturnResponse]:
        rows = self.repo.list_returns(self.hospital_id, limit)
        return [ReturnResponse.model_validate(r) for r in rows]

    # ── Rx requests ───────────────────────────────────────────────────────────

    def list_rx_requests(
        self,
        status: str | None = None,
        prescription_id: UUID | None = None,
        limit: int = 100,
    ) -> list[RxRequestResponse]:
        rows = self.repo.list_rx_requests(
            self.hospital_id, status=status, prescription_id=prescription_id, limit=limit
        )
        return [self._rx_request_to_response(r) for r in rows]

    def get_rx_request(self, rx_request_id: UUID) -> RxRequestResponse:
        row = self.repo.get_rx_request_by_id(rx_request_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rx request not found")
        return self._rx_request_to_response(row)

    def create_rx_request(self, payload: RxRequestCreate) -> RxRequestResponse:
        actor = actor_name(self.user)

        rx_obj = None
        if payload.prescription_id:
            from modules.clinical_records.entities.clinical_record import Prescription
            rx_obj = (
                self.db.query(Prescription)
                .filter(
                    Prescription.id == payload.prescription_id,
                    Prescription.hospital_id == self.hospital_id,
                )
                .first()
            )
            if not rx_obj:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Prescription not found"
                )
            if getattr(rx_obj, "status", None) == "draft":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot create an Rx request for a draft prescription. Prescription must be issued.",
                )

        items_payload = payload.items or []
        if not items_payload:
            if rx_obj is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="At least one Rx request item is required, or provide a prescription_id to derive items.",
                )
            items_payload = self._parse_prescription_items(rx_obj)

        patient_id = payload.patient_id or (rx_obj.patient_id if rx_obj else None)
        admission_id = payload.admission_id or (rx_obj.admission_id if rx_obj else None)
        doctor_id = payload.doctor_id or (rx_obj.doctor_id if rx_obj else None)
        patient_name = payload.patient_name or (rx_obj.patient.name if rx_obj and rx_obj.patient else "")
        patient_phone = payload.patient_phone or (rx_obj.patient.mobile if rx_obj and rx_obj.patient else "")
        doctor_name = payload.doctor_name or (rx_obj.doctor.name if rx_obj and rx_obj.doctor else "")

        rx = PharmacyRxRequest(
            hospital_id=self.hospital_id,
            prescription_id=payload.prescription_id,
            patient_id=patient_id,
            admission_id=admission_id,
            doctor_id=doctor_id,
            patient_name=patient_name,
            patient_phone=patient_phone,
            doctor_name=doctor_name,
            status=PharmacyRxRequestStatus.pending,
            notes=payload.notes.strip() if payload.notes else None,
        )
        self.db.add(rx)
        self.db.flush()

        for idx, it in enumerate(items_payload):
            medicine_id = getattr(it, "medicine_id", None)
            medicine_name = getattr(it, "medicine_name", None) or ""
            quantity = float(getattr(it, "quantity", 1.0) or 1.0)
            dosage = getattr(it, "dosage", None)
            self.db.add(
                PharmacyRxRequestItem(
                    hospital_id=self.hospital_id,
                    request_id=rx.id,
                    medicine_id=medicine_id,
                    medicine_name=medicine_name.strip(),
                    quantity=quantity,
                    dosage=dosage.strip() if dosage else None,
                    dispensed_quantity=0.0,
                    status=PharmacyRxRequestStatus.pending.value,
                    sort_order=idx,
                )
            )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="pharmacy_rx_request",
            entity_id=rx.id,
            summary=f"Created pharmacy Rx request for {patient_name}",
        )
        self.db.commit()
        created = self.repo.get_rx_request_by_id(rx.id, self.hospital_id)
        assert created is not None
        return self._rx_request_to_response(created)

    @staticmethod
    def _parse_prescription_items(rx_obj: Any) -> list[Any]:
        """Parse the free-text clinical Prescription.medicines blob into item payloads."""
        text = getattr(rx_obj, "medicines", "") or ""
        parts = []
        for chunk in text.replace(";", ",").replace("\n", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                parts.append(chunk)
        return [SimpleNamespace(medicine_id=None, medicine_name=p, quantity=1.0, dosage=None) for p in parts]

    @staticmethod
    def _recompute_rx_status(rx: PharmacyRxRequest) -> PharmacyRxRequestStatus:
        """Derive the request status from its items' dispensed quantities."""
        items = rx.items or []
        if not items:
            return PharmacyRxRequestStatus.pending
        any_remaining = False
        any_dispensed = False
        for it in items:
            remaining = float(it.quantity or 0) - float(it.dispensed_quantity or 0)
            if remaining > 0.0001:
                any_remaining = True
            if float(it.dispensed_quantity or 0) > 0.0001:
                any_dispensed = True
        if not any_remaining:
            return PharmacyRxRequestStatus.completed
        if any_dispensed:
            return PharmacyRxRequestStatus.partially_dispensed
        return PharmacyRxRequestStatus.pending

    @staticmethod
    def _sync_item_status(it: PharmacyRxRequestItem) -> None:
        remaining = float(it.quantity or 0) - float(it.dispensed_quantity or 0)
        if remaining <= 0.0001:
            it.status = PharmacyRxRequestStatus.dispensed.value
        elif float(it.dispensed_quantity or 0) > 0.0001:
            it.status = PharmacyRxRequestStatus.partially_dispensed.value
        else:
            it.status = PharmacyRxRequestStatus.pending.value

    # ── Reports ───────────────────────────────────────────────────────────────

    def get_reports(
        self,
        report: str,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> dict[str, Any]:
        today = date.today()
        if report in {"current_stock", "low_stock", "out_of_stock", "inventory_valuation"}:
            rows = self.repo.list_inventory(self.hospital_id)
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
                self.db.query(MedicineBatch)
                .options(joinedload(MedicineBatch.medicine))
                .filter(
                    MedicineBatch.hospital_id == self.hospital_id,
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
            q = self.db.query(PharmacySale).filter(
                PharmacySale.hospital_id == self.hospital_id,
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
            q = (
                self.db.query(PharmacyPurchase)
                .options(joinedload(PharmacyPurchase.supplier))
                .filter(PharmacyPurchase.hospital_id == self.hospital_id)
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
                self.db.query(
                    Medicine.medicine_name,
                    func.sum(PharmacySaleItem.quantity).label("qty"),
                    func.sum(PharmacySaleItem.net_amount).label("amt"),
                )
                .join(PharmacySaleItem, PharmacySaleItem.medicine_id == Medicine.id)
                .join(PharmacySale, PharmacySale.id == PharmacySaleItem.sale_id)
                .filter(
                    PharmacySale.hospital_id == self.hospital_id,
                    PharmacySale.status != PharmacySaleStatus.cancelled,
                )
            )
            if from_date:
                q = q.filter(PharmacySale.sale_date >= from_date)
            if to_date:
                q = q.filter(PharmacySale.sale_date <= to_date)
            rows = q.group_by(Medicine.medicine_name).order_by(func.sum(PharmacySaleItem.quantity).desc()).limit(50).all()
            return {
                "report": report,
                "rows": [{"medicine_name": n, "qty": float(qty or 0), "amount": float(amt or 0)} for n, qty, amt in rows],
            }

        if report == "profit":
            items = (
                self.db.query(PharmacySaleItem)
                .join(PharmacySale, PharmacySale.id == PharmacySaleItem.sale_id)
                .filter(
                    PharmacySale.hospital_id == self.hospital_id,
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

        # Default fallback
        rows = self.repo.list_inventory(self.hospital_id)
        return {"report": report, "rows": [r.model_dump() for r in rows]}
