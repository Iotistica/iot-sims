\
from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from bacnet_pics import PicsParser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract a BACnet PICS PDF into structured JSON."
    )
    parser.add_argument("pdf", type=Path, help="Path to the PICS PDF")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output JSON path. Defaults to <pdf-name>.json",
    )
    parser.add_argument(
        "--deployment",
        help=(
            "Azure OpenAI deployment name. Defaults to "
            "AZURE_OPENAI_DEPLOYMENT."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages.",
    )
    return parser


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()

    output = args.output or args.pdf.with_suffix(".json")

    parser = PicsParser(
        args.pdf,
        deployment=args.deployment,
        verbose=not args.quiet,
    )
    profile = parser.parse()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        profile.model_dump_json(indent=2),
        encoding="utf-8",
    )

    print(f"Saved {len(profile.objects)} object definitions to {output}")
    if profile.warnings:
        print(f"Profile warnings: {len(profile.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
