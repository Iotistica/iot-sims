#!/usr/bin/env python3
"""
Scrapes the BACnet International BTL product database.
Also optionally downloads and parses PICS PDFs for supported object types.

Key findings from debug run:
- Manufacturer list: select[name='manu'] on base page (server-rendered)
- jQuery reveals real filter URL: index.php?m=<numeric_id>
- Product pages (index.php?m=<id>) are server-rendered — no JS/Playwright needed
- Structure: <h4> headings name the profile type (B-BC, B-AAC, etc.)
             <table> rows below each h4 contain the product names + PICS links

Usage:
    pip install requests beautifulsoup4
    pip install pdfplumber          # only needed for --parse-pics / --update-pics

    # Full scrape (capture PICS URLs, no PDF parsing):
    python scrape-btl.py

    # Scrape + parse PDFs in one pass:
    python scrape-btl.py --parse-pics

    # Test on a single manufacturer (numeric BTL ID, e.g. 23 = Siemens):
    python scrape-btl.py --manufacturer 23 --parse-pics --output /tmp/test.json

    # Parse PDFs for products already in the JSON (no re-scrape):
    python scrape-btl.py --update-pics [--manufacturer 23]
"""

import argparse
import io
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)

# Product/vendor names scraped from the BTL site can contain characters
# (non-breaking hyphens, accented letters, the registered-trademark sign)
# that aren't representable in the Windows console's default cp1252
# encoding. A print() crash mid-run means nothing gets written to disk (the
# output is only persisted at the very end) — so make stdout/stderr tolerant
# instead of letting an unencodable character abort the whole scrape.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "https://www.bacnetinternational.net/btl/"
MFR_URL  = "https://www.bacnetinternational.net/btl/index.php?m={id}"

PROFILE_LABELS = {
    "B-AWS": "Workstation", "B-OWS": "Operator Workstation",
    "B-OD": "Operator Display", "B-LOD": "Lightweight Operator Display",
    "B-BC": "Building Controller", "B-AAC": "Advanced Application Controller",
    "B-ASC": "Application Specific Controller",
    "B-LSC": "Lighting Application Specific Controller",
    "B-SS": "Smart Sensor", "B-SA": "Smart Actuator",
    "B-RTR": "Router", "B-GW": "Gateway", "B-BBMD": "BBMD",
    "B-LD": "Lighting Director", "B-GEN": "Generic", "B-SCHUB": "SC Hub",
}

PROFILE_RE = re.compile(
    r'\b(B-(?:AWS|OWS|OD|LOD|XAWS|ALSWS|AACWS|BC|AAC|ASC|LSC|LD|SS|SA|RTR|GW|BBMD|GEN|SCHUB))\b'
)

# BACnet object type patterns → short codes used in the output JSON
OBJECT_TYPES: list[tuple[str, str]] = [
    (r"analog[\s\-]?input",         "AI"),
    (r"analog[\s\-]?output",        "AO"),
    (r"analog[\s\-]?value",         "AV"),
    (r"binary[\s\-]?input",         "BI"),
    (r"binary[\s\-]?output",        "BO"),
    (r"binary[\s\-]?value",         "BV"),
    (r"multi[\s\-]?state[\s\-]?input",  "MSI"),
    (r"multi[\s\-]?state[\s\-]?output", "MSO"),
    (r"multi[\s\-]?state[\s\-]?value",  "MSV"),
    (r"calendar",                   "Calendar"),
    (r"schedule",                   "Schedule"),
    (r"\bloop\b",                   "Loop"),
    (r"trend[\s\-]?log",            "TL"),
    (r"event[\s\-]?enrollment",     "EE"),
    (r"notification[\s\-]?class",   "NC"),
    (r"\bprogram\b",                "Program"),
    (r"\bcommand\b",                "Command"),
    (r"\bfile\b",                   "File"),
    (r"\bdevice\b",                 "Device"),
    (r"network[\s\-]?port",         "NP"),
]

# BTL "Product Listing" certificates (bacnetinternational.net) list only the
# object types a product supports under this heading — no per-item "Yes"/
# count marker like full vendor PICS documents use. Being listed here *is*
# the affirmative signal.
_OBJECT_TYPE_SUPPORT_HEADING = "object type support"
_OBJECT_TYPE_SUPPORT_TERMINATORS = (
    "data link layer options",
    "character set support",
    "special functionality",
    "routing capabilities",
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
})


# ---------------------------------------------------------------------------
# HTML scraping
# ---------------------------------------------------------------------------

