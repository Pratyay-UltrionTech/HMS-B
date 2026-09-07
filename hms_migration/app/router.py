"""Root router composing module routers.

Conforms to UltrionTech-Backend-Template app/router.py specification.
Mounts migrated modules that are verified and production-ready.
"""

from fastapi import APIRouter

from hms_migration.modules.admin.api.admin_api import router as admin_router
from hms_migration.modules.analytics.api.analytics_api import router as analytics_router
from hms_migration.modules.appointments.api.appointments_api import router as appointments_router
from hms_migration.modules.auth.api.auth_api import router as auth_router
from hms_migration.modules.beds.api.beds_api import registration_inpatient_router
from hms_migration.modules.beds.api.beds_api import router as beds_router
from hms_migration.modules.billing.api.billing_api import router as billing_router
from hms_migration.modules.dms.api.dms_api import router as dms_router
from hms_migration.modules.doctors.api.doctors_api import router as doctors_router
from hms_migration.modules.equipment.api.equipment_api import router as equipment_router
from hms_migration.modules.inpatient.api.ipd_api import router as ipd_router
from hms_migration.modules.laboratory.api.laboratory_api import router as laboratory_router
from hms_migration.modules.masters.api.masters_api import router as masters_router
from hms_migration.modules.mis.api.mis_api import router as mis_router
from hms_migration.modules.ot.api.ot_api import router as ot_router
from hms_migration.modules.patients.api.patients_api import router as patients_router
from hms_migration.modules.pharmacy.api.pharmacy_api import router as pharmacy_router
from hms_migration.modules.radiology.api.radiology_api import router as radiology_router
from hms_migration.modules.tenancy.api.tenancy_api import router as tenancy_router
from hms_migration.modules.vitals.api.vitals_api import router as vitals_router

root_router = APIRouter()

# Mount migrated vertical slice routers
root_router.include_router(auth_router)
root_router.include_router(patients_router)
root_router.include_router(vitals_router)
root_router.include_router(analytics_router)
root_router.include_router(appointments_router)
root_router.include_router(doctors_router)
root_router.include_router(beds_router)
root_router.include_router(registration_inpatient_router)
root_router.include_router(ipd_router)
root_router.include_router(billing_router)
root_router.include_router(laboratory_router)
root_router.include_router(dms_router)
root_router.include_router(radiology_router)
root_router.include_router(ot_router)
root_router.include_router(equipment_router)
root_router.include_router(pharmacy_router)
root_router.include_router(mis_router)
root_router.include_router(masters_router)
root_router.include_router(admin_router)
root_router.include_router(tenancy_router)
