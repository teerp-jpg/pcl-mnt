"""Register the complete MTN schema without connecting to a database."""

from app.models.assets import (
    CoreExportBatch,
    Vehicle,
    VehicleChange,
    VehicleImportBatch,
    VehicleImportRow,
    VehiclePlateHistory,
)
from app.models.identity import AppUser, AuditLog, SystemSetting
from app.models.maintenance import (
    MaintenanceAttachment,
    MaintenanceLine,
    MaintenanceRecord,
    OdometerReading,
    PMPlan,
)
from app.models.master import Garage, MaintenanceCategory

__all__ = [
    "AppUser",
    "AuditLog",
    "CoreExportBatch",
    "Garage",
    "MaintenanceAttachment",
    "MaintenanceCategory",
    "MaintenanceLine",
    "MaintenanceRecord",
    "OdometerReading",
    "PMPlan",
    "SystemSetting",
    "Vehicle",
    "VehicleChange",
    "VehicleImportBatch",
    "VehicleImportRow",
    "VehiclePlateHistory",
]
