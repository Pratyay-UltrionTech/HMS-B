"""Root router composing module routers.

Conforms to UltrionTech-Backend-Template app/router.py specification.
Mounts migrated modules that are verified and production-ready.
"""

from fastapi import APIRouter

from modules.admin.api.admin_api import router as admin_router
from modules.ambulance.api.ambulance_api import router as ambulance_router
from modules.analytics.api.analytics_api import router as analytics_router
from modules.appointments.api.appointments_api import router as appointments_router
from modules.auth.api.auth_api import router as auth_router
from modules.beds.api.beds_api import registration_inpatient_router
from modules.beds.api.beds_api import router as beds_router
from modules.billing.api.billing_api import router as billing_router
from modules.critical_care.api.critical_care_api import router as critical_care_router
from modules.cssd.api.cssd_api import router as cssd_router
from modules.dms.api.dms_api import router as dms_router
from modules.doctors.api.doctors_api import router as doctors_router
from modules.emergency.api.emergency_api import router as emergency_router
from modules.equipment.api.equipment_api import router as equipment_router
from modules.inpatient.api.ipd_api import router as ipd_router
from modules.inpatient.api.nursing_api import router as nursing_router
from modules.inventory.api.inventory_api import router as inventory_router
from modules.laboratory.api.laboratory_api import router as laboratory_router
from modules.blood_bank.api.blood_bank_api import router as blood_bank_router
from modules.clinical_decision.api.clinical_decision_api import router as clinical_decision_router
from modules.insurance.api.insurance_api import router as insurance_router
from modules.masters.api.masters_api import router as masters_router
from modules.mis.api.mis_api import router as mis_router
from modules.ot.api.ot_api import router as ot_router
from modules.patients.api.allergy_api import router as allergy_router
from modules.patients.api.patients_api import router as patients_router
from modules.procurement.api.procurement_api import router as procurement_router
from modules.pharmacy.api.drug_interaction_api import router as drug_interaction_router
from modules.pharmacy.api.pharmacy_api import router as pharmacy_router
from modules.radiology.api.radiology_api import router as radiology_router
from modules.tenancy.api.tenancy_api import router as tenancy_router
from modules.vitals.api.vitals_api import router as vitals_router

root_router = APIRouter()

# Mount migrated vertical slice routers
root_router.include_router(auth_router)
root_router.include_router(patients_router)
root_router.include_router(allergy_router)
root_router.include_router(vitals_router)
root_router.include_router(analytics_router)
root_router.include_router(appointments_router)
root_router.include_router(doctors_router)
root_router.include_router(emergency_router)
root_router.include_router(critical_care_router)
root_router.include_router(ambulance_router)
root_router.include_router(beds_router)
root_router.include_router(registration_inpatient_router)
root_router.include_router(ipd_router)
root_router.include_router(nursing_router)
root_router.include_router(billing_router)
root_router.include_router(laboratory_router)
root_router.include_router(dms_router)
root_router.include_router(radiology_router)
root_router.include_router(ot_router)
root_router.include_router(equipment_router)
root_router.include_router(pharmacy_router)
root_router.include_router(drug_interaction_router)
root_router.include_router(mis_router)
root_router.include_router(masters_router)
root_router.include_router(clinical_decision_router)
root_router.include_router(insurance_router)
root_router.include_router(blood_bank_router)
root_router.include_router(cssd_router)
root_router.include_router(inventory_router)
root_router.include_router(procurement_router)
root_router.include_router(admin_router)
root_router.include_router(tenancy_router)
