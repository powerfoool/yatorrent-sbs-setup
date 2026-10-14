#!/usr/bin/env python3
"""Offline task and Sheets projection helpers; no broker or Google requests."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import install
import coordinator_prompts


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def pointer(value, path):
    if path == '':
        return value
    if not path.startswith('/'):
        raise ValueError('Expected JSON Pointer')
    for key in path[1:].split('/'):
        key = key.replace('~1', '/').replace('~0', '~')
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def validate_agreement(agreement):
    if agreement.get('schema_version') != 1 or agreement.get('approved') is not True:
        raise ValueError('Result format must be agreed with the user before preparing tasks')
    if not agreement.get('approval_note') or not isinstance(agreement.get('output_schema'), dict):
        raise ValueError('Missing agreement or output schema')
    if agreement.get('result_pool', 'kron') not in ('kron', 'aim2'):
        raise ValueError('Unsupported broker result pool')
    destination = agreement.get('destination', 'google_sheets')
    if destination not in ('google_sheets', 'local_json'):
        raise ValueError('Explicit supported result destination required')
    if destination == 'local_json':
        return agreement
    outputs = agreement.get('outputs')
    if not isinstance(outputs, dict) or not outputs:
        raise ValueError('Missing output column mapping')
    inputs = agreement.get('input_columns', {})
    if not inputs or any(not re.fullmatch('[A-Z]+', x) for x in [*inputs.values(), *outputs.keys()]):
        raise ValueError('Columns must be explicit uppercase A1 names')
    if set(inputs.values()) & set(outputs):
        raise ValueError('Input columns cannot be overwritten by result columns')
    for p in outputs.values():
        if not isinstance(p, str) or (p and not p.startswith('/')):
            raise ValueError('Output mappings use JSON Pointers; empty pointer is full JSON')
    return agreement


def prepare(pair, agreement, selection):
    validate_agreement(agreement)
    required = {'id', 'query', 'left_html', 'right_html'}
    if not required <= pair.keys() or not all(isinstance(pair[k], str) and pair[k] for k in required):
        raise ValueError('Each pair needs a stable ID, query and both saved SERP HTML strings')
    allowed = required | {'region', 'device', 'left_top5', 'right_top5', 'left_source_url', 'right_source_url', 'captcha_grant'}
    if set(pair) - allowed:
        raise ValueError('Do not pass unrelated row data, previous verdicts or credentials to the worker')
    selection = coordinator_prompts.validate_selection(selection)
    # Bind immutable instructions and the user command to this task identity.
    identity = fingerprint({'pair': pair, 'agreement': agreement, 'selection': selection})
    item = 'sbs-' + identity[:48]
    data = {'contract':'sbs-instructions-v1','pair':pair,'instructions':selection['instructions'],
            'selection':{'revision':selection['revision'],'user_command':selection['user_command']}}
    publication = {'item_id':item,'task_type':'codex','worker_version':'0.3.9', 'result_pool':agreement.get('result_pool','kron'),'payload':[
        {'id':item,'prompt':'SBS_COORDINATOR_TASK_V1','input_data':data,'output_schema':agreement['output_schema']}]}
    if len(json.dumps(publication['payload'][0],ensure_ascii=False,separators=(',',':')).encode()) > 240000:
        raise ValueError('Pair plus instructions exceeds native item budget; do not truncate')
    return {'schema_version':1,'pair_id':pair['id'],'input_fingerprint':fingerprint(pair),
            'format_fingerprint':fingerprint(agreement),'selection_revision':selection['revision'],
            'instruction_hashes':{role:v['sha256'] for role,v in selection['instructions'].items()},
            'publication':publication}


def project(output, agreement, row):
    validate_agreement(agreement)
    if agreement.get('destination', 'google_sheets') != 'google_sheets':
        raise ValueError('Local JSON results cannot be projected to Sheets')
    if type(row) is not int or row < 1:
        raise ValueError('Positive current row required')
    title = agreement.get('sheet_title')
    if not isinstance(title, str) or not title:
        raise ValueError('Resolve actual sheet title from its numeric ID')
    data = []
    for column, path in agreement['outputs'].items():
        value = pointer(output, path)
        # RAW prevents formula execution and preserves JSON; oversized fields must
        # be handled through the agreed layout instead of silently truncated.
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        elif value is None:
            value = ''
        if not isinstance(value, (str, int, float, bool)) or len(str(value)) > 49000:
            raise ValueError('Output exceeds a cell or needs an agreed alternative')
        name = "'" + title.replace("'", "''") + "'!" + column + str(row)
        data.append({'range': name, 'values': [[value]]})
    return {'valueInputOption': 'RAW', 'data': data,
            'includeValuesInResponse': True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['prepare', 'project'])
    p.add_argument('--agreement', required=True, type=Path)
    p.add_argument('--input', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--selection', type=Path, help='Coordinator instruction selection; required for prepare')
    p.add_argument('--row', type=int)
    a = p.parse_args()
    try:
        agreement = install.read_json(a.agreement)
        value = install.read_json(a.input)
        result = prepare(value, agreement, install.read_json(a.selection)) if a.mode == 'prepare' else project(value, agreement, a.row)
        if a.output.exists():
            if install.read_json(a.output) != result:
                raise ValueError('Do not overwrite a different task or projection')
        else:
            install.write_json(a.output, result, a.output.parent)
        print(json.dumps({'ok': True, 'mode': a.mode, 'output': str(a.output), 'network_requests': 0}))
        return 0
    except (ValueError, KeyError, OSError, TypeError, AttributeError, install.SetupError):
        print('Coordinator data rejected: input, agreement or mapping invalid; contents withheld', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
