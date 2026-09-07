"""
Pharmacy business action orchestrator.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.billing.entities.billing_entities import BillingSourceType
from hms_migration.modules.billing.services.billing_service import (
    cancel_charge_for_source,
    ensure_charge,
)
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
    PurchaseItemResponse,
    PurchaseResponse,
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
from hms_migration.modules.pharmacy.db.pharmacy_repository import PharmacyRepository
from hms_migration.modules.pharmacy.entities.pharmacy_entities import (
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
    PharmacyRxRequestStatus,
    PharmacySale,
    PharmacySaleItem,
    PharmacySaleStatus,
    PharmacySaleType,
    PharmacySupplier,
    PurchasePaymentStatus,
    StockAdjustment,
    StockTransactionType,
)
from hms_migration.modules.pharmacy.services.pharmacy_stock_service import (
    actor_name,
    allocate_fifo,
    apply_stock_change,
    inventory_status,
    line_amounts,
    next_doc_number,
    refresh_medicine_inventory,
)
from hms_migration.shared.audit import write_audit_log


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
    ) -> list[MedicineResponse]:
        rows = self.repo.list_medicines(self.hospital_id, search, category_id, active_only)
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

        line_computations = []
        for it in payload.items:
            med = self.repo.get_medicine_by_id(it.medicine_id, self.hospital_id)
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
        self.db.flush()

        actor = actor_name(self.user)
        for it, med, gross, disc, gst, net in line_computations:
            batch_num = it.batch_number.strip()
            batch = (
                self.db.query(MedicineBatch)
                .filter(
                    MedicineBatch.hospital_id == self.hospital_id,
                    MedicineBatch.medicine_id == it.medicine_id,
                    MedicineBatch.batch_number == batch_num,
                )
                .first()
            )
            total_incoming = it.quantity + it.free_quantity
            if not batch:
                batch = MedicineBatch(
                    hospital_id=self.hospital_id,
                    medicine_id=it.medicine_id,
                    supplier_id=payload.supplier_id,
                    batch_number=batch_num,
                    manufacture_date=it.manufacture_date,
                    expiry_date=it.expiry_date,
                    purchase_price=it.purchase_price,
                    selling_price=it.selling_price,
                    mrp=it.mrp,
                    initial_quantity=total_incoming,
                    available_quantity=0.0,
                    is_active=True,
                )
                self.db.add(batch)
                self.db.flush()

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

            apply_stock_change(
                self.db,
                hospital_id=self.hospital_id,
                medicine_id=it.medicine_id,
                batch_id=batch.id,
                quantity_delta=total_incoming,
                transaction_type=StockTransactionType.purchase,
                reference_type="pharmacy_purchase",
                reference_id=purchase.id,
                notes=f"Purchase {inv_num}",
                created_by_name=actor,
                allow_expired=True,
            )

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
        actor = actor_name(self.user)
        today = payload.sale_date or date.today()
        invoice_number = next_doc_number(self.db, self.hospital_id, PharmacySale, "invoice_number", "SAL")

        sale = PharmacySale(
            hospital_id=self.hospital_id,
            invoice_number=invoice_number,
            sale_type=payload.sale_type,
            status=PharmacySaleStatus.completed,
            customer_name=payload.customer_name.strip(),
            customer_phone=payload.customer_phone.strip(),
            patient_id=payload.patient_id,
            prescription_id=payload.prescription_id,
            pharmacy_rx_request_id=payload.pharmacy_rx_request_id,
            doctor_name=payload.doctor_name.strip() if payload.doctor_name else None,
            sale_date=today,
            subtotal=0.0,
            discount_amount=payload.discount_amount,
            gst_amount=0.0,
            net_amount=0.0,
            amount_paid=payload.amount_paid if payload.amount_paid is not None else 0.0,
            payment_status=PharmacyPaymentStatus.paid,
            payment_method=payload.payment_method,
            notes=payload.notes.strip() if payload.notes else None,
            created_by_name=actor,
        )
        self.db.add(sale)
        self.db.flush()

        total_subtotal = 0.0
        total_gst = 0.0
        total_net = 0.0
        warnings: list[str] = []

        for it in payload.items:
            med = self.repo.get_medicine_by_id(it.medicine_id, self.hospital_id)
            if not med:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Medicine {it.medicine_id} not found")

            allocations: list[tuple[MedicineBatch, float]] = []
            if it.batch_id:
                batch = self.repo.get_batch_by_id(it.batch_id, self.hospital_id)
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
                allocations = allocate_fifo(
                    self.db,
                    hospital_id=self.hospital_id,
                    medicine_id=it.medicine_id,
                    quantity=it.quantity,
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
                    hospital_id=self.hospital_id,
                    sale_id=sale.id,
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
                self.db.add(sale_item)

                apply_stock_change(
                    self.db,
                    hospital_id=self.hospital_id,
                    medicine_id=it.medicine_id,
                    batch_id=b.id,
                    quantity_delta=-qty,
                    transaction_type=StockTransactionType.sale,
                    reference_type="pharmacy_sale",
                    reference_id=sale.id,
                    notes=f"Sale {invoice_number}",
                    created_by_name=actor,
                )

        final_net = max(0.0, round(total_net - payload.discount_amount, 2))
        sale.subtotal = round(total_subtotal, 2)
        sale.gst_amount = round(total_gst, 2)
        sale.net_amount = final_net
        if payload.amount_paid is None:
            sale.amount_paid = final_net

        paid = float(sale.amount_paid or 0)
        if paid >= final_net - 0.0001:
            sale.payment_status = PharmacyPaymentStatus.paid
        elif paid > 0:
            sale.payment_status = PharmacyPaymentStatus.partial
        else:
            sale.payment_status = PharmacyPaymentStatus.unpaid

        # Integrate with target Billing domain if registered patient
        if payload.patient_id:
            charge = ensure_charge(
                self.db,
                hospital_id=self.hospital_id,
                patient_id=payload.patient_id,
                source_type=BillingSourceType.pharmacy,
                source_id=sale.id,
                description=f"Pharmacy {invoice_number} — {sale.customer_name}"[:512],
                charge_amount=sale.net_amount,
                created_by_name=actor,
            )
            sale.billing_charge_id = charge.id

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
        for it in sale.items or []:
            refundable = float(it.quantity or 0) - float(it.returned_quantity or 0)
            if refundable <= 0:
                continue
            apply_stock_change(
                self.db,
                hospital_id=self.hospital_id,
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
            cancel_charge_for_source(self.db, self.hospital_id, BillingSourceType.pharmacy, sale.id)

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
