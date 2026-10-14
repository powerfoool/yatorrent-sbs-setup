#!/usr/bin/env python3
"""Validate local draft2020-12 schemas and outputs; never fetch remote references."""
import json
from pathlib import Path
import sys
from jsonschema import Draft202012Validator


def check_refs(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'$ref', '$dynamicRef'} and (not isinstance(item, str) or not item.startswith('#')):
                raise ValueError('Resolve remote schemas locally before intake')
            check_refs(item)
    elif isinstance(value, list):
        for item in value:
            check_refs(item)


def main():
    if len(sys.argv) not in {2, 3}:
        return 2
    try:
        agreement = json.loads(Path(sys.argv[1]).read_text())
        schema = agreement['output_schema']
        check_refs(schema)
        Draft202012Validator.check_schema(schema)
        if len(sys.argv) == 3:
            Draft202012Validator(schema).validate(json.loads(Path(sys.argv[2]).read_text()))
        print(json.dumps({'ok': True, 'schema_valid': True, 'output_validated': len(sys.argv) == 3}))
        return 0
    except Exception:
        print('Schema or output invalid; data contents withheld', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
