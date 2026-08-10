from __future__ import annotations

from fastapi import HTTPException


def reject_external_device(device: dict) -> None:
    """
    Blocks any simulator write/mutation operation against an external
    BACnet device's project-inventory row. "Read-only" here means no
    BACnet actions and no simulation ownership -- it does NOT mean the
    inventory record itself is immutable; DELETE /devices/{id} deliberately
    does not call this (removing the record performs no BACnet action).
    """
    if device.get("source_type") == "external-bacnet":
        raise HTTPException(
            status_code=403,
            detail=(
                "This device is an external BACnet device (read-only) -- "
                "simulator write/mutation operations are not permitted."
            ),
        )


# Fields that mirror the real physical device or simulator ownership --
# these stay locked for an external-BACnet device's own row. Everything else
# (name, description, location_id, equipment_type,
# can_receive_event_notifications) is project-local metadata and may change
# freely -- see reject_external_source_mutation()'s docstring.
_EXTERNAL_PROTECTED_FIELDS = (
    "device_instance",
    "vendor_name",
    "model_name",
    "firmware_revision",
    "protocol_revision",
    "max_apdu_length_accepted",
    "segmentation_supported",
    "enabled",
)


def reject_external_source_mutation(existing: dict, body_dict: dict) -> None:
    """
    Distinguishes source mutations (prohibited) from project-model
    mutations (allowed) for an external-BACnet device's own row. Unlike
    reject_external_device() above, this only blocks fields that mirror the
    real physical device or simulator ownership -- location_id,
    equipment_type, can_receive_event_notifications, name, and description
    are project-local metadata and may change freely (e.g. assigning a
    Location, or renaming the device for display purposes here, never
    touches the physical device or the BACnet network).
    """
    if existing.get("source_type") != "external-bacnet":
        return
    for field in _EXTERNAL_PROTECTED_FIELDS:
        if field in body_dict and body_dict[field] != existing.get(field):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Cannot modify '{field}' on an external BACnet device "
                    "-- it reflects the real physical device."
                ),
            )
