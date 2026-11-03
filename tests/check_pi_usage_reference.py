"""Compare positive Pi usage totals with the exact pinned upstream TypeScript fold.

Supply usage-totals.ts from earendil-works/pi commit
914cf1472e715297caa30db4b9535d534a9eb718; Node must support TypeScript stripping.
This check never invokes a model or the Pi CLI.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from check_native_pi_package import entries, raw
from proteus.core.native_pi_usage import capture_pi_sources
from proteus.core.native_usage import pi_usage

REFERENCE_SHA256 = 'e15cef84e1a54aa81e9ebbaefc5c34aab342b687d0cb16db52fbe4accac8c956'


def check(source, node='node'):
    source = Path(source).resolve()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == REFERENCE_SHA256
    program = '''const {getUsageCostBreakdown}=await import(process.argv[1]);
let input='';for await (const chunk of process.stdin) input+=chunk;
console.log(JSON.stringify(getUsageCostBreakdown(JSON.parse(input))));'''
    cases = []
    for cache_scale, output_scale, files in ((1,1,1), (10,2,1), (0,3,1), (2,4,2)):
        with tempfile.TemporaryDirectory(prefix='proteus-pi-reference-') as directory:
            paths, combined = [], []
            for index in range(files):
                values = copy.deepcopy(entries())
                for entry in values[1:]:
                    target = entry['message'] if entry['type'] == 'message' else entry
                    usage = target['usage']
                    usage['cacheRead'] *= cache_scale
                    usage['cacheWrite'] *= cache_scale
                    usage['cacheWrite1h'] = usage['cacheWrite']
                    usage['output'] *= output_scale
                    usage['totalTokens'] = sum(usage[key] for key in ('input','cacheRead','cacheWrite','output'))
                path = Path(directory)/f'{index}.jsonl'
                path.write_bytes(raw(values))
                paths.append(path)
                combined.extend(values)
            ours = pi_usage(capture_pi_sources(paths, {}), transport_complete=True)['reported']
            process = subprocess.run([node, '--input-type=module', '-e', program, source.as_uri()],
                input=json.dumps(combined), capture_output=True, text=True, check=True)
            expected = sum(row['tokens'] for row in json.loads(process.stdout))
            assert ours['input_tokens']+ours['output_tokens'] == expected
            assert ours['cost_usd'] is None and ours['model_calls'] is None
            cases.append({'files':files,'cache_scale':cache_scale,'output_scale':output_scale,'tokens':expected})
    print(json.dumps({'reference_sha256':REFERENCE_SHA256,'cases':cases}))
    print('Pinned upstream usage fold agrees for four positive-token profiles; ambiguous SDK zero defaults deliberately remain unknown.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--node', default='node')
    args = parser.parse_args()
    check(args.source, args.node)
