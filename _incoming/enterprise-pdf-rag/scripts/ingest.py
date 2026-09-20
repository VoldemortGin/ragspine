#!/usr/bin/env python3
"""Thin entry for the installed package's generic PDF ingestion command."""

import sys

from enterprise_pdf_rag.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["ingest", *sys.argv[1:]]))
