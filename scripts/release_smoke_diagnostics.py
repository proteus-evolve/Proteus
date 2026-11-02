"""Bounded, non-resumable smoke diagnostics; never copy auth/state/build directories."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proteus.core.continuity import _redact  # noqa: E402

ENTRY_FILES = {'pi': 'src/packages/coding-agent/src/cli.ts',
               'dsh': 'src/apps/cli/src/bin.ts'}
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DECODED_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_FILES = 256
MAX_INVALID_LINE_SAMPLES = 64
MAX_INDEX_BYTES = 512 * 1024


def edit_goal(harness: str) -> str:
    target = '/workspace/candidate/' + ENTRY_FILES[harness]
    return (
        f'REQUIRED TASK, do this before any other work: inspect {target}. '
        'Your running harness is frozen and read-only at /workspace; persistent edits '
        'belong only in /workspace/candidate. Do not use a relative src/ path or edit '
        '/workspace/src or /opt/src. If the target does not contain PROTEUS-NOTE, '
        'use your write or edit tool to insert exactly this comment on its own line '
        'immediately after the shebang: // PROTEUS-NOTE: CLI entry point; boots the agent session. '
        'Make no other source change. If the marker is already present, leave the source '
        'unchanged. Never execute or reload candidate source during a phase: Proteus '
        'validates it at the episode boundary and activates it in the next episode. '
        'Preserve a concise operational handoff including the absolute file path and '
        'whether the edit was performed, already present, or failed.'
    )


def _sensitive(key: str) -> bool:
    key = re.sub('[^a-z0-9]', '', key.lower())
    return key.endswith(('apikey', 'token', 'secret', 'password', 'authorization', 'privatekey',
                         'credential', 'credentials'))


def sanitize(value, secrets=()):
    """Reuse continuity redaction, removing credential fields and private reasoning."""
    if isinstance(value, dict):
        if value.get('type') in ('thinking', 'redacted_thinking', 'reasoning', 'reasoning-delta',
                                 'assistant/chunk', 'message_update', 'request/header',
                                 'text-chunks', 'reasoning-chunks', 'tool-call-chunks'):
            return {'diagnostic_omitted_event': value['type']}
        return {str(key): ('[REDACTED]' if _sensitive(str(key)) else
                          '[PRIVATE REASONING OMITTED]' if isinstance(item, (str, list, dict))
                          and ('reasoning' in str(key).lower()
                               or str(key).lower() in ('thinking', 'signature')) else sanitize(item, secrets))
                for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            value = value.replace(secret, '[REDACTED]')
    # Tool arguments often contain a second JSON object encoded as a string.
    if value.lstrip().startswith(('{', '[')):
        try:
            return json.dumps(sanitize(json.loads(value), secrets), ensure_ascii=False)
        except (ValueError, TypeError):
            pass
    value = _redact(value)
    value = re.sub(r'''(?i)(\bauthorization\s*:\s*)(?:Bearer\s+|Basic\s+)?[^\s"']+''',
                   r'\1[REDACTED]', value)
    value = re.sub(r'(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                   '[PRIVATE KEY OMITTED]', value)
    value = re.sub(r'(?i)(\b[A-Z_]*(?:API_KEY|TOKEN|PASSWORD|SECRET)\s*=\s*)[^\s,;]+',
                   r'\1[REDACTED]', value)
    return value


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def safe_path(path, base):
    try:
        relative = Path(path).relative_to(base)
        return (not base.is_symlink() and Path(path).resolve().is_relative_to(base.resolve())
                and not any((base / part).is_symlink() for part in (relative, *relative.parents)))
    except ValueError:
        return False


class Collector:
    def __init__(self, output, *, secrets=(), max_bytes=MAX_OUTPUT_BYTES):
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.secrets = tuple(secret for secret in secrets if secret)
        self.remaining = max_bytes
        self.records = []
        self.limit_reached = False

    def put(self, name, data, record):
        if len(data) > self.remaining:
            record['status'] = 'omitted_output_byte_limit'
            return
        if any(secret.encode() in data for secret in self.secrets):
            record['status'] = 'refused_remaining_credential'
            return
        destination = self.output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        self.remaining -= len(data)
        record.update(status='retained_sanitized_derivative', exported=name,
                      exported_sha256=hashlib.sha256(data).hexdigest(), exported_bytes=len(data))

    def capture(self, path, base, label, *, kind='json'):
        if len(self.records) >= MAX_FILES:
            self.limit_reached = True
            return
        path, base = Path(path), Path(base)
        record = {'source': sanitize(label, self.secrets), 'kind': kind}
        self.records.append(record)
        try:
            if not safe_path(path, base):
                record['status'] = 'refused_symlink'
                return
            if not path.is_file():
                record['status'] = 'missing'
                return
            record['original_bytes'] = path.stat().st_size
            if record['original_bytes'] > MAX_FILE_BYTES:
                record.update(status='omitted_input_byte_limit', original_sha256=None)
                return
            with path.open('rb') as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                record.update(status='omitted_input_byte_limit', original_sha256=None)
                return
            record['original_sha256'] = hashlib.sha256(raw).hexdigest()
            if kind == 'opaque':
                record['status'] = 'hash_only_opaque_usage_source_not_uploaded'
                return
            if kind == 'dsh':
                record['decoded_stream_completeness'] = 'not_claimed_for_diagnostic_capture'
                try:
                    from compression import zstd
                    reader = zstd.open(io.BytesIO(raw), 'rb')
                except ImportError:
                    import zstandard
                    reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw), read_across_frames=True)
                with reader:
                    raw = reader.read(MAX_DECODED_BYTES + 1)
                if len(raw) > MAX_DECODED_BYTES:
                    record['status'] = 'omitted_decompressed_byte_limit'
                    return
            if kind in ('jsonl', 'dsh'):
                lines, invalid = [], []
                invalid_count = valid_count = line_count = 0
                for number, line in enumerate(io.BytesIO(raw), 1):
                    line_count = number
                    line = line.rstrip(b'\r\n')
                    if not line.strip():
                        continue
                    try:
                        lines.append(json.dumps(sanitize(json.loads(line), self.secrets), ensure_ascii=False,
                                                allow_nan=False))
                        valid_count += 1
                    except (ValueError, TypeError):
                        invalid_count += 1
                        if len(invalid) < MAX_INVALID_LINE_SAMPLES:
                            invalid.append({'line': number, 'sha256': hashlib.sha256(line).hexdigest()})
                record.update(jsonl_line_count=line_count, jsonl_valid_line_count=valid_count,
                    invalid_line_count=invalid_count, invalid_lines=invalid,
                    invalid_line_samples_omitted=invalid_count - len(invalid),
                    invalid_line_samples_truncated=invalid_count > len(invalid))
                data = ('\n'.join(lines) + '\n').encode()
            elif kind == 'json':
                data = encoded(sanitize(json.loads(raw), self.secrets))
            else:
                data = sanitize(raw.decode('utf-8'), self.secrets).encode()
            name = 'files/' + hashlib.sha256(label.encode()).hexdigest() + ('.jsonl' if kind in ('jsonl', 'dsh') else '.txt')
            self.put(name, data, record)
        except Exception as exc:  # noqa: BLE001 — preserve per-file decode failures without printing payloads
            record.update(status='unavailable', error_type=type(exc).__name__)

    def checkpoints(self, run, harness, label):
        if len(self.records) >= MAX_FILES:
            self.limit_reached = True
            return
        git_dir = run / '.snapshot.git'
        record = {'source': label + '/snapshot-checkpoints', 'kind': 'checkpoint_projection'}
        self.records.append(record)
        if git_dir.is_symlink() or not git_dir.is_dir():
            record['status'] = 'missing_or_symlink_snapshot'
            return

        def git(*args):
            return subprocess.check_output(['git', '--no-pager', '--no-replace-objects',
                '--git-dir', str(git_dir), *args], stderr=subprocess.DEVNULL, timeout=10)

        try:
            log = git('log', '--format=%H %s').decode()
            commits = {}
            for line in log.splitlines():
                match = re.fullmatch(r'([0-9a-f]{40}) episode ([012]):.*', line)
                if match:
                    commits.setdefault(int(match[2]), match[1])
            value = {'episode_commits': commits, 'refs': git('for-each-ref',
                     '--format=%(refname) %(objectname)').decode(), 'entry_files': []}
            if 0 in commits and 1 in commits:
                value['ep0_to_ep1_src_name_status'] = git('diff', '--no-ext-diff', '--no-textconv',
                    '--name-status', commits[0], commits[1], '--', 'src/').decode()
            for episode, commit in sorted(commits.items()):
                entry = ENTRY_FILES[harness]
                identity = f'{commit}:{entry}'
                size = int(git('cat-file', '-s', identity))
                item = {'episode': episode, 'commit': commit, 'path': entry, 'bytes': size}
                value['entry_files'].append(item)
                if size <= MAX_FILE_BYTES:
                    data = git('show', identity)
                    item.update(original_sha256=hashlib.sha256(data).hexdigest(),
                                sanitized_text=sanitize(data.decode('utf-8'), self.secrets))
                else:
                    item.update(status='omitted_input_byte_limit', original_sha256=None)
            self.put('files/' + hashlib.sha256(label.encode()).hexdigest() + '-checkpoints.json',
                     encoded(sanitize(value, self.secrets)), record)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            record.update(status='unavailable', error_type=type(exc).__name__)

    def sweep(self, root, harness, label):
        root = Path(root).absolute()
        for name, kind in (('manifest.json', 'json'), ('seeds.jsonl', 'jsonl')):
            self.capture(root / name, root, label + '/' + name, kind=kind)
        if root.is_symlink() or not root.is_dir():
            return
        for path in sorted((root / 'progress').glob('*.jsonl')) if safe_path(root / 'progress', root) else ():
            self.capture(path, root, label + '/' + path.relative_to(root).as_posix(), kind='jsonl')
        # Run roots come from the owned layout, never from paths supplied in seeds.jsonl.
        for run in sorted((root / 'runs').iterdir()) if safe_path(root / 'runs', root) and (root / 'runs').is_dir() else ():
            if run.name.startswith('.') or not safe_path(run, root) or not run.is_dir():
                continue
            prefix = label + '/runs/' + run.name
            self.checkpoints(run, harness, prefix)
            patterns = ('traces/ep*.json', '.proteus-state/handoff.md', '.proteus-state/latest.md',
                        '.proteus-state/continuity.json',
                        '.proteus-state/controller-notice.md', '.proteus-state/handoffs/*.json')
            for pattern in patterns:
                if not safe_path(run / pattern.split('/')[0], root):
                    continue
                for path in sorted(run.glob(pattern)):
                    kind = 'json' if path.suffix == '.json' else 'text'
                    self.capture(path, root, label + '/' + path.relative_to(root).as_posix(), kind=kind)
            private = root / 'runs/.proteus-records' / run.name
            for name in ('episode_checkpoint.json', 'pending_candidate.json', 'eval_history.json'):
                self.capture(private / name, root, prefix + '/private/' + name)
            for path in sorted((private / 'native-inputs').glob('*')) if safe_path(private / 'native-inputs', root) else ():
                if path.suffix == '.json' or path.suffix == '.usage-source':
                    self.capture(path, root, prefix + '/native-inputs/' + path.name,
                                 kind='json' if path.suffix == '.json' else 'opaque')
                    if path.name.endswith('.input.json'):
                        outcome = path.with_name(path.name.removesuffix('.input.json') + '.outcome.json')
                        if not outcome.exists():
                            count = len(self.records)
                            self.capture(outcome, root, prefix + '/native-inputs/' + outcome.name)
                            if len(self.records) > count:
                                self.records[-1]['completion'] = 'unknown'
            state = run / ('.dsh-state/sessions' if harness == 'dsh' else '.pi-state')
            pattern = '**/session.jsonl.zstd' if harness == 'dsh' else '*.jsonl'
            for path in sorted(state.glob(pattern)) if safe_path(state, root) else ():
                self.capture(path, root, label + '/' + path.relative_to(root).as_posix(),
                             kind='dsh' if harness == 'dsh' else 'jsonl')


def bounded_index(index, secrets=()):
    """Keep counts/hashes/statuses; samples and verbose metadata yield to a fixed cap."""
    index = sanitize(index, secrets)
    index.update(index_byte_limit=MAX_INDEX_BYTES,
        invalid_line_sample_limit_per_source=MAX_INVALID_LINE_SAMPLES,
        invalid_line_hash_scope='line bytes without CR/LF terminators',
        invalid_line_count_scope='parsed source bytes only; missing or over-limit sources remain unknown',
        index_degradation='none')

    def render():
        records = index['records']
        count = sum(row.get('invalid_line_count', 0) for row in records)
        retained = sum(len(row.get('invalid_lines', [])) for row in records)
        index['invalid_line_summary'] = {'count': count, 'samples_retained': retained,
                                        'samples_omitted': count - retained}
        return encoded(index)

    data = render()
    if len(data) > MAX_INDEX_BYTES:
        index['index_degradation'] = 'invalid_line_samples_omitted_for_index_limit'
        for row in index['records']:
            if 'invalid_line_count' in row:
                row.update(invalid_lines=[], invalid_line_samples_omitted=row['invalid_line_count'],
                           invalid_line_samples_truncated=row['invalid_line_count'] > 0)
        data = render()
    if len(data) > MAX_INDEX_BYTES:
        index['index_degradation'] = 'bounded_metadata_projection'
        index['unprojected_sanitized_metadata_sha256'] = hashlib.sha256(data).hexdigest()
        context = encoded(index['execution_context'])
        index['execution_context'] = {'omitted_for_index_limit': True,
            'sanitized_sha256': hashlib.sha256(context).hexdigest(), 'sanitized_bytes': len(context)}
        keys = ('kind', 'status', 'original_bytes', 'original_sha256', 'exported', 'exported_sha256',
                'exported_bytes', 'completion', 'error_type', 'decoded_stream_completeness',
                'jsonl_line_count', 'jsonl_valid_line_count', 'invalid_line_count', 'invalid_lines',
                'invalid_line_samples_omitted', 'invalid_line_samples_truncated')
        projected = []
        for row in index['records']:
            source = str(row['source'])
            item = {key: row[key] for key in keys if key in row}
            item.update(source=source if len(source) <= 80 else source[:40] + '…' + source[-39:],
                        source_label_sha256=hashlib.sha256(source.encode()).hexdigest())
            projected.append(item)
        index['records'] = projected
        data = render()
    if len(data) > MAX_INDEX_BYTES:
        # Do not silently discard per-source hashes or write an oversized index.
        raise ValueError('critical diagnostic metadata exceeds the fixed index byte limit')
    return index, data


def collect(roots, output, harness, *, secrets=(), max_bytes=MAX_OUTPUT_BYTES, context=None):
    if any(Path(output).resolve().is_relative_to(Path(root).resolve()) for root in roots):
        raise ValueError('diagnostic output must be outside all sweeps')
    collector = Collector(output, secrets=secrets, max_bytes=max_bytes)
    for index, root in enumerate(roots):
        try:
            collector.sweep(root, harness, f'sweep-{index}')
        except (OSError, ValueError) as exc:
            if len(collector.records) < MAX_FILES:
                collector.records.append({'source': f'sweep-{index}', 'status': 'unavailable',
                                          'error_type': type(exc).__name__})
            else:
                collector.limit_reached = True
    index = {'schema': 'proteus.release-smoke.diagnostics.v1', 'harness': harness,
        'execution_context': context or {},
        'non_resumable_sanitized_diagnostics': True, 'provider_calls_by_collector': 0,
        'output_data_bytes': max_bytes - collector.remaining, 'output_data_byte_limit': max_bytes,
        'file_limit_reached': collector.limit_reached, 'records': collector.records,
        'cost_unknowns_are_not_zero': True,
        'omissions': 'No auth/settings/environment files, gateway databases, builds, full source trees, '
                     'snapshot repositories, opaque usage-source bytes, or private reasoning are copied.'}
    index, data = bounded_index(index, secrets)
    (collector.output / 'diagnostics.json').write_bytes(data)
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    goal = commands.add_parser('edit-goal')
    goal.add_argument('--harness', choices=tuple(ENTRY_FILES), required=True)
    capture = commands.add_parser('collect')
    capture.add_argument('--harness', choices=tuple(ENTRY_FILES), required=True)
    capture.add_argument('--root', type=Path, action='append', required=True)
    capture.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'edit-goal':
        print(edit_goal(args.harness))
    else:
        secrets = tuple(os.environ.get(key, '') for key in
            ('DEEPSEEK_API_KEY', 'DEEPSEEK_KEY', 'OPENAI_API_KEY', 'CODEX_API_KEY',
             'PROTEUS_GATEWAY_TOKEN', 'GITHUB_TOKEN', 'GH_TOKEN'))
        if any(args.output.resolve().is_relative_to(root.resolve()) for root in args.root):
            parser.error('diagnostic output must be outside all sweeps')
        context = {key: os.environ.get(key, 'unknown') for key in
            ('GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'SMOKE_BAKE_OUTCOME',
             'SMOKE_SWEEP_OUTCOME', 'SMOKE_SELF_EDIT_OUTCOME')}
        collect(args.root, args.output, args.harness, secrets=secrets, context=context)


if __name__ == '__main__':
    main()
