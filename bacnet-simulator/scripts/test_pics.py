"""
Simple BACnet PICS extractor using Azure OpenAI.

Reads a Honeywell PICS PDF, finds the Analog Input page,
sends it to Azure OpenAI (GPT-4.1-mini),
and returns structured JSON.

Author: ChatGPT
"""

from __future__ import annotations

import os
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv
from openai import AzureOpenAI
from pydantic import BaseModel, Field


###########################################################################
# Configuration
###########################################################################

PDF_FILE = r"C:\Users\Dan\Documents\iotistica\bacnet\BACnet  IP MSTP FCU CONTROLLER-PICS.pdf"
SEARCH_HEADING = "Analog Input"

load_dotenv()

ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
API_KEY = os.environ["AZURE_OPENAI_API_KEY"]
DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]


###########################################################################
# Azure OpenAI
###########################################################################

client = AzureOpenAI(
    azure_endpoint=ENDPOINT,
    api_key=API_KEY,
    api_version="2024-10-21",
)


###########################################################################
# Structured Output Models
###########################################################################


class Property(BaseModel):
    name: str

    requirement: str

    writable: bool | None = None

    property_id: int | None = None

    datatype: str | None = None

    range: str | None = None

    remarks: str | None = None


class ObjectType(BaseModel):
    object_type: str

    properties: list[Property] = Field(default_factory=list)

    warnings: list[str] = Field(default_factory=list)


###########################################################################
# PDF Helpers
###########################################################################


def find_page(pdf_path: str, heading: str) -> tuple[int, str]:
    with pdfplumber.open(pdf_path) as pdf:

        found_summary = False

        for index, page in enumerate(pdf.pages):

            text = page.extract_text() or ""

            if heading.lower() in text.lower():

                # Skip the summary page
                if "Standard Object Types Supported" in text:
                    found_summary = True
                    continue

                # This looks like the actual object page
                if "Property name" in text:
                    return index + 1, text

        raise RuntimeError(f"{heading} not found.")


###########################################################################
# Main
###########################################################################


def main():

    pdf = Path(PDF_FILE)

    if not pdf.exists():
        raise FileNotFoundError(pdf)

    page_number, page_text = find_page(
        PDF_FILE,
        SEARCH_HEADING,
    )

    print("=" * 80)
    print(page_text)
    print("=" * 80)

    print()
    print("=" * 80)
    print(f"Found '{SEARCH_HEADING}' on page {page_number}")
    print("=" * 80)
    print()

    system_prompt = """
You are an expert BACnet engineer.

You extract BACnet PICS information.

Rules:

- Never invent information.
- Use ONLY information explicitly present.
- Preserve proprietary properties.
- Preserve property IDs.
- Preserve datatype.
- Preserve ranges.
- Preserve remarks.

Requirement mapping:

R = required
O = optional
P = proprietary

If writable is not stated, return null.

If datatype is not stated, return null.

If range is not stated, return null.

If something looks inconsistent,
add a warning instead of correcting it.
"""

    user_prompt = f"""
Extract ONLY the Analog Input object.

Return structured data.

Page {page_number}

----------------------------

{page_text}

----------------------------
"""

    response = client.beta.chat.completions.parse(
        model=DEPLOYMENT,
        temperature=0,
        response_format=ObjectType,
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
    )

    result = response.choices[0].message.parsed

    if result is None:
        raise RuntimeError("No structured response returned.")

    print(result.model_dump_json(indent=4))

    with open(
        "analog_input.json",
        "w",
        encoding="utf8",
    ) as fp:
        fp.write(result.model_dump_json(indent=4))

    print()
    print("Saved to analog_input.json")


###########################################################################

if __name__ == "__main__":
    main()