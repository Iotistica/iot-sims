\
from __future__ import annotations

from pathlib import Path

from .azure import AzureStructuredClient
from .models import (
    DocumentSummary,
    PageObjects,
    PicsObject,
    PicsProfile,
)
from .pdf import (
    PdfPage,
    extract_legacy_object_headings,
    extract_object_heading,
    extract_pages,
    has_document_summary_content,
    looks_like_legacy_object_page,
    looks_like_object_continuation,
    looks_like_object_page,
)
from .prompts import OBJECT_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT

from .models import (
    DocumentSummary,
    ObjectSupport,
    PageObjects,
    PicsObject,
    PicsProfile,
)

from .pdf import (
    PdfPage,
    extract_legacy_object_headings,
    extract_object_heading,
    extract_pages,
    has_document_summary_content,
    looks_like_legacy_continuation,
    looks_like_legacy_object_page,
    looks_like_object_continuation,
    looks_like_object_page,
)




class PicsParser:
    def __init__(
        self,
        pdf_path: str | Path,
        *,
        deployment: str | None = None,
        verbose: bool = True,
    ) -> None:
        self.pdf_path = Path(pdf_path)
        self.verbose = verbose
        self.ai = AzureStructuredClient(deployment=deployment)

    def parse(self) -> PicsProfile:
        pages = extract_pages(self.pdf_path)
        if self.verbose:
            print(f"Loaded {len(pages)} pages from {self.pdf_path.name}")

        summary = self._parse_summary(pages)
        objects = self._parse_object_pages(pages)
        objects = self._merge_duplicate_objects(objects)

        profile = PicsProfile(
            source_file=str(self.pdf_path),
            page_count=len(pages),
            identity=summary.identity,
            bibbs=summary.bibbs,
            standard_objects=summary.standard_objects,
            objects=objects,
            network=summary.network,
            warnings=summary.warnings,
        )
        self._validate(profile)
        return profile

    def _parse_summary(
        self,
        pages: list[PdfPage],
    ) -> DocumentSummary:
        selected = [
            page
            for page in pages
            if has_document_summary_content(page.text)
        ]

        content = "\n\n".join(
            f"--- PAGE {page.number} ---\n{page.text}"
            for page in selected
        )

        if self.verbose:
            print(
                "Extracting document summary from pages: "
                + ", ".join(str(page.number) for page in selected)
            )

        summary = self.ai.parse(
            response_model=DocumentSummary,
            system_prompt=SUMMARY_SYSTEM_PROMPT,
            user_prompt=(
                "Extract identity, device profiles, BIBBs, the standard object "
                "support list, segmentation, data-link/networking options, and "
                f"character sets from these PICS pages:\n\n{content}"
            ),
        )

        if not summary.identity.device_profiles:
            profile_map = {
                "BACnet Operator Workstation (B-OWS)": "B-OWS",
                "BACnet Building Controller (B-BC)": "B-BC",
                "BACnet Advanced Application Controller (B-AAC)": "B-AAC",
                "BACnet Application Specific Controller (B-ASC)": "B-ASC",
                "BACnet Smart Sensor (B-SS)": "B-SS",
                "BACnet Smart Actuator (B-SA)": "B-SA",
            }

            selected_profiles: list[str] = []

            print("\n====================")
            print("DEVICE PROFILE TEXT")
            print("====================")

            for page in selected:
                if (
                    "BACnet Standardized Device Profile" in page.text
                    or "B-AAC" in page.text
                ):
                    print(f"\n--- PAGE {page.number} ---")
                    print(page.text)

            for page in selected:
                for line in page.text.splitlines():
                    stripped = line.strip()

                    # Temporary Honeywell checkbox check.
                    if not stripped.startswith(""):
                        continue

                    for label, code in profile_map.items():
                        if label in stripped:
                            selected_profiles.append(code)

            summary.identity.device_profiles = sorted(
                set(selected_profiles)
            )

        return summary
    
    def _parse_object_pages(
    self,
    pages: list[PdfPage],
    ) -> list[PicsObject]:
        results: list[PicsObject] = []
        previous_heading: str | None = None

        for page in pages:
            is_modern_page = looks_like_object_page(page.text)
            is_legacy_page = looks_like_legacy_object_page(page.text)
            is_continuation = (
                looks_like_object_continuation(page.text)
                or looks_like_legacy_continuation(page.text)
            )

            if not is_modern_page and not is_legacy_page and not is_continuation:
                previous_heading = None
                continue

            if is_modern_page:
                detected_headings = [
                    heading
                    for heading in [extract_object_heading(page.text)]
                    if heading
                ]
            elif is_legacy_page:
                detected_headings = extract_legacy_object_headings(page.text)
            else:
                detected_headings = (
                    [previous_heading]
                    if previous_heading
                    else []
                )

            print(
                f"Legacy headings page {page.number}: "
                f"{extract_legacy_object_headings(page.text)}"
            )

            if self.verbose:
                heading_display = (
                    ", ".join(detected_headings)
                    if detected_headings
                    else "unknown"
                )

                print(
                    f"Extracting object table(s) from page "
                    f"{page.number}: {heading_display}"
                )

            parsed = self.ai.parse(
                response_model=PageObjects,
                system_prompt=OBJECT_SYSTEM_PROMPT,
                user_prompt=(
                    f"Page number: {page.number}\n"
                    f"Document table format: "
                    f"{'legacy readable/writable columns' if is_legacy_page else 'modern PICS property table'}\n"
                    f"Expected object headings: "
                    f"{', '.join(detected_headings) or 'unknown'}\n"
                    f"Is continuation page: {is_continuation}\n\n"
                    f"{page.text}"
                ),
            )

            for index, obj in enumerate(parsed.objects):
                if index < len(detected_headings):
                    obj.object_type = detected_headings[index]
                elif len(detected_headings) == 1:
                    obj.object_type = detected_headings[0]

                obj.source_pages = sorted(
                    set(obj.source_pages + [page.number])
                )

                self._normalize_object(obj)
                results.append(obj)

            if parsed.objects:
                previous_heading = parsed.objects[-1].object_type
            elif detected_headings:
                previous_heading = detected_headings[-1]

        return results
    
    @staticmethod
    def _normalize_object(obj: PicsObject) -> None:
        standard_object_ids = {
            "accumulator": 23,
            "analog input": 0,
            "analog output": 1,
            "analog value": 2,
            "analog value (no priority array)": 2,
            "analog value (with priority array)": 2,
            "binary input": 3,
            "binary output": 4,
            "binary value": 5,
            "binary value (no priority array)": 5,
            "binary value (with priority array)": 5,
            "calendar": 6,
            "device": 8,
            "file": 10,
            "multistate input": 13,
            "multistate output": 14,
            "multistate value": 19,
            "multistate value (no priority array)": 19,
            "multistate value (with priority array)": 19,
            "notification class": 15,
            "schedule": 17,
        }

        

        normalized_name = (
            obj.object_type
            .strip()
            .replace("-", " ")
            .casefold()
        )

        expected_id = standard_object_ids.get(normalized_name)

        if expected_id is not None:
            if (
                obj.object_type_id is not None
                and obj.object_type_id != expected_id
            ):
                obj.warnings.append(
                    f"Corrected object_type_id from "
                    f"{obj.object_type_id} to {expected_id}."
                )

            obj.object_type_id = expected_id
            obj.proprietary = False

        elif (
            obj.object_type_id is not None
            and obj.object_type_id >= 128
        ):
            # Vendor-defined BACnet object type.
            obj.proprietary = True

        for prop in obj.properties:
            normalized_property_name = (
                prop.name
                .strip()
                .replace(" ", "_")
                .casefold()
            )

            if normalized_property_name == "object_type":
                prop.property_id = None
                prop.range = None

                if obj.object_type_id is not None:
                    prop.remarks = str(obj.object_type_id)

            if prop.requirement == "W":
                prop.writable = True

    @staticmethod
    def _merge_duplicate_objects(
        objects: list[PicsObject],
    ) -> list[PicsObject]:
        merged: dict[tuple[str, int | None, bool], PicsObject] = {}

        for obj in objects:
            key = (
                obj.object_type.strip().casefold(),
                obj.object_type_id,
                obj.proprietary,
            )

            if key not in merged:
                merged[key] = obj.model_copy(deep=True)
                continue

            target = merged[key]

            target.source_pages = sorted(
                set(target.source_pages + obj.source_pages)
            )

            for warning in obj.warnings:
                if warning not in target.warnings:
                    target.warnings.append(warning)

            existing_properties = {
                prop.name.strip().replace(" ", "_").casefold(): prop
                for prop in target.properties
            }

            for prop in obj.properties:
                prop_key = (
                    prop.name
                    .strip()
                    .replace(" ", "_")
                    .casefold()
                )

                if prop_key not in existing_properties:
                    target.properties.append(prop)
                    existing_properties[prop_key] = prop
                    continue

                existing = existing_properties[prop_key]

                for field in (
                    "writable",
                    "property_id",
                    "datatype",
                    "range",
                    "remarks",
                ):
                    existing_value = getattr(existing, field)
                    new_value = getattr(prop, field)

                    if existing_value is None and new_value is not None:
                        setattr(existing, field, new_value)

        return list(merged.values())

    @staticmethod
    def _validate(profile: PicsProfile) -> None:
        standard_object_ids = {
            "analog input": 0,
            "analog output": 1,
            "analog value": 2,
            "binary input": 3,
            "binary output": 4,
            "binary value": 5,
            "calendar": 6,
            "command": 7,
            "device": 8,
            "event enrollment": 9,
            "file": 10,
            "group": 11,
            "loop": 12,
            "multi-state input": 13,
            "multi-state output": 14,
            "notification class": 15,
            "program": 16,
            "schedule": 17,
            "averaging": 18,
            "multi-state value": 19,
            "trend log": 20,
            "life safety point": 21,
            "life safety zone": 22,
            "accumulator": 23,
        }

        seen: set[str] = set()

        for obj in profile.objects:
            normalized_object_name = obj.object_type.strip().casefold()
            expected_id = standard_object_ids.get(normalized_object_name)

            if (
                expected_id is not None
                and obj.object_type_id is not None
                and obj.object_type_id != expected_id
            ):
                obj.warnings.append(
                    f"Object heading/type mismatch: "
                    f"{obj.object_type!r} normally has object type ID "
                    f"{expected_id}, but the extraction produced "
                    f"{obj.object_type_id}."
                )

            key = obj.object_type.casefold()

            if key in seen:
                profile.warnings.append(
                    f"Duplicate object section remains after merge: "
                    f"{obj.object_type}"
                )

            seen.add(key)

            property_names: set[str] = set()

            for prop in obj.properties:
                pkey = prop.name.casefold()

                if pkey in property_names:
                    obj.warnings.append(
                        f"Duplicate property row: {prop.name}"
                    )

                property_names.add(pkey)

                if prop.requirement == "P" and prop.property_id is None:
                    obj.warnings.append(
                        f"Proprietary property has no ID: {prop.name}"
                    )

                supported_names = {
                item.object_type.strip().casefold()
                for item in profile.standard_objects
            }

            for obj in profile.objects:
                normalized_name = obj.object_type.strip().casefold()

                if obj.proprietary:
                    continue

                if normalized_name not in supported_names:
                    profile.standard_objects.append(
                        ObjectSupport(
                            object_type=obj.object_type,
                            dynamically_creatable=None,
                            dynamically_deletable=None,
                        )
                    )
                    supported_names.add(normalized_name)