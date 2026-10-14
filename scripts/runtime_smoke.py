#!/usr/bin/env python3
"""Bounded real-Codex smoke; only redacted tool/policy evidence is returned."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import uuid

import install


class EvidenceError(ValueError):
    pass


def _texts(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [x.get('text', '') for x in value if isinstance(x, dict) and x.get('type') in ('input_text', 'text', 'output_text')]
    return []


def _json_objects(text):
    """Decode structured tool output only, never execute any rollout content."""
    values = []
    for line in text.splitlines():
        candidate = line.strip()
        try:
            value = json.loads(candidate)
        except ValueError:
            # Some code-mode tool results escape inner stdout JSON once more.
            if candidate.startswith('{\\"'):
                try:
                    value = json.loads(candidate.replace('\\"', '"'))
                except ValueError:
                    continue
            else:
                continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _command(call):
    name = call.get('name', '').split('.')[-1]
    if call.get('type') == 'function_call' and name == 'exec_command':
        value = json.loads(call.get('arguments', ''))
    elif call.get('type') == 'custom_tool_call' and name == 'exec':
        # A narrow syntax whitelist prevents accepting text(fake_success).
        code = call.get('input', '').strip()
        code = re.sub(r'^// @exec:[^\n]*\n', '', code).strip()
        match = re.fullmatch(r'text\(await tools\.exec_command\((\{[\s\S]*\})\)\);?', code)
        if not match:
            raise EvidenceError('unsupported_code_mode_execution')
        obj = re.sub(r'(?<=[{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:', lambda m: json.dumps(m[1]) + ':', match[1])
        value = json.loads(obj)
    else:
        raise EvidenceError('unexpected_tool_call')
    if not isinstance(value, dict) or not isinstance(value.get('cmd'), str):
        raise EvidenceError('missing_execution_command')
    if set(value) - {'cmd', 'yield_time_ms', 'max_output_tokens', 'workdir', 'login'}:
        raise EvidenceError('unexpected_execution_options')
    if value.get('login', False) is not False:
        raise EvidenceError('login_shell_requested')
    return value


def _execution(output, code_mode):
    texts = _texts(output)
    records = [x for text in texts for x in _json_objects(text) if 'output' in x and ('exit_code' in x or 'session_id' in x)]
    if len(records) == 1:
        value = records[0]
        if type(value.get('exit_code')) is not int or not isinstance(value.get('output'), str):
            raise EvidenceError('tool_execution_not_completed')
        return value['exit_code'], value['output']
    # Native direct exec_command serializes status separately from Final output.
    if not code_mode:
        text = '\n'.join(texts)
        match = re.search(r'Process exited with code (\d+)\n(?:Final output:|Output:)\n([\s\S]*)', text)
        if match:
            return int(match[1]), match[2]
    raise EvidenceError('missing_or_ambiguous_tool_exit_code')


def parse_rollout(events, commands, markers, expected_versions, workspace):
    """Validate paired execution records. Model final messages are ignored."""
    calls, outputs, contexts, slots = {}, {}, [], []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get('payload'), dict):
            raise EvidenceError('malformed_rollout_event')
        payload = event['payload']
        if event.get('type') == 'turn_context':
            contexts.append(payload)
        if event.get('type') != 'response_item':
            continue
        kind = payload.get('type')
        if kind == 'message' and payload.get('role') == 'developer':
            for text in _texts(payload.get('content')):
                slots.extend(int(x) for x in re.findall(r'There are (\d+) available concurrency slots', text))
        if kind in ('function_call', 'custom_tool_call'):
            key = payload.get('call_id')
            if not key or key in calls:
                raise EvidenceError('duplicate_or_missing_call_id')
            calls[key] = payload
        if kind in ('function_call_output', 'custom_tool_call_output'):
            key = payload.get('call_id')
            if not key or key in outputs:
                raise EvidenceError('duplicate_or_missing_output_call_id')
            outputs[key] = payload.get('output')
    if not contexts or not slots or set(slots) != {11}:
        raise EvidenceError('actual_agent_slots_not_eleven')
    policies = []
    for context in contexts:
        sandbox = context.get('sandbox_policy', {})
        if sandbox.get('type') != 'workspace-write' or sandbox.get('network_access') is not True:
            raise EvidenceError('actual_workspace_write_network_policy_mismatch')
        policy = context.get('approval_policy')
        if policy not in ('never', 'untrusted', 'unless-trusted', 'UnlessTrusted'):
            raise EvidenceError('unrecognized_actual_approval_policy')
        policies.append(policy)
        if context.get('cwd') is not None and Path(context['cwd']) != workspace:
            raise EvidenceError('actual_workspace_mismatch')
    if len(calls) != 3 or set(calls) != set(outputs):
        raise EvidenceError('exact_three_paired_tool_calls_required')
    observed, browser_result = {}, None
    for call_id, call in calls.items():
        value = _command(call)
        command = value['cmd']
        roles = [role for role, expected in commands.items() if command == expected]
        if len(roles) != 1 or roles[0] in observed:
            raise EvidenceError('unexpected_or_duplicate_execution_command')
        role = roles[0]
        if 'workdir' in value and Path(value['workdir']) != workspace:
            raise EvidenceError('execution_workspace_mismatch')
        code, stdout = _execution(outputs[call_id], call['type'] == 'custom_tool_call')
        if code != 0:
            raise EvidenceError('execution_nonzero_' + role)
        if role == 'browser':
            objects = _json_objects(stdout)
            if len(objects) != 1:
                raise EvidenceError('browser_missing_or_malformed_readback')
            browser_result = objects[0]
            expected = {'ok': True, 'readback': 'SBS_CHROMIUM_ALLOW_OK', 'chromium_sandbox': True, 'tls_verification': True, **expected_versions}
            if any(browser_result.get(key) != val or (isinstance(val, bool) and type(browser_result.get(key)) is not bool) for key, val in expected.items()):
                raise EvidenceError('browser_semantic_readback_mismatch')
        elif stdout.strip() != markers[role]:
            raise EvidenceError('reader_marker_mismatch_' + role)
        observed[role] = {'call_id': call_id, 'exit_code': code, 'semantic_readback': True}
    return {'ok': True, 'actual_agent_slots': 11, 'actual_sandbox': 'workspace-write', 'actual_network_access': True,
            'requested_approval_policy': 'never', 'actual_approval_policies': sorted(set(policies)),
            'actual_approval_policy_names': sorted(set('Never' if x == 'never' else 'UnlessTrusted' for x in policies)),
            'managed_approval_override_observed': any(x != 'never' for x in policies),
            'tools': observed, 'browser': {key: browser_result[key] for key in ('node', 'playwright', 'chromium', 'chromium_sandbox', 'tls_verification', 'readback')}}


def _expected_versions(root):
    browsers = install.read_json(root / 'browser-runtime/node_modules/playwright-core/browsers.json').get('browsers', [])
    versions = [item.get('browserVersion') for item in browsers if item.get('name') == 'chromium']
    if versions != [install.CHROMIUM_VERSION]:
        raise install.SetupError('Installed Chromium metadata version pin mismatch')
    return {'node': 'v' + install.NODE_VERSION, 'playwright': install.PLAYWRIGHT_VERSION, 'chromium': install.CHROMIUM_VERSION}


def run_runtime_smoke(root, timeout=240):
    root = install.command_path(root)
    setup = install.read_json(root / 'lib/install-config.json')
    node, browser, reader = install.entry_points(setup, root)
    launcher = install.trusted_path(root / 'lib/codex-launcher.mjs', root)
    guard = install.trusted_path(root / 'lib/sbs-auth-guard.mjs', root)
    # Byte-exact installed launcher/fixed scripts; no invented alternate runtime.
    for target in (launcher, guard, browser, reader, root / 'lib/sbs-task-instructions.mjs', root / 'lib/sbs-url-budget.mjs', root / 'lib/sbs-navigation-transaction.mjs'):
        if target.read_bytes() != (install.ASSETS / target.name).read_bytes():
            raise install.SetupError('Installed runtime asset mismatch')
    profile = install.public_profile(root / 'profiles/databroker.aim2')
    model = profile['executors']['codex']['model']
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]+', model):
        raise install.SetupError('Invalid public profile model')
    expected_versions = _expected_versions(root)
    version_env = dict(os.environ, PATH=str(node.parent) + os.pathsep + os.environ.get('PATH', ''))
    actual_version = subprocess.run([str(setup['real_codex']), '--version'], env=version_env, capture_output=True, timeout=20)
    if actual_version.returncode != 0 or actual_version.stdout.decode(errors='replace').strip() != 'codex-cli ' + install.CODEX_VERSION:
        raise install.SetupError('Actual Codex executable version pin mismatch')
    package = install.read_json(root / 'codex/node_modules/@openai/codex/package.json')
    if package.get('version') != install.CODEX_VERSION:
        raise install.SetupError('Installed Codex version pin mismatch')
    smoke_parent = root / 'runtime-smoke'
    install.trusted_path(smoke_parent, smoke_parent, require_existing=False)
    smoke_parent.mkdir(mode=0o700, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix='job-', dir=smoke_parent))
    (workspace / 'prompts').mkdir(mode=0o700)
    markers = {role: 'SBS_RUNTIME_' + role.upper() + '_' + uuid.uuid4().hex for role in ('foreman', 'scout')}
    for role, marker in markers.items():
        (workspace / 'prompts' / (role + '.md')).write_text(marker + '\n')
    commands = {'browser': f'{node} {browser} --check', **{role: f'{node} {reader} {role}' for role in markers}}
    schema = {'type': 'object', 'properties': {'completed': {'type': 'boolean'}}, 'required': ['completed'], 'additionalProperties': False}
    schema_path = workspace / 'result-schema.json'
    schema_path.write_text(json.dumps(schema))
    prompt = ('Run exactly these three shell commands, one execution call per command, in this workspace. '
              'Use exec_command with login=false, yield_time_ms=30000, max_output_tokens=4000. '
              'If code-mode is required, use only text(await tools.exec_command({...})); with a JSON-compatible object. '
              'Use the exact literal unquoted commands below; no quotes in shell commands, no extra tools, no scripts, '
              'no shell chaining, no evaluation, no delegates, no files other than the fixed reader inputs. '
              'The browser check uses local content only. Do not access broker, sites, private files, or credentials. '
              'Do not retry failures. The final JSON completed value may be true only if all three tools succeeded.\n'
              + '\n'.join(commands.values()))
    args = [str(node), str(launcher), '--sbs-diagnostic', 'exec', '--model', model, '--sandbox', 'workspace-write', '--ignore-user-config',
            '--ephemeral', '--skip-git-repo-check', '--color', 'never', '-c', 'approval_policy=never',
            '-c', 'sandbox_workspace_write.network_access=true', '--output-schema', str(schema_path),
            '--output-last-message', str(workspace / 'last-message.json'), '-']
    env = dict(os.environ, PATH=str(node.parent) + os.pathsep + os.environ.get('PATH', ''))
    result = {'ok': False, 'diagnostic_only': True, 'worker_ready': False, 'broker_intake_started': False,
              'workspace': str(workspace), 'model': model, 'codex_version': install.CODEX_VERSION, 'actual_codex_version_readback': 'codex-cli ' + install.CODEX_VERSION}
    process = subprocess.Popen(args, cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    timed_out = False
    try:
        process.communicate(prompt.encode(), timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    result.update(codex_exit_code=process.returncode, timed_out=timed_out)
    homes = list(workspace.glob('.sbs-cli-*'))
    auth_removed = len(homes) == 1 and not os.path.lexists(homes[0] / 'auth.json')
    result['session_auth_symlink_removed'] = auth_removed
    # A forced timeout may prevent the launcher's finally block. Remove only
    # its exact credential symlink after the whole process group has stopped.
    # The recorded launcher-cleanup failure still makes this smoke fail.
    if timed_out:
        for home in homes:
            link = home / 'auth.json'
            if not home.is_symlink() and link.is_symlink() and os.readlink(link) == str(Path(setup['auth_home']) / 'auth.json'):
                link.unlink()
    try:
        if timed_out or process.returncode != 0:
            raise EvidenceError('codex_timeout_or_nonzero')
        if len(homes) != 1 or homes[0].is_symlink() or not auth_removed:
            raise EvidenceError('isolated_session_or_auth_cleanup_mismatch')
        rollouts = list(homes[0].glob('sessions/**/rollout-*.jsonl'))
        if len(rollouts) != 1 or rollouts[0].is_symlink():
            raise EvidenceError('unique_session_rollout_required')
        events = [json.loads(line) for line in rollouts[0].read_text().splitlines()]
        result.update(parse_rollout(events, commands, markers, expected_versions, workspace))
        result['rollout'] = str(rollouts[0])
    except (EvidenceError, ValueError, OSError, TypeError, KeyError) as exc:
        result['failure'] = str(exc) if isinstance(exc, EvidenceError) else 'malformed_or_unreadable_runtime_evidence'
    evidence = workspace / 'runtime-evidence.json'
    result['evidence'] = str(evidence)
    evidence.write_text(json.dumps(result, indent=2) + '\n')
    evidence.chmod(0o600)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=install.DEFAULT_ROOT)
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args(argv)
    if not 30 <= args.timeout <= 300:
        parser.error('--timeout must be 30..300 seconds')
    try:
        result = run_runtime_smoke(args.root, args.timeout)
        print(json.dumps(result, indent=2))
        return 0 if result['ok'] else 1
    except (install.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        print(json.dumps({'ok': False, 'diagnostic_only': True, 'failure': 'runtime_preflight_failed; no secret output'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
