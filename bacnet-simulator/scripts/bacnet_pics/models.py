\
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


Requirement = Literal["R", "O", "P", "W", "unknown"]


class Evidence(BaseModel):
    page: int
    text: str | None = None


class PicsProperty(BaseModel):
    name: str
    requirement: Requirement = "unknown"
    writable: bool | None = None
    property_id: int | None = None
    datatype: str | None = None
    range: str | None = None
    remarks: str | None = None


class PicsObject(BaseModel):
    object_type: str
    object_type_id: int | None = None
    proprietary: bool = False
    properties: list[PicsProperty] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_pages: list[int] = Field(default_factory=list)


class Identity(BaseModel):
    vendor_name: str | None = None
    product_name: str | None = None
    product_model_numbers: list[str] = Field(default_factory=list)
    application_software_version: str | None = None
    firmware_revision: str | None = None
    protocol_version: str | None = None
    protocol_revision: str | None = None
    product_description: str | None = None
    device_profiles: list[str] = Field(default_factory=list)


class Bibb(BaseModel):
    code: str
    description: str | None = None
    category: str | None = None


class ObjectSupport(BaseModel):
    object_type: str
    dynamically_creatable: bool | None = None
    dynamically_deletable: bool | None = None


class Segmentation(BaseModel):
    segmented_requests_supported: bool | None = None
    segmented_responses_supported: bool | None = None
    request_window_size: int | None = None
    response_window_size: int | None = None


class NetworkCapabilities(BaseModel):
    data_link_options: list[str] = Field(default_factory=list)
    static_device_binding: bool | None = None
    networking_options: list[str] = Field(default_factory=list)
    character_sets: list[str] = Field(default_factory=list)
    segmentation: Segmentation = Field(default_factory=Segmentation)


class DocumentSummary(BaseModel):
    identity: Identity = Field(default_factory=Identity)
    bibbs: list[Bibb] = Field(default_factory=list)
    standard_objects: list[ObjectSupport] = Field(default_factory=list)
    network: NetworkCapabilities = Field(default_factory=NetworkCapabilities)
    warnings: list[str] = Field(default_factory=list)


class PageObjects(BaseModel):
    objects: list[PicsObject] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PicsProfile(BaseModel):
    source_file: str
    page_count: int
    identity: Identity = Field(default_factory=Identity)
    bibbs: list[Bibb] = Field(default_factory=list)
    standard_objects: list[ObjectSupport] = Field(default_factory=list)
    objects: list[PicsObject] = Field(default_factory=list)
    network: NetworkCapabilities = Field(default_factory=NetworkCapabilities)
    warnings: list[str] = Field(default_factory=list)
    document_layout: Literal["modern", "legacy_read_write", "mixed", "unknown"] = "unknown"
    status: Literal["accepted", "needs_review", "failed"] = "accepted"
    confidence: float = 1.0
