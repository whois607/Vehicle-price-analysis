"""The test suites. They run against fictional sites (tests/sites_test.json),
never your private data/sites.json - unless VPA_SITES is already set."""
import os
from pathlib import Path

os.environ.setdefault("VPA_SITES", str(Path(__file__).resolve().parent / "sites_test.json"))
