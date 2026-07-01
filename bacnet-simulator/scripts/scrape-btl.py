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
]

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

                # Capture PICS PDF link from any cell in the row
                for cell in cells:
                    for a in cell.find_all("a", href=True):
                        href = a["href"]
                        if href.lower().endswith(".pdf"):
                            entry["pics_url"] = urljoin(url, href)
                            break
                    if "pics_url" in entry:
                        break

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

    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        for page in pdf.pages:
            for table in (page.extract_tables() or []):
                obj_types.update(_parse_table(table))
            text = page.extract_text() or ""
            obj_types.update(_parse_text(text, existing=obj_types))

    return obj_types


# ---------------------------------------------------------------------------
# Scrape modes
# ---------------------------------------------------------------------------

def _pics_stats(products: list[dict]) -> tuple[int, int]:
    """Return (products_with_pics_url, products_with_object_types)."""
    return (
        sum(1 for p in products if "pics_url" in p),
        sum(1 for p in products if "object_types" in p),
    )


def scrape(parse_pics: bool = False, only_mfr_id: str | None = None) -> dict:
    """Full scrape of the BTL website."""
    vendors: dict[str, list[dict]] = {}

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

            if parse_pics:
                for p in unique:
                    if "pics_url" not in p:
                        continue
                    try:
                        ot = parse_pics_pdf(p["pics_url"])
                        if ot:
                            p["object_types"] = ot
                    except Exception as e:
                        p["pics_error"] = str(e)
                    time.sleep(0.5)

            if unique:
                vendors[name] = unique
                n_links, n_parsed = _pics_stats(unique)
                tag = f" ({n_links} PICS"
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
    Read existing JSON, parse PDFs for products that have a pics_url but
    no object_types yet (or all of them if --force).  No HTML re-scrape.
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
            if "pics_url" not in product:
                continue
            if not force and "object_types" in product:
                continue
            total += 1
            print(f"  Parsing {vendor['name']} / {product['name']} …", end="", flush=True)
            try:
                ot = parse_pics_pdf(product["pics_url"])
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
        data = scrape(parse_pics=args.parse_pics, only_mfr_id=args.manufacturer)

    vendor_count  = len(data["vendors"])
    product_count = sum(len(v["models"]) for v in data["vendors"])
    pics_links    = sum(1 for v in data["vendors"] for m in v["models"] if "pics_url" in m)
    pics_parsed   = sum(1 for v in data["vendors"] for m in v["models"] if "object_types" in m)
    print(f"Result: {vendor_count} vendors, {product_count} products, "
          f"{pics_links} PICS links, {pics_parsed} parsed")

    if vendor_count == 0 and not args.update_pics:
        print("WARNING: 0 vendors — preserving existing file.", file=sys.stderr)
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
