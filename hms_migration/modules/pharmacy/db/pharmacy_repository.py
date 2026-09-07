"""
Pharmacy database repository for querying and managing records.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.pharmacy.contracts.pharmacy_contracts import (
    InventoryRow,
    SearchHit,
)
from hms_migration.modules.pharmacy.entities.pharmacy_entities import (
    Medicine,
    MedicineBatch,
    MedicineCategory,
    MedicineInventory,
    PharmacyPaymentStatus,
    PharmacyPurchase,
    PharmacyPurchaseItem,
    PharmacyReturn,
    PharmacySale,
    PharmacySaleItem,
    PharmacySaleStatus,
    PharmacySaleType,
    PharmacySupplier,
    StockAdjustment,
)
from hms_migration.modules.pharmacy.services.pharmacy_stock_service import inventory_status


class PharmacyRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── Categories ────────────────────────────────────────────────────────────

    def list_categories(self, hospital_id: UUID, active_only: bool | None = None) -> Sequence[MedicineCategory]:
        q = self.db.query(MedicineCategory).filter(MedicineCategory.hospital_id == hospital_id)
        if active_only is not None:
            q = q.filter(MedicineCategory.is_active == active_only)
        return q.order_by(MedicineCategory.name.asc()).all()

    def get_category_by_id(self, category_id: UUID, hospital_id: UUID) -> MedicineCategory | None:
        return (
            self.db.query(MedicineCategory)
            .filter(MedicineCategory.id == category_id, MedicineCategory.hospital_id == hospital_id)
            .first()
        )

    def get_category_by_name(self, hospital_id: UUID, name: str) -> MedicineCategory | None:
        return (
            self.db.query(MedicineCategory)
            .filter(MedicineCategory.hospital_id == hospital_id, MedicineCategory.name.ilike(name))
            .first()
        )

    # ── Suppliers ─────────────────────────────────────────────────────────────

    def list_suppliers(self, hospital_id: UUID, active_only: bool | None = None) -> Sequence[PharmacySupplier]:
        q = self.db.query(PharmacySupplier).filter(PharmacySupplier.hospital_id == hospital_id)
        if active_only is not None:
            q = q.filter(PharmacySupplier.is_active == active_only)
        return q.order_by(PharmacySupplier.company_name.asc()).all()

    def get_supplier_by_id(self, supplier_id: UUID, hospital_id: UUID) -> PharmacySupplier | None:
        return (
            self.db.query(PharmacySupplier)
            .filter(PharmacySupplier.id == supplier_id, PharmacySupplier.hospital_id == hospital_id)
            .first()
        )

    def get_supplier_by_company_name(self, hospital_id: UUID, company_name: str) -> PharmacySupplier | None:
        return (
            self.db.query(PharmacySupplier)
            .filter(PharmacySupplier.hospital_id == hospital_id, PharmacySupplier.company_name.ilike(company_name))
            .first()
        )

    # ── Medicines ─────────────────────────────────────────────────────────────

    def list_medicines(
        self,
        hospital_id: UUID,
        search: str | None = None,
        category_id: UUID | None = None,
        active_only: bool | None = None,
    ) -> Sequence[Medicine]:
        q = (
            self.db.query(Medicine)
            .options(joinedload(Medicine.category), joinedload(Medicine.inventory))
            .filter(Medicine.hospital_id == hospital_id)
        )
        if search:
            like = f"%{search.strip()}%"
            q = q.filter(
                or_(
                    Medicine.medicine_name.ilike(like),
                    Medicine.generic_name.ilike(like),
                    Medicine.brand_name.ilike(like),
                    Medicine.manufacturer.ilike(like),
                    Medicine.barcode_value.ilike(like),
                )
            )
        if category_id:
            q = q.filter(Medicine.category_id == category_id)
        if active_only is not None:
            q = q.filter(Medicine.is_active == active_only)
        return q.order_by(Medicine.medicine_name.asc()).all()

    def get_medicine_by_id(self, medicine_id: UUID, hospital_id: UUID) -> Medicine | None:
        return (
            self.db.query(Medicine)
            .options(joinedload(Medicine.category), joinedload(Medicine.inventory))
            .filter(Medicine.id == medicine_id, Medicine.hospital_id == hospital_id)
            .first()
        )

    def find_duplicate_medicine(
        self,
        hospital_id: UUID,
        brand_name: str,
        strength: str,
        manufacturer: str,
        exclude_id: UUID | None = None,
    ) -> Medicine | None:
        q = self.db.query(Medicine).filter(
            Medicine.hospital_id == hospital_id,
            Medicine.brand_name.ilike(brand_name),
            Medicine.strength.ilike(strength),
            Medicine.manufacturer.ilike(manufacturer),
        )
        if exclude_id:
            q = q.filter(Medicine.id != exclude_id)
        return q.first()

    # ── Batches ───────────────────────────────────────────────────────────────

    def list_batches_for_medicine(self, hospital_id: UUID, medicine_id: UUID) -> Sequence[MedicineBatch]:
        return (
            self.db.query(MedicineBatch)
            .options(joinedload(MedicineBatch.medicine))
            .filter(
                MedicineBatch.hospital_id == hospital_id,
                MedicineBatch.medicine_id == medicine_id,
                MedicineBatch.is_active.is_(True),
            )
            .order_by(MedicineBatch.expiry_date.asc())
            .all()
        )

    def get_batch_by_id(self, batch_id: UUID, hospital_id: UUID) -> MedicineBatch | None:
        return (
            self.db.query(MedicineBatch)
            .options(joinedload(MedicineBatch.medicine))
            .filter(MedicineBatch.id == batch_id, MedicineBatch.hospital_id == hospital_id)
            .first()
        )

    # ── Inventory ─────────────────────────────────────────────────────────────

    def list_inventory(
        self,
        hospital_id: UUID,
        search: str | None = None,
        status_filter: str | None = None,
        category_id: UUID | None = None,
    ) -> list[InventoryRow]:
        today = date.today()
        q = (
            self.db.query(Medicine, MedicineInventory)
            .outerjoin(MedicineInventory, MedicineInventory.medicine_id == Medicine.id)
            .options(joinedload(Medicine.category))
            .filter(Medicine.hospital_id == hospital_id, Medicine.is_active.is_(True))
        )
        if search:
            like = f"%{search.strip()}%"
            q = q.filter(
                or_(
                    Medicine.medicine_name.ilike(like),
                    Medicine.brand_name.ilike(like),
                    Medicine.generic_name.ilike(like),
                )
            )
        if category_id:
            q = q.filter(Medicine.category_id == category_id)

        rows: list[InventoryRow] = []
        for med, inv in q.all():
            st = inventory_status(inv, med, today)
            if status_filter and st != status_filter:
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
                    requires_prescription=med.requires_prescription,
                )
            )
        return rows

    # ── Search ────────────────────────────────────────────────────────────────

    def search_hits(self, hospital_id: UUID, q: str, limit: int = 25) -> list[SearchHit]:
        text = q.strip()
        like = f"%{text}%"
        hits: list[SearchHit] = []

        batch_matches = (
            self.db.query(MedicineBatch, Medicine)
            .join(Medicine, Medicine.id == MedicineBatch.medicine_id)
            .filter(
                MedicineBatch.hospital_id == hospital_id,
                MedicineBatch.is_active.is_(True),
                or_(
                    MedicineBatch.barcode_value == text,
                    MedicineBatch.batch_number.ilike(like),
                    Medicine.barcode_value == text,
                    Medicine.qr_code == text,
                ),
            )
            .limit(limit)
            .all()
        )
        for b, m in batch_matches:
            field = "barcode" if (b.barcode_value == text or m.barcode_value == text) else "batch"
            hits.append(
                SearchHit(
                    medicine_id=m.id,
                    medicine_name=m.medicine_name,
                    brand_name=m.brand_name,
                    strength=m.strength,
                    batch_id=b.id,
                    batch_number=b.batch_number,
                    available_quantity=float(b.available_quantity or 0),
                    expiry_date=b.expiry_date,
                    selling_price=float(b.selling_price or m.selling_price or 0),
                    match_field=field,
                )
            )
        if len(hits) >= limit:
            return hits[:limit]

        med_matches = (
            self.db.query(Medicine)
            .options(joinedload(Medicine.batches))
            .filter(
                Medicine.hospital_id == hospital_id,
                Medicine.is_active.is_(True),
                or_(
                    Medicine.medicine_name.ilike(like),
                    Medicine.brand_name.ilike(like),
                    Medicine.generic_name.ilike(like),
                ),
            )
            .limit(limit)
            .all()
        )
        seen_pairs = {(h.medicine_id, h.batch_id) for h in hits}
        today = date.today()
        for m in med_matches:
            valid_batches = [
                b for b in (m.batches or []) if b.is_active and b.available_quantity > 0 and b.expiry_date >= today
            ]
            if not valid_batches:
                if (m.id, None) not in seen_pairs:
                    hits.append(
                        SearchHit(
                            medicine_id=m.id,
                            medicine_name=m.medicine_name,
                            brand_name=m.brand_name,
                            strength=m.strength,
                            batch_id=None,
                            batch_number=None,
                            available_quantity=0.0,
                            expiry_date=None,
                            selling_price=float(m.selling_price or 0),
                            match_field="name",
                        )
                    )
                    seen_pairs.add((m.id, None))
            else:
                for b in valid_batches:
                    if (m.id, b.id) not in seen_pairs:
                        hits.append(
                            SearchHit(
                                medicine_id=m.id,
                                medicine_name=m.medicine_name,
                                brand_name=m.brand_name,
                                strength=m.strength,
                                batch_id=b.id,
                                batch_number=b.batch_number,
                                available_quantity=float(b.available_quantity or 0),
                                expiry_date=b.expiry_date,
                                selling_price=float(b.selling_price or m.selling_price or 0),
                                match_field="name",
                            )
                        )
                        seen_pairs.add((m.id, b.id))
        return hits[:limit]

    # ── Purchases ─────────────────────────────────────────────────────────────

    def list_purchases(
        self,
        hospital_id: UUID,
        supplier_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 100,
    ) -> Sequence[PharmacyPurchase]:
        q = (
            self.db.query(PharmacyPurchase)
            .options(
                joinedload(PharmacyPurchase.supplier),
                joinedload(PharmacyPurchase.items).joinedload(PharmacyPurchaseItem.medicine),
            )
            .filter(PharmacyPurchase.hospital_id == hospital_id)
        )
        if supplier_id:
            q = q.filter(PharmacyPurchase.supplier_id == supplier_id)
        if from_date:
            q = q.filter(PharmacyPurchase.received_date >= from_date)
        if to_date:
            q = q.filter(PharmacyPurchase.received_date <= to_date)
        return q.order_by(PharmacyPurchase.received_date.desc()).limit(limit).all()

    def get_purchase_by_id(self, purchase_id: UUID, hospital_id: UUID) -> PharmacyPurchase | None:
        return (
            self.db.query(PharmacyPurchase)
            .options(
                joinedload(PharmacyPurchase.supplier),
                joinedload(PharmacyPurchase.items).joinedload(PharmacyPurchaseItem.medicine),
            )
            .filter(PharmacyPurchase.id == purchase_id, PharmacyPurchase.hospital_id == hospital_id)
            .first()
        )

    def get_purchase_by_invoice(self, hospital_id: UUID, invoice_number: str) -> PharmacyPurchase | None:
        return (
            self.db.query(PharmacyPurchase)
            .filter(PharmacyPurchase.hospital_id == hospital_id, PharmacyPurchase.invoice_number == invoice_number)
            .first()
        )

    # ── Sales ─────────────────────────────────────────────────────────────────

    def list_sales(
        self,
        hospital_id: UUID,
        search: str | None = None,
        sale_type: PharmacySaleType | None = None,
        payment_status: PharmacyPaymentStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        patient_id: UUID | None = None,
        limit: int = 100,
    ) -> Sequence[PharmacySale]:
        q = (
            self.db.query(PharmacySale)
            .options(
                joinedload(PharmacySale.items).joinedload(PharmacySaleItem.medicine),
            )
            .filter(PharmacySale.hospital_id == hospital_id)
        )
        if search:
            like = f"%{search.strip()}%"
            q = q.filter(
                or_(
                    PharmacySale.invoice_number.ilike(like),
                    PharmacySale.customer_name.ilike(like),
                    PharmacySale.customer_phone.ilike(like),
                )
            )
        if sale_type:
            q = q.filter(PharmacySale.sale_type == sale_type)
        if payment_status:
            q = q.filter(PharmacySale.payment_status == payment_status)
        if from_date:
            q = q.filter(PharmacySale.sale_date >= from_date)
        if to_date:
            q = q.filter(PharmacySale.sale_date <= to_date)
        if patient_id:
            q = q.filter(PharmacySale.patient_id == patient_id)
        return q.order_by(PharmacySale.created_at.desc()).limit(limit).all()

    def get_sale_by_id(self, sale_id: UUID, hospital_id: UUID) -> PharmacySale | None:
        return (
            self.db.query(PharmacySale)
            .options(
                joinedload(PharmacySale.items).joinedload(PharmacySaleItem.medicine),
            )
            .filter(PharmacySale.id == sale_id, PharmacySale.hospital_id == hospital_id)
            .first()
        )

    # ── Adjustments ───────────────────────────────────────────────────────────

    def list_adjustments(
        self, hospital_id: UUID, medicine_id: UUID | None = None, limit: int = 100
    ) -> Sequence[StockAdjustment]:
        q = (
            self.db.query(StockAdjustment)
            .options(joinedload(StockAdjustment.medicine), joinedload(StockAdjustment.batch))
            .filter(StockAdjustment.hospital_id == hospital_id)
        )
        if medicine_id:
            q = q.filter(StockAdjustment.medicine_id == medicine_id)
        return q.order_by(StockAdjustment.created_at.desc()).limit(limit).all()

    def get_adjustment_by_id(self, adjustment_id: UUID, hospital_id: UUID) -> StockAdjustment | None:
        return (
            self.db.query(StockAdjustment)
            .options(joinedload(StockAdjustment.medicine), joinedload(StockAdjustment.batch))
            .filter(StockAdjustment.id == adjustment_id, StockAdjustment.hospital_id == hospital_id)
            .first()
        )

    # ── Returns ───────────────────────────────────────────────────────────────

    def list_returns(self, hospital_id: UUID, limit: int = 100) -> Sequence[PharmacyReturn]:
        return (
            self.db.query(PharmacyReturn)
            .filter(PharmacyReturn.hospital_id == hospital_id)
            .order_by(PharmacyReturn.created_at.desc())
            .limit(limit)
            .all()
        )
