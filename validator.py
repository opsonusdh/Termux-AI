import sys
import os
import json
import jsonschema

# Ensure ai_root is in sys.path
ai_root = os.path.dirname(os.path.abspath(__file__))
if ai_root not in sys.path:
    sys.path.append(ai_root)

import paths

def validate_execution(result):
    """Validate the execution result against the validator_schema.json schema.

    Returns {'validation': 'Success'} if valid, otherwise {'validation': 'Failure', 'error': <msg>}.
    """
    schema_path = paths.VALIDATOR_SCHEMA_FILE
    with open(schema_path, 'r') as f:
        schema = json.load(f)
    try:
        jsonschema.validate(instance=result, schema=schema)
        return {"validation": "Success"}
    except jsonschema.ValidationError as e:
        return {"validation": "Failure", "error": str(e)}
