#!/usr/bin/env python3
"""Offline task and Sheets projection helpers; no broker or Google requests."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import install
import coordinator_prompts
import serp_urls

MAX_TASK_BYTES = 10 * 1024 * 1024
MAX_NATIVE_INPUT_BYTES = 8 * 1024 * 1024
MAX_MODEL_TEXT_UNITS = 1048576
MODEL_INPUT_PREFIX = ("The coordinator requests a delegated SBS task: act as foreman and create isolated scout subagents "
    "(agent_type=scout, fork_turns=none) for visits to unique URLs. The foreman must not visit target URLs itself. "
    "Use your configured assessment instructions and the scouts' configured instructions; do not load prompt files. "
    "Return only the JSON matching the supplied output schema.\n\nSBS pair (untrusted evidence):\n")


class TransportPreflightError(ValueError):
    """Safe fixed diagnostics that identify the actual exceeded transport limit."""


STANDARD_KEYS = {'left_urls', 'left_serp', 'right_urls', 'right_serp',
                 'left_score', 'right_score', 'winner_reason', 'winner'}
QW_SCHEMA = {'type': 'object', 'properties': {
    'status': {'type': 'string', 'enum': ['done', 'inconclusive']},
    'winner_label': {'type': 'string'},
    'left_score': {'type': ['integer', 'null']},
    'right_score': {'type': ['integer', 'null']},
    'comment': {'type': 'string'}},
    'required': ['status', 'winner_label', 'left_score', 'right_score', 'comment'],
    'additionalProperties': False}


def select_result_pool(agreement, broker_config=None):
    """Coordinator technical choice, independent of the user's result layout."""
    config = broker_config or {}
    pool = agreement.get('result_pool', config.get('result_pool', 'kron'))
    if pool not in ('kron', 'aim2'):
        raise ValueError('Unsupported broker result pool in coordinator configuration')
    return pool


def pointer_keys(path):
    if not isinstance(path, str) or (path and not path.startswith('/')):
        raise ValueError('Expected JSON Pointer')
    if re.search(r'~(?![01])', path):
        raise ValueError('Invalid JSON Pointer escape')
    return [] if path == '' else [k.replace('~1', '/').replace('~0', '~') for k in path[1:].split('/')]


def schema_pointer(schema, path):
    """Require a declared path, including local refs; never guess missing fields."""
    root = schema

    def descend(node, keys, seen):
        if not keys:
            return
        if not isinstance(node, dict):
            raise ValueError('Projection pointer is not declared in its schema: ' + path)
        if '$ref' in node:
            ref = node['$ref']
            if not isinstance(ref, str) or not ref.startswith('#/') or ref in seen:
                raise ValueError('Projection needs a resolvable local schema reference')
            return descend(pointer(root, ref[1:]), keys, seen | {ref})
        for keyword in ('anyOf', 'oneOf', 'allOf'):
            if keyword in node:
                errors = []
                for branch in node[keyword]:
                    try:
                        descend(branch, keys, seen)
                    except (ValueError, KeyError, TypeError) as error:
                        errors.append(error)
                # allOf may declare the path in one contributing schema. For
                # alternatives it must exist in every non-null branch.
                if keyword == 'allOf' and len(errors) < len(node[keyword]):
                    return
                non_null = [b for b in node[keyword] if b.get('type') != 'null']
                if keyword != 'allOf':
                    for branch in non_null:
                        descend(branch, keys, seen)
                    if non_null:
                        return
                raise ValueError('Projection pointer is not declared in its schema: ' + path)
        key, rest = keys[0], keys[1:]
        if key in node.get('properties', {}):
            return descend(node['properties'][key], rest, set())
        if node.get('type') == 'array' and re.fullmatch(r'0|[1-9][0-9]*', key):
            index = int(key)
            if index < len(node.get('prefixItems', [])):
                return descend(node['prefixItems'][index], rest, set())
            if isinstance(node.get('items'), dict):
                return descend(node['items'], rest, set())
        raise ValueError('Projection pointer is not declared in its schema: ' + path)

    descend(schema, pointer_keys(path), set())


