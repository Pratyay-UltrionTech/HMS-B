"""
Patient domain vertical slice.

Conforms to UltrionTech-Backend-Template modules/patients specification.
Contains core demographic, clinical identity, and registration capabilities.
"""

from hms_migration.modules.patients.api.patients_api import router
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus

__all__ = [
    "Patient",
    "PatientStatus",
    "router",
]
