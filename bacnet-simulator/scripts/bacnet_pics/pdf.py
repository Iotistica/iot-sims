from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pdfplumber
import re


@dataclass(frozen=True)
class PdfPage:
    number: int
    text: str


def extract_pages(pdf_path: str | Path) -> list[PdfPage]:
    path = Path(pdf_path)

    if not path.exists():
        raise FileNotFoundError(path)

    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF file, got: {path}")

    pages: list[PdfPage] = []

    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(
                x_tolerance=2,
                y_tolerance=3,
            ) or ""

            pages.append(
                PdfPage(
                    number=index,
                    text=text.strip(),
                )
            )

    return pages


def looks_like_object_page(text: str) -> bool:
    lowered = text.lower()

    return (
        "property name" in lowered
        and (
            "r=required" in lowered
            or "o=optional" in lowered
            or "p=proprietary" in lowered
        )
    )


def looks_like_object_continuation(text: str) -> bool:
    """
    Detect a continuation page that does not repeat the full table heading.
    """
    lowered = text.lower()

    return (
        "property name" not in lowered
        and (
            "; adt" in lowered
            or "property_list" in lowered
            or "event_detection_enable" in lowered
        )
    )


def extract_object_heading(text: str) -> str | None:
    """
    Extract the object heading appearing before 'Property name'.

    For the Honeywell document this returns values such as:
        Analog Input
        Binary Output
        Binary Value
        Device
    """
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    property_index: int | None = None

    for index, line in enumerate(lines):
        if line.lower().startswith("property name"):
            property_index = index
            break

    if property_index is None:
        return None

    ignored_prefixes = (
        "irm ",
        "pics",
        "bacnet protocol",
        "page ",
    )

    candidates: list[str] = []

    for line in lines[:property_index]:
        lowered = line.lower()

        if lowered.startswith(ignored_prefixes):
            continue

        if "r=required" in lowered:
            continue

        if "o=optional" in lowered:
            continue

        if "p=proprietary" in lowered:
            continue

        candidates.append(line)

    if not candidates:
        return None

    # The heading is normally the last meaningful line before the column header.
    return candidates[-1]


def has_document_summary_content(text: str) -> bool:
    lowered = text.lower()

    markers = (
        "vendor name:",
        "product name:",
        "bacnet interoperability building blocks",
        "standard object types supported",
        "segmentation capability",
        "data link layer options",
        "character sets supported",
        "bacnet standardized device profile",
        "bacnet advanced application controller",
        "bacnet application specific controller",
        "bacnet smart sensor",
        "bacnet smart actuator",
    )


    return any(marker in lowered for marker in markers)


def extract_legacy_object_headings(text: str) -> list[str]:
    """
    Detect legacy headings such as:

        Analog Input Object:
        Binary Output Object
        Accumulatore Object
        Multi-State Value Object:

    The trailing colon is optional because PDF extraction may drop it.
    """
    import re

    headings: list[str] = []

    replacements = {
        "accumulatore": "Accumulator",
        "analog input": "Analog Input",
        "analog output": "Analog Output",
        "analog value": "Analog Value",
        "binary input": "Binary Input",
        "binary output": "Binary Output",
        "binary value": "Binary Value",
        "calendar": "Calendar",
        "device": "Device",
        "file": "File",
        "multi-state input": "Multistate Input",
        "multi-state output": "Multistate Output",
        "multi-state value": "Multistate Value",
        "multistate input": "Multistate Input",
        "multistate output": "Multistate Output",
        "multistate value": "Multistate Value",
        "notification class": "Notification Class",
        "schedule": "Schedule",
    }

    pattern = re.compile(
        r"^\s*([A-Za-z][A-Za-z0-9 _\-]+?)\s+Object\s*:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    for match in pattern.finditer(text):
        raw_name = match.group(1).strip()
        normalized = (
            raw_name
            .replace("_", " ")
            .strip()
            .casefold()
        )

        heading = replacements.get(normalized)

        if heading and heading not in headings:
            headings.append(heading)

    return headings

def looks_like_legacy_object_page(text: str) -> bool:
    lowered = text.casefold()

    has_property_columns = (
        "readable properties" in lowered
        and "writable properties" in lowered
    )

    has_object_heading = bool(
        extract_legacy_object_headings(text)
    )

    return has_property_columns or has_object_heading

def looks_like_legacy_continuation(text: str) -> bool:
    lowered = text.casefold()

    property_markers = (
        "reliability",
        "event_time_stamps",
        "priority_array",
        "relinquish_default",
        "protocol_version",
        "notification_class",
        "alarm_values",
        "fault_values",
    )

    return (
        not extract_legacy_object_headings(text)
        and any(marker in lowered for marker in property_markers)
        and (
            "readable properties" in lowered
            or "writable properties" in lowered
        )
    )
