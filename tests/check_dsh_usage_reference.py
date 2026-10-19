"""Compare DSH storage and main-loop accounting with pinned upstream pure functions.

Node supplies schema/brand-only imports and refuses to run the unrelated surface
projection. This is an isolated pure-function oracle, not a DSH CLI/provider test.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from check_native_dsh_package import compress, events, raw
from proteus.core.native_dsh_storage import unpack_record
from proteus.core.native_dsh_usage import capture_dsh_sources
from proteus.core.native_usage import dsh_usage


PINS = {
    'chunks': ('dsh-packages_core_session_src_chunk-rows.ts', 'c9a877b64c4a2bc8ecaa21e15c637c02f66baaa19c41227a4f5a45bf20bfad4d'),
    'usage': ('dsh-packages_llm_token-meter_src_usage-projection.ts', '11207f66668008840e4994be9ff819a7c58b4f791d89cbe4b958af47b80bb81a'),
}
PROGRAM = '''import vm from 'node:vm';import {stripTypeScriptTypes} from 'node:module';
let data='';for await(const piece of process.stdin)data+=piece;const input=JSON.parse(data);
const context=vm.createContext({});let chain;
chain=new Proxy(function(){},{get:()=>chain,apply:()=>chain});
const fail=()=>{throw new Error('unrelated runtime helper invoked')};
const module=new vm.SourceTextModule(stripTypeScriptTypes(input.source),{context});
await module.link(async(specifier)=>{
 let exports;
 if(specifier==='zod')exports={z:chain};
 else if(specifier==='./surface-projection.ts')exports={foldSurfaceProjection:fail};
 else if(specifier==='@deepseek-ai/dsh-llm')exports={CallId:x=>x,assertNever:fail};
 else throw new Error('unexpected import '+specifier);
 return new vm.SyntheticModule(Object.keys(exports),function(){for(const [key,value]of Object.entries(exports))this.setExport(key,value)},{context});
});await module.evaluate();
if(input.kind==='chunks'){
 const packed=module.namespace.packChunkRuns(input.events);
 console.log(JSON.stringify({packed,expanded:packed.flatMap(module.namespace.decodeStorageRecord)}));
}else{
 const projection=module.namespace.tokenUsageProjectionDefinition;
 let state=projection.init();for(const event of input.events)state=projection.apply(state,event);
 console.log(JSON.stringify(projection.view(state)));
}'''


def reference(root, kind, values, node):
    filename, digest = PINS[kind]
    source = (root/filename).read_bytes()
    assert hashlib.sha256(source).hexdigest() == digest
    process = subprocess.run([node, '--experimental-vm-modules', '--input-type=module', '-e', PROGRAM],
        input=json.dumps({'source':source.decode(), 'kind':kind, 'events':values}),
        capture_output=True, text=True, check=True)
    return json.loads(process.stdout)


def check(root, node='node'):
    root = Path(root)
    for kind in ('text-delta', 'reasoning-delta', 'tool-call-delta'):
        values = []
        for index, text in enumerate(('a', ' ', 'b')):
            chunk = {'type':kind, 'index':0}
            if kind == 'tool-call-delta':
                chunk.update(id='call', name='fixture', argumentsDelta=text)
            else:
                chunk['text'] = text
            values.append({'type':'assistant/chunk','seq':index,'time':(100,104,102)[index],
                           'data':{'turn':1,'step':1,'chunk':chunk}})
        expected = reference(root, 'chunks', values, node)
        assert len(expected['packed']) == 1
        actual = [event for record in expected['packed'] for event in unpack_record(record)]
        assert actual == expected['expanded'] == values
    results = []
    for scale in (0,1,5):
        values = [event for event in events() if not event['type'].startswith('compaction/')]
        for event in values:
            usage = event['data'].get('usage', event['data'].get('chunk', {}).get('usage'))
            if usage:
                usage['cacheReadTokens'] *= scale
                usage['cacheWriteTokens'] *= scale
        expected = reference(root, 'usage', values, node)
        with tempfile.TemporaryDirectory(prefix='proteus-dsh-reference-') as directory:
            path = Path(directory)/'session.jsonl.zstd'
            path.write_bytes(compress(raw(values)))
            actual = dsh_usage(capture_dsh_sources([path],{},root=Path(directory)),transport_complete=True)['reported']
        assert actual['input_tokens'] == expected['uncachedInputTokens']+expected['cacheReadTokens']+expected['cacheWriteTokens']
        assert actual['output_tokens'] == expected['outputTokens']
        assert actual['model_calls'] is None and actual['cost_usd'] is None
        results.append({'cache_scale':scale, 'input_tokens':actual['input_tokens'], 'output_tokens':actual['output_tokens']})
    print(json.dumps({'storage_shapes':3,'usage_profiles':results,'reference_hashes':PINS}))
    print('Pinned DSH storage packing/expansion and latest-sample token projection agree; auxiliary gaps tested separately.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--node', default='node')
    args = parser.parse_args()
    check(args.source_root, args.node)
