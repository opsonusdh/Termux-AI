"""
agent/validator.py — JSON-schema validator for execution results.
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import paths

try:
    import jsonschema
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False


def validate_execution(result: dict) -> dict:
    """
    Validate *result* against data/validator_schema.json.
    Returns {'validation': 'Success'} or {'validation': 'Failure', 'error': ...}.
    """
    schema_path = paths.VALIDATOR_SCHEMA_FILE
    if not _HAS_JSONSCHEMA:
        return {"validation": "Success", "note": "jsonschema not installed — skipped"}
    if not os.path.exists(schema_path):
        return {"validation": "Success", "note": "schema file not found — skipped"}
    with open(schema_path, 'r') as f:
        schema = json.load(f)
    try:
        jsonschema.validate(instance=result, schema=schema)
        return {"validation": "Success"}
    except jsonschema.ValidationError as e:
        return {"validation": "Failure", "error": str(e)}
