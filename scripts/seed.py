"""Load seed data into the database named by FAIBLE_DATABASE_URL (idempotent).

    server/.venv/bin/python scripts/seed.py

Same as `faible seed`. If data/shotlist.csv exists (columns: sequence,shot_code,description)
it replaces the synthetic ten shots.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from faible.cli import main  # noqa: E402

sys.exit(main(["seed"]))