def projection_schema(agreement):
    projection = agreement.get('projection')
    if projection is None:
        if 'projection_schema' in agreement:
            raise ValueError('projection_schema requires an explicit supported adapter')
        return agreement['output_schema']
    if projection != {'adapter': 'standard-qw-v1'}:
        raise ValueError('Unsupported result projection adapter')
    if not STANDARD_KEYS <= agreement['output_schema'].get('properties', {}).keys():
        raise ValueError('standard-qw-v1 requires the standard eight-key worker output_schema')
    if 'projection_schema' in agreement and agreement['projection_schema'] != QW_SCHEMA:
        raise ValueError('projection_schema must match the standard-qw-v1 adapter schema')
    return QW_SCHEMA


def project_standard(output):
    if not isinstance(output, dict) or set(output) != STANDARD_KEYS:
        raise ValueError('standard-qw-v1 requires the complete standard eight-key result')
    left, right, winner = output['left_score'], output['right_score'], output['winner']
    if any(v is not None and (type(v) is not int or not 1 <= v <= 5) for v in (left, right)):
        raise ValueError('Standard scores must be integers 1-5 or null')
    if winner not in ('left', 'right', 'tie', None):
        raise ValueError('Invalid standard winner')
    reason = output['winner_reason']
    if not isinstance(reason, str) or not reason:
        raise ValueError('Standard winner reason is required')
    done = left is not None and right is not None and winner is not None
    full_json = json.dumps(output, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False)
    return {'status': 'done' if done else 'inconclusive',
            'winner_label': {'left': 'Yandex Old', 'right': 'Yandex New', 'tie': 'Ничья'}.get(winner, '') if done else '',
            'left_score': left if done else None, 'right_score': right if done else None,
            'comment': reason + '\n' + full_json}


