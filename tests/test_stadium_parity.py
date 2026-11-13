"""Pin every loaded stadium field, including scalar types, before ownership changes."""

import json
from dataclasses import asdict
from pathlib import Path

from pipeline.stadiums.loader import load_stadium_book

FIXTURE = Path(__file__).parent / "fixtures" / "stadiums_before_split.json"


def test_stadium_ownership_split_exact_parity() -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = {sid: asdict(st) for sid, st in load_stadium_book().stadiums.items()}
    # JSON text distinguishes int/float/bool as well as values; object order is immaterial.
    assert json.dumps(actual, sort_keys=True, separators=(",", ":")) == json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    )
