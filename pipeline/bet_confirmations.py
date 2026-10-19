"""Read-only access to Worker-owned explicit bet confirmations."""
import json
from pathlib import Path

from pipeline.state import StateSchemaError


def load_confirmations(directory: Path) -> dict:
    try:
        data = json.loads((directory / 'bet_confirmations.json').read_text(encoding='utf8'))
    except FileNotFoundError:
        return {'schema_version': 1, 'bets': {}}
    except (ValueError, OSError) as exc:
        raise StateSchemaError('Bet confirmations could not be read safely') from exc
    return validate_confirmations(data)


def validate_confirmations(data: dict) -> dict:
    """Pure validation shared by file readers and read-only remote inspections."""
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('bets'), dict):
        raise StateSchemaError('Unsupported bet confirmation ledger')
    for bid, row in data['bets'].items():
        if (not isinstance(row, dict) or row.get('bet_id') != bid or row.get('confirmed') is not True
                or row.get('source') != 'explicit_user_confirmation'):
            raise StateSchemaError('Invalid explicit bet confirmation')
    return data