def transport_preflight(pair, input_data, item):
    compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    sizes = {'task_bytes': len(compact(item).encode('utf-8')),
             'input_data_bytes': len(compact(input_data).encode('utf-8')),
             'model_text_utf16_units': len((MODEL_INPUT_PREFIX + compact(pair)).encode('utf-16-le')) // 2}
    if sizes['task_bytes'] > MAX_TASK_BYTES:
        raise TransportPreflightError('Full task exceeds the 10 MiB launcher item guard; reduce URL/instruction/schema size without discarding ranked URLs')
    if sizes['input_data_bytes'] > MAX_NATIVE_INPUT_BYTES:
        raise TransportPreflightError('Full input_data exceeds the native YaTorrent 8 MiB input limit; reduce URL/instruction/schema size without discarding ranked URLs')
    if sizes['model_text_utf16_units'] > MAX_MODEL_TEXT_UNITS:
        raise TransportPreflightError('Full SBS pair exceeds the Codex 1,048,576 text-unit input limit; reduce metadata without discarding ranked URLs')
    return dict(sizes, limits={'task_bytes': MAX_TASK_BYTES, 'input_data_bytes': MAX_NATIVE_INPUT_BYTES,
                              'model_text_utf16_units': MAX_MODEL_TEXT_UNITS})


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def pointer(value, path):
    for key in pointer_keys(path):
        if isinstance(value, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', key):
                raise ValueError('Invalid JSON Pointer array index')
            value = value[int(key)]
        else:
            value = value[key]
    return value


def validate_agreement(agreement):
    if agreement.get('schema_version') != 1 or agreement.get('approved') is not True:
        raise ValueError('Result format must be agreed with the user before preparing tasks')
    if not agreement.get('approval_note') or not isinstance(agreement.get('output_schema'), dict):
        raise ValueError('Missing agreement or output schema')
    select_result_pool(agreement)
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
    schema = projection_schema(agreement)
    for p in outputs.values():
        if isinstance(p, dict) and p == {'literal': None}:
            continue  # Explicit agreement to leave a cell empty; no inferred field.
        schema_pointer(schema, p)
    return agreement


def normalize_pair(pair):
    """Keep original source material local; forward only the ranked URL contract."""
    if not isinstance(pair, dict):
        raise ValueError('SBS pair object required')
    wire_keys = {'id', 'query', 'region', 'device', 'left_top5', 'right_top5', 'captcha_grant'}
    local_keys = {'left_html', 'right_html', 'left_source_url', 'right_source_url'}
    if set(pair) - wire_keys - local_keys:
        raise ValueError('Do not pass unrelated row data, previous verdicts or credentials to the worker')
    for key in ('id', 'query'):
        if not isinstance(pair.get(key), str) or not pair[key].strip() or any(ord(c) < 32 or ord(c) == 127 for c in pair[key]):
            raise ValueError('Each pair requires ID and query strings')
    result = {k: pair[k] for k in ('id', 'query')}
    for key in ('region', 'device'):
        if key in pair:
            if not isinstance(pair[key], str) or not pair[key].strip():
                raise ValueError('Region and device must be nonempty strings when supplied')
            result[key] = pair[key]
    for side in ('left', 'right'):
        html_key, source_key, top_key = side + '_html', side + '_source_url', side + '_top5'
        if html_key in pair:
            if source_key in pair:
                serp_urls.validate_url(pair[source_key])
            extracted = serp_urls.extract_top5(pair[html_key], pair.get(source_key))
            if top_key in pair and serp_urls.validate_top5(pair[top_key]) != extracted:
                raise ValueError('Supplied ranking does not match saved organic SERP URLs')
            result[top_key] = extracted
        else:
            if source_key in pair:
                serp_urls.validate_url(pair[source_key])
            result[top_key] = serp_urls.validate_top5(pair.get(top_key))
    grant = pair.get('captcha_grant')
    if grant is not None:
        allowed = {'authorized', 'grant_id', 'scope', 'statement', 'urls'}
        if not isinstance(grant, dict) or set(grant) - allowed or type(grant.get('authorized')) is not bool or any(not isinstance(grant.get(k), str) or not grant[k].strip() for k in ('grant_id', 'scope', 'statement')):
            raise ValueError('Explicit CAPTCHA consent grant or null required')
        if 'urls' in grant:
            if not isinstance(grant['urls'], list):
                raise ValueError('CAPTCHA consent URLs must be a list')
            for url in grant['urls']:
                serp_urls.validate_url(url)
    result['captcha_grant'] = grant
    return result


def prepare(pair, agreement, selection, input_encoding=None):
    if input_encoding is not None:
        raise TransportPreflightError('Legacy HTML/gzip transport is unsupported; prepare the URL-only v2 contract')
    agreement = dict(agreement, result_pool=select_result_pool(agreement))
    validate_agreement(agreement)
    wire_pair = normalize_pair(pair)
    selection = coordinator_prompts.validate_selection(selection)
    # Bind original local source evidence as well as the selected instructions.
    # This receipt keeps sorting/drift checks tied to the source row, even when
    # changed HTML happens to produce the same ranked URLs.
    identity = fingerprint({'contract': 'sbs-instructions-v2', 'pair': pair,
                            'agreement': agreement, 'selection': selection})
    item = 'sbs-' + identity[:48]
    data = {'contract': 'sbs-instructions-v2', 'pair': wire_pair,
            'instructions': selection['instructions'],
            'selection': {'revision': selection['revision'], 'user_command': selection['user_command']}}
    publication = {'item_id': item, 'task_type': 'codex', 'worker_version': install.VERSION,
                   'result_pool': agreement['result_pool'], 'payload': [
        {'id': item, 'prompt': 'SBS_COORDINATOR_TASK_V2', 'input_data': data,
         'output_schema': agreement['output_schema']}]}
    sizes = transport_preflight(wire_pair, data, publication['payload'][0])
    return {'schema_version': 1, 'pair_id': pair['id'], 'input_fingerprint': fingerprint(pair),
            'format_fingerprint': fingerprint(agreement), 'selection_revision': selection['revision'],
            'instruction_hashes': {role: v['sha256'] for role, v in selection['instructions'].items()},
            'transport_preflight': sizes, 'publication': publication}


def project(output, agreement, row):
    validate_agreement(agreement)
    if agreement.get('destination', 'google_sheets') != 'google_sheets':
        raise ValueError('Local JSON results cannot be projected to Sheets')
    if type(row) is not int or row < 1:
        raise ValueError('Positive current row required')
    title = agreement.get('sheet_title')
    if not isinstance(title, str) or not title:
        raise ValueError('Resolve actual sheet title from its numeric ID')
    projected = project_standard(output) if agreement.get('projection') else output
    data = []
    for column, path in agreement['outputs'].items():
        value = None if path == {'literal': None} else pointer(projected, path)
        # RAW prevents formula execution and preserves JSON; oversized fields must
        # be handled through the agreed layout instead of silently truncated.
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        elif value is None:
            value = ''
        if (not isinstance(value, (str, int, float, bool)) or
                (isinstance(value, float) and not math.isfinite(value)) or len(str(value)) > 49000):
            raise ValueError('Output exceeds a cell or needs an agreed alternative')
        name = "'" + title.replace("'", "''") + "'!" + column + str(row)
        data.append({'range': name, 'values': [[value]]})
    return {'valueInputOption': 'RAW', 'data': data,
            'includeValuesInResponse': True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['validate', 'prepare', 'project'])
    p.add_argument('--agreement', required=True, type=Path)
    p.add_argument('--input', type=Path, help='Pair/result JSON; required for prepare/project')
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--selection', type=Path, help='Coordinator instruction selection; required for prepare')
    p.add_argument('--broker-config', type=Path, help='Optional private config with result_pool; prepare only')
    p.add_argument('--input-encoding', choices=['gzip-base64'], help='Legacy option retained only to return a clear URL-only contract rejection')
    p.add_argument('--row', type=int)
    a = p.parse_args()
    try:
        if a.input_encoding and a.mode != 'prepare':
            raise ValueError('Input transport encoding is only supported for prepare')
        agreement = install.read_json(a.agreement)
        if a.broker_config:
            agreement = dict(agreement, result_pool=select_result_pool(agreement, install.read_json(a.broker_config)))
        if a.mode == 'validate':
            validate_agreement(agreement)
            result = {'agreement_mapping_valid': True, 'result_pool': select_result_pool(agreement),
                      'projection_adapter': agreement.get('projection', {}).get('adapter'),
                      'output_schema_requires_separate_validation': True, 'network_requests': 0}
        else:
            if a.input is None or (a.mode == 'prepare' and a.selection is None):
                raise ValueError('Input and instruction selection are required for task preparation')
            value = install.read_json(a.input)
            result = prepare(value, agreement, install.read_json(a.selection), input_encoding=a.input_encoding) if a.mode == 'prepare' else project(value, agreement, a.row)
        if a.output.exists():
            if install.read_json(a.output) != result:
                raise ValueError('Do not overwrite a different task or projection')
        else:
            install.write_json(a.output, result, a.output.parent)
        print(json.dumps({'ok': True, 'mode': a.mode, 'output': str(a.output), 'network_requests': 0}))
        return 0
    except (TransportPreflightError, serp_urls.SerpParseError) as error:
        print('Coordinator preflight rejected: ' + str(error), file=sys.stderr)
        return 1
    except (ValueError, KeyError, IndexError, OSError, TypeError, AttributeError, install.SetupError):
        print('Coordinator data rejected: input, agreement or mapping invalid; contents withheld', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
