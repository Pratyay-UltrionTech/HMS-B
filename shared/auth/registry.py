"""
Canonical Module and Action Registry.

Defines the authoritative set of 26 HMS modules, supported granular actions,
database column flag mappings, and permission bundle templates.
Ensures zero naming drift across routers, services, and frontend consumers.
"""

from typing import Any, NamedTuple


class ModuleDefinition(NamedTuple):
    key: str
    display_name: str
    supported_actions: tuple[str, ...]
    category: str


ACTION_FLAG_MAP: dict[str, str] = {
    "view": "can_view",
    "edit": "can_edit",
    "create": "can_create",
    "delete": "can_delete",
    "approve": "can_approve",
    "validate": "can_validate",
    "release": "can_release",
    "dispense": "can_dispense",
    "refund": "can_refund",
    "cancel": "can_cancel",
    "administer": "can_administer",
}

ALL_ACTIONS = tuple(ACTION_FLAG_MAP.keys())

# Canonical registry of all 26 HMS modules
MODULE_REGISTRY: dict[str, ModuleDefinition] = {
    "masters": ModuleDefinition("masters", "Masters Management", ("view", "edit", "create", "delete"), "Administrative"),
    "admin": ModuleDefinition("admin", "Admin Management", ("view", "edit", "create", "delete"), "Administrative"),
    "doctors": ModuleDefinition("doctors", "Doctors Management", ("view", "edit", "create", "delete"), "Clinical"),
    "nurses": ModuleDefinition("nurses", "Nurses Management", ("view", "edit", "create", "delete"), "Clinical"),
    "registration": ModuleDefinition("registration", "Patient Registration", ("view", "edit", "create", "delete"), "Patient Access"),
    "appointment": ModuleDefinition("appointment", "Appointments", ("view", "edit", "create", "delete", "cancel"), "Patient Access"),
    "vitals": ModuleDefinition("vitals", "Vitals & Triage", ("view", "edit", "create"), "Clinical"),
    "bed": ModuleDefinition("bed", "Bed Management", ("view", "edit", "create"), "Inpatient"),
    "all_ipd": ModuleDefinition("all_ipd", "Inpatient Operations (IPD)", ("view", "edit", "create", "approve"), "Inpatient"),
    "laboratory": ModuleDefinition("laboratory", "Laboratory Services", ("view", "edit", "create", "validate", "release"), "Diagnostic"),
    "radiology": ModuleDefinition("radiology", "Radiology & Imaging", ("view", "edit", "create", "validate", "release"), "Diagnostic"),
    "ot": ModuleDefinition("ot", "Operation Theatre", ("view", "edit", "create"), "Surgical"),
    "dms": ModuleDefinition("dms", "Document Management", ("view", "edit", "create", "delete"), "Records"),
    "equipment": ModuleDefinition("equipment", "Biomedical Equipment", ("view", "edit", "create"), "Operations"),
    "mis": ModuleDefinition("mis", "Management Information System", ("view",), "Analytics"),
    "billing": ModuleDefinition("billing", "Billing & Cashier", ("view", "edit", "create", "cancel", "refund", "approve"), "Financial"),
    "pharmacy": ModuleDefinition("pharmacy", "Pharmacy Operations", ("view", "edit", "create", "dispense"), "Pharmacy"),
    "emergency": ModuleDefinition("emergency", "Emergency Department", ("view", "edit", "create", "administer"), "Emergency"),
    "critical_care": ModuleDefinition("critical_care", "Critical Care & ICU", ("view", "edit", "create", "administer"), "Inpatient"),
    "ambulance": ModuleDefinition("ambulance", "Ambulance Fleet", ("view", "edit", "create"), "Logistics"),
    "blood_bank": ModuleDefinition("blood_bank", "Blood Bank", ("view", "edit", "create", "release", "approve"), "Diagnostic"),
    "cssd": ModuleDefinition("cssd", "Central Sterile Services (CSSD)", ("view", "edit", "create"), "Operations"),
    "inventory": ModuleDefinition("inventory", "Central Inventory", ("view", "edit", "create"), "Operations"),
    "procurement": ModuleDefinition("procurement", "Procurement & Purchase", ("view", "edit", "create", "approve"), "Operations"),
    "clinical_decision": ModuleDefinition("clinical_decision", "Clinical Decision Support", ("view", "edit"), "Clinical"),
    "insurance": ModuleDefinition("insurance", "TPA & Insurance Claims", ("view", "edit", "create", "approve"), "Financial"),
}


MODULE_ALIASES: dict[str, str] = {
    "appointments": "appointment",
    "patients": "registration",
    "patient": "registration",
    "ipd": "all_ipd",
    "clinical": "doctors",
}


def parse_action(action_spec: str | tuple[str, str]) -> tuple[str, str, str]:
    """
    Parse action specification into (module_key, action_name, db_flag).
    Accepts:
      - 'laboratory.validate' (canonical)
      - 'laboratory:validate' (colon delimiter)
      - ('laboratory', 'validate') (tuple)
      - 'laboratory' (defaults action to 'view')
    """
    if isinstance(action_spec, tuple):
        module_key, action_name = action_spec[0].strip().lower(), action_spec[1].strip().lower()
    elif "." in action_spec:
        parts = action_spec.strip().split(".", 1)
        module_key, action_name = parts[0].strip().lower(), parts[1].strip().lower()
    elif ":" in action_spec:
        parts = action_spec.strip().split(":", 1)
        module_key, action_name = parts[0].strip().lower(), parts[1].strip().lower()
    else:
        module_key = action_spec.strip().lower()
        action_name = "view"

    module_key = MODULE_ALIASES.get(module_key, module_key)
    db_flag = ACTION_FLAG_MAP.get(action_name, f"can_{action_name}")
    return module_key, action_name, db_flag


# Reusable permission bundle templates for standard hospital roles
ROLE_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "doctor": {
        "doctors": ["view", "edit"],
        "appointment": ["view", "edit", "create", "cancel"],
        "vitals": ["view", "edit", "create"],
        "all_ipd": ["view", "edit", "create"],
        "laboratory": ["view", "create"],
        "radiology": ["view", "create"],
        "pharmacy": ["view", "create"],
        "clinical_decision": ["view"],
    },
    "nurse": {
        "nurses": ["view", "edit"],
        "vitals": ["view", "edit", "create"],
        "bed": ["view", "edit"],
        "all_ipd": ["view", "edit"],
        "emergency": ["view", "administer"],
        "critical_care": ["view", "administer"],
    },
    "lab_technician": {
        "laboratory": ["view", "create", "edit"],
    },
    "pathologist": {
        "laboratory": ["view", "create", "edit", "validate", "release"],
    },
    "radiologist": {
        "radiology": ["view", "create", "edit", "validate", "release"],
    },
    "pharmacist": {
        "pharmacy": ["view", "create", "edit", "dispense"],
    },
    "billing_cashier": {
        "billing": ["view", "create", "edit"],
        "registration": ["view"],
    },
    "billing_manager": {
        "billing": ["view", "create", "edit", "cancel", "refund", "approve"],
    },
}