def get_manufacturers() -> list[dict]:
    resp = SESSION.get(BASE_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    sel = soup.find("select", {"name": "manu"})
    if not sel:
        raise RuntimeError("Could not find select[name='manu'] on base page")
    return [
        {"id": opt["value"].strip(), "name": opt.text.strip()}
        for opt in sel.find_all("option")
        if opt.get("value", "").strip() and opt.text.strip() != "Filter by Manufacturer"
    ]


def get_products(mfr_id: str) -> list[dict]:
    """
    Fetch index.php?m=<id> and extract all products + PICS PDF links.
    Page structure: <h4> heading sets current profile (B-XX), then a <table>
    whose rows are products. Each row may have a link to a PICS PDF.
    """
    url = MFR_URL.format(id=mfr_id)
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    products: list[dict] = []
    current_profile = ""

    content = soup.find("div", id="content") or soup.find("main") or soup.body
    if not content:
        return products

    for elem in content.find_all(["h4", "h3", "h2", "table"]):
        tag = elem.name

        if tag in ("h4", "h3", "h2"):
            m = PROFILE_RE.search(elem.get_text())
            if m:
                current_profile = m.group(1)

        elif tag == "table":
            for row in elem.find_all("tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                name = cells[0].get_text(separator=" ", strip=True)
                if not name or len(name) < 3 or re.match(
                    r'^(product|model|name|type|version|n/a)$', name, re.I
                ):
                    continue

                entry: dict = {"name": name}

                if current_profile in PROFILE_LABELS:
                    entry["type"] = current_profile
                    entry["typeLabel"] = PROFILE_LABELS[current_profile]
                elif current_profile:
                    entry["type"] = current_profile
                if not current_profile:
                    pm = PROFILE_RE.search(row.get_text())
                    if pm:
                        p = pm.group(1)
                        entry["type"] = p
                        if p in PROFILE_LABELS:
                            entry["typeLabel"] = PROFILE_LABELS[p]

                # Capture links from the row. The "PICS" column is a JS
                # button (href="#") carrying the real, full vendor-submitted
                # PICS document in a data-path attribute — not every product
                # has one. The two .pdf hrefs are the auto-generated
                # short-form BTL Listing certificate, then the 1-page
                # Certificate of Conformance (always present as a pair).
                pdf_urls: list[str] = []
                for cell in cells:
                    for a in cell.find_all("a"):
                        data_path = a.get("data-path")
                        if data_path and "pics_url" not in entry:
                            entry["pics_url"] = data_path
                        href = a.get("href")
                        if href and href.lower().endswith(".pdf"):
                            pdf_urls.append(urljoin(url, href))

                if pdf_urls:
                    entry["listing_url"] = pdf_urls[0]
                if len(pdf_urls) > 1:
                    entry["certificate_url"] = pdf_urls[1]

                products.append(entry)

    return products


# ---------------------------------------------------------------------------
# PICS PDF parsing
# ---------------------------------------------------------------------------

def _match_object_type(text: str) -> str | None:
    """Return the short type code if text contains a known BACnet object type name."""
    for pattern, code in OBJECT_TYPES:
        if re.search(pattern, text, re.I):
            return code
    return None


def _parse_table(table: list[list]) -> dict[str, int | bool]:
    """Extract object types from a pdfplumber table."""
    result: dict[str, int | bool] = {}
    for row in (table or []):
        if not row:
            continue
        first = str(row[0] or "").strip()
        if not first:
            continue
        code = _match_object_type(first)
        if not code:
            continue

        count: int | None = None
        supported = False
        for cell in row[1:]:
            val = str(cell or "").strip()
            if re.match(r"^(yes|supported|x|required|true)$", val, re.I):
                supported = True
            elif re.match(r"^(no|not supported|false)$", val, re.I):
                supported = False
                break  # explicitly not supported — skip this type
            num_m = re.match(r"^(\d{1,4})$", val)
            if num_m and int(num_m.group(1)) > 0:
                count = int(num_m.group(1))
                supported = True

        if supported:
            result[code] = count if count is not None else True

    return result


def _parse_text(text: str, existing: dict | None = None) -> dict[str, int | bool]:
    """Scan PDF text lines for object type mentions near 'supported' or a count."""
    result: dict[str, int | bool] = {}
    seen = set(existing or {})

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        code = _match_object_type(stripped)
        if not code or code in seen:
            continue
        lower = stripped.lower()
        if re.search(r"not\s+support", lower):
            continue
        if re.search(r"\bsupport|\byes\b", lower):
            num_m = re.search(r"\b(\d{1,4})\b", stripped)
            val = int(num_m.group(1)) if num_m and 1 <= int(num_m.group(1)) <= 9999 else True
            result[code] = val
            seen.add(code)

    return result


def _extract_object_type_support_block(full_text: str) -> str:
    """
    Return the text between the "Object Type Support" heading and the next
    recognized section heading (or end of document). Scans the full,
    multi-page document text since the heading and its terminator can land
    on different pages.
    """
    lines = full_text.splitlines()
    start = None
    for i, raw_line in enumerate(lines):
        if raw_line.strip().casefold() == _OBJECT_TYPE_SUPPORT_HEADING:
            start = i + 1
            break
    if start is None:
        return ""

    end = len(lines)
    for i in range(start, len(lines)):
        stripped = lines[i].strip().casefold()
        if not stripped:
            continue
        if stripped in _OBJECT_TYPE_SUPPORT_TERMINATORS or re.match(r"^page\s+\d+\s+of\s+\d+$", stripped):
            end = i
            break

    return "\n".join(lines[start:end])


def _parse_object_type_support_section(full_text: str) -> dict[str, int | bool]:
    """
    BTL "Product Listing" certificates list supported object types as bare
    names grouped under an "Object Type Support" heading, with no per-item
    "Yes"/count marker the way full vendor PICS documents use — so every
    object type name found within that heading's block is supported.
    """
    block = _extract_object_type_support_block(full_text)
    if not block:
        return {}

    result: dict[str, int | bool] = {}
    for pattern, code in OBJECT_TYPES:
        if re.search(pattern, block, re.I):
            result[code] = True
    return result


def parse_pics_pdf(url: str) -> dict[str, int | bool]:
    """
    Download a PICS PDF and return supported object types.
    Returns {type_code: instance_count_or_True}.
    """
    try:
        import pdfplumber
    except ImportError:
        print("\n  ERROR: pip install pdfplumber", file=sys.stderr)
        return {}

    resp = SESSION.get(url, timeout=60)
    resp.raise_for_status()

    obj_types: dict[str, int | bool] = {}
    full_text_parts: list[str] = []

    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        for page in pdf.pages:
            for table in (page.extract_tables() or []):
                obj_types.update(_parse_table(table))
            text = page.extract_text() or ""
            obj_types.update(_parse_text(text, existing=obj_types))
            full_text_parts.append(text)

    for code, val in _parse_object_type_support_section("\n".join(full_text_parts)).items():
        obj_types.setdefault(code, val)

    return obj_types


# ---------------------------------------------------------------------------
# Scrape modes
# ---------------------------------------------------------------------------

def _object_types_source_url(product: dict) -> str | None:
    """
    The URL to feed the object-type extractor. Prefer the real, full
    vendor-submitted PICS document (pics_url) when the product has one;
    fall back to the short auto-generated BTL Listing certificate
    (listing_url) otherwise — both are handled by parse_pics_pdf.
    """
    return product.get("pics_url") or product.get("listing_url")


def _pics_stats(products: list[dict]) -> tuple[int, int, int]:
    """Return (products_with_real_pics, products_with_listing_only, products_with_object_types)."""
    return (
        sum(1 for p in products if "pics_url" in p),
        sum(1 for p in products if "pics_url" not in p and "listing_url" in p),
        sum(1 for p in products if "object_types" in p),
    )


def _load_existing_lookup(out_path: Path) -> dict[tuple[str, str], dict]:
    """
    Read a previously-written output file (if any) and index products by
    (vendor name, product name) so a fresh HTML scrape can carry forward
    already-parsed object_types/pics_error instead of discarding them.
    """
    if not out_path.exists():
        return {}
    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    lookup: dict[tuple[str, str], dict] = {}
    for vendor in data.get("vendors", []):
        for product in vendor.get("models", []):
            lookup[(vendor["name"], product["name"])] = product
    return lookup


def scrape(parse_pics: bool = False, only_mfr_id: str | None = None, out_path: Path | None = None) -> dict:
    """Full scrape of the BTL website."""
    vendors: dict[str, list[dict]] = {}
    existing = _load_existing_lookup(out_path) if out_path else {}

    print("Fetching manufacturer list…")
    manufacturers = get_manufacturers()
    if only_mfr_id:
        manufacturers = [m for m in manufacturers if m["id"] == only_mfr_id]
        if not manufacturers:
            print(f"ERROR: manufacturer id {only_mfr_id!r} not found", file=sys.stderr)
            sys.exit(1)
    print(f"{len(manufacturers)} manufacturer(s) to process")

    for i, mfr in enumerate(manufacturers, 1):
        name = mfr["name"]
        mid  = mfr["id"]
        print(f"  [{i}/{len(manufacturers)}] {name}", end="", flush=True)
        try:
            products = get_products(mid)
            seen: set[str] = set()
            unique = [p for p in products if p["name"] not in seen and not seen.add(p["name"])]  # type: ignore[func-returns-value]

            # Carry forward previously-parsed PICS data when the product's
            # source URL hasn't changed, so a plain re-scrape never throws
            # away work a prior --parse-pics/--update-pics run already did.
            for p in unique:
                prev = existing.get((name, p["name"]))
                if prev and _object_types_source_url(prev) == _object_types_source_url(p):
                    if "object_types" in prev:
                        p["object_types"] = prev["object_types"]
                    if "pics_error" in prev:
                        p["pics_error"] = prev["pics_error"]

            if parse_pics:
                for p in unique:
                    source_url = _object_types_source_url(p)
                    if not source_url:
                        continue
                    if "object_types" in p or "pics_error" in p:
                        continue  # already have a result carried forward
                    try:
                        ot = parse_pics_pdf(source_url)
                        if ot:
                            p["object_types"] = ot
                    except Exception as e:
                        p["pics_error"] = str(e)
                    time.sleep(0.5)

            if unique:
                vendors[name] = unique
                n_real, n_listing_only, n_parsed = _pics_stats(unique)
                tag = f" ({n_real} PICS, {n_listing_only} listing-only"
                if parse_pics:
                    tag += f", {n_parsed} parsed"
                tag += ")"
                print(f" → {len(unique)} products{tag}")
            else:
                print(" → 0")
            time.sleep(0.25)
        except Exception as e:
            print(f" → ERROR: {e}")

    return _build_output(vendors)


def update_pics(out_path: Path, only_mfr_name: str | None = None, force: bool = False) -> dict:
    """
    Read existing JSON, parse PDFs for products that have a pics_url or
    listing_url but no object_types yet (or all of them if --force).  No
    HTML re-scrape.
    """
    if not out_path.exists():
        print(f"ERROR: {out_path} not found — run without --update-pics first", file=sys.stderr)
        sys.exit(1)

    data = json.loads(out_path.read_text(encoding="utf-8"))
    total = 0
    updated = 0

    for vendor in data["vendors"]:
        if only_mfr_name and only_mfr_name.lower() not in vendor["name"].lower():
            continue
        for product in vendor["models"]:
            source_url = _object_types_source_url(product)
            if not source_url:
                continue
            if not force and "object_types" in product:
                continue
            total += 1
            print(f"  Parsing {vendor['name']} / {product['name']} …", end="", flush=True)
            try:
                ot = parse_pics_pdf(source_url)
                if ot:
                    product["object_types"] = ot
                    updated += 1
                    print(f" {list(ot.keys())}")
                else:
                    print(" (no object types found)")
            except Exception as e:
                product["pics_error"] = str(e)
                print(f" ERROR: {e}")
            time.sleep(0.5)

    data["updated"] = str(date.today())
    print(f"\nParsed {updated}/{total} PICS PDFs")
    return data


def _build_output(vendors: dict[str, list[dict]]) -> dict:
    return {
        "updated": str(date.today()),
        "source": "BACnet International BTL Database",
        "url": "https://www.bacnetinternational.net/btl/",
        "vendors": [
            {"name": n, "models": sorted(m, key=lambda x: x["name"])}
            for n, m in sorted(vendors.items()) if m
        ],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default=None, metavar="PATH",
                        help="Output JSON path (default: ../bacnet-vendors.json)")
    parser.add_argument("--parse-pics", action="store_true",
                        help="Download and parse PICS PDFs during scrape (needs pdfplumber)")
    parser.add_argument("--update-pics", action="store_true",
                        help="Parse PDFs for existing JSON entries without re-scraping")
    parser.add_argument("--force", action="store_true",
                        help="With --update-pics: re-parse even if object_types already set")
    parser.add_argument("--manufacturer", default=None, metavar="ID_OR_NAME",
                        help="Limit to one manufacturer. With --update-pics use name substring "
                             "(e.g. Siemens); with scrape mode use numeric BTL ID (e.g. 23)")
    args = parser.parse_args()

    out_path = Path(args.output) if args.output else Path(__file__).parent.parent / "bacnet-vendors.json"
    print(f"Output: {out_path}")

    if args.update_pics:
        data = update_pics(out_path, only_mfr_name=args.manufacturer, force=args.force)
    else:
        if args.parse_pics:
            print("PICS parsing enabled")
        data = scrape(parse_pics=args.parse_pics, only_mfr_id=args.manufacturer, out_path=out_path)

    vendor_count   = len(data["vendors"])
    product_count  = sum(len(v["models"]) for v in data["vendors"])
    real_pics      = sum(1 for v in data["vendors"] for m in v["models"] if "pics_url" in m)
    listing_only   = sum(1 for v in data["vendors"] for m in v["models"] if "pics_url" not in m and "listing_url" in m)
    pics_parsed    = sum(1 for v in data["vendors"] for m in v["models"] if "object_types" in m)
    print(f"Result: {vendor_count} vendors, {product_count} products, "
          f"{real_pics} with a real PICS document, {listing_only} listing-only, "
          f"{pics_parsed} object_types parsed")

    if vendor_count == 0 and not args.update_pics:
        print("WARNING: 0 vendors — preserving existing file.", file=sys.stderr)
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
