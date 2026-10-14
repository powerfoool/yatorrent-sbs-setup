#!/usr/bin/env python3
"""Coordinator-owned assessment selection. Human-command receipt is workflow evidence, not authentication."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import install

DEFAULT_PROMPTS = Path(__file__).resolve().parents[1] / 'prompts'


def instruction(content):
    if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 64000:
        raise ValueError('Nonempty role instructions of at most 64000 UTF-8 bytes required')
    if any(ord(c) < 32 and c not in '\t\r\n' or ord(c) == 127 for c in content):
        raise ValueError('Unsupported instruction control character')
    return {'content': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()}


def validate_selection(value):
    if set(value) != {'schema_version','revision','user_command','instructions'} or value['schema_version'] != 1 or type(value['revision']) is not int or value['revision'] < 1:
        raise ValueError('Coordinator selection required')
    command=value['user_command']
    if not isinstance(command,str) or not command.strip() or len(command)>4096:
        raise ValueError('Actual user command must be recorded by the coordinator')
    if set(value['instructions']) != {'foreman','scout'}:
        raise ValueError('Both coordinator instructions required; worker defaults forbidden')
    for role,v in value['instructions'].items():
        if instruction(v['content']) != v:
            raise ValueError('Coordinator instruction hash mismatch')
    return value


def select(state, foreman, scout, user_command, use_defaults=False):
    previous=install.read_json(state) if state.exists() else None
    if previous: validate_selection(previous)
    if use_defaults and (foreman is not None or scout is not None):
        raise ValueError('Choose bundled defaults or explicit overrides, not both')
    roles=dict(previous['instructions']) if previous else {}
    for role,file in [('foreman',foreman),('scout',scout)]:
        if use_defaults or (previous is None and file is None):
            file=DEFAULT_PROMPTS/(role+'.md')
        if file is not None: roles[role]=instruction(file.read_bytes().decode('utf-8'))
    value={'schema_version':1,'revision':previous['revision']+1 if previous else 1,
           'user_command':user_command,'instructions':roles}
    validate_selection(value)
    if previous and roles==previous['instructions']:
        return previous
    install.write_json(state,value,state.parent)
    return value


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',required=True,type=Path)
    p.add_argument('--foreman',type=Path)
    p.add_argument('--scout',type=Path)
    p.add_argument('--use-skill-defaults',action='store_true',help='Explicit user-command reset to bundled coordinator defaults')
    p.add_argument('--user-command',required=True)
    a=p.parse_args()
    try:
        value=select(a.state,a.foreman,a.scout,a.user_command,a.use_skill_defaults)
        print(json.dumps({'ok':True,'revision':value['revision'],'foreman_sha256':value['instructions']['foreman']['sha256'],'scout_sha256':value['instructions']['scout']['sha256']}))
        return 0
    except (ValueError,KeyError,TypeError,OSError,install.SetupError):
        print('Instruction selection rejected; current selection preserved, contents withheld',file=sys.stderr)
        return 1


if __name__=='__main__':sys.exit(main())
