#!/usr/bin/env python3
"""Set the stable HTTPS manifest URL embedded in future Caddie builds."""

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHANNEL_PATH = ROOT / "assets" / "update_channel.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-url", required=True)
    parser.add_argument("--channel", default="alpha")
    args = parser.parse_args()
    if not args.manifest_url.startswith("https://"):
        raise SystemExit("Manifest URL must use HTTPS")
    CHANNEL_PATH.write_text(
        json.dumps(
            {"channel": args.channel, "manifest_url": args.manifest_url},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Configured {args.channel}: {args.manifest_url}")


if __name__ == "__main__":
    main()
