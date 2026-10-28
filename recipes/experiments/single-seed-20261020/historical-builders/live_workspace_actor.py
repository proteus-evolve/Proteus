"""Explicit pilot-only JSON-file proposer; model code executes only in Harbor."""
from dataclasses import replace
import json
from pathlib import Path, PurePosixPath

from proteus.core.contracts import PluginRef, require
from proteus.core.plugins import Plugin, implementation_hash
from proteus.execution.actors import ChatActor, ChatOptions, validate_chat


class JsonFileProposer(ChatActor):
    def execute(self, spec, request, workspace):
        task=workspace/'task'
        context={}
        total=0
        for directory in ('input','history'):
            for path in sorted((task/directory).rglob('*')):
                if not path.is_file() or path.is_symlink(): continue
                if path.suffix not in ('.py','.json','.md','.jsonl','.txt'): continue
                data=path.read_text(errors='replace')
                # The view is already scoped by Proteus; cap only this coding actor's context.
                if total+len(data)>120000: continue
                context[path.relative_to(task).as_posix()]=data
                total+=len(data)
        prompt=request.prompt+'\n\nREAD ONLY WORKSPACE FILES\n'+json.dumps(context,ensure_ascii=False)
        prompt+='''\n\nPilot transport adaptation: you cannot execute shell commands.
Read the supplied baseline files and authorized history and return ONLY a JSON
object {"files":{"relative/path":"complete UTF-8 source text"}}. Do not return
markdown fences. Create one or more NEW memory systems, not fixture responses.
The pending_eval.json file MUST contain {"iteration":1,"candidates":[
{"name":"new_unique_name","file":"agents/new_unique_name.py","axis":"exploration",
"base_system":"fewshot_all","hypothesis":"your hypothesis","components":["memory"]}]}.
Each agent must subclass text_classification.memory_system.MemorySystem using
relative imports as in the supplied baseline, implement predict, learn_from_batch,
get_state and set_state, and use self.call_llm for all inference. Return no SDK
usage, network calls or host reads. Your Python will be evaluated in a credential-free
container. Preserve serialization and exact final_answer extraction. Only output
agents/*.py, reports/*.md and pending_eval.json. Do not include private test data.
'''
        result=super().execute(spec,replace(request,prompt=prompt),workspace)
        text=result.text.strip()
        if text.startswith('```'):
            text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
        output=json.loads(text)
        require(set(output)=={'files'} and isinstance(output['files'],dict),'file proposer needs files mapping')
        require(0<len(output['files'])<=30,'invalid proposer file count')
        for relative,content in output['files'].items():
            path=PurePosixPath(relative)
            require(not path.is_absolute() and '..' not in path.parts and type(content) is str,'unsafe output path')
            require(relative=='pending_eval.json' or
                (path.parts[0] in ('agents','reports') and path.suffix in ('.py','.md')),'unsupported output file')
            if path.suffix=='.py': compile(content,relative,'exec')
            dest=task/'output'/relative
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_text(content)
        return replace(result,trace=({'kind':'pilot-json-file-proposer','read_files':list(context),
            'output_files':list(output['files']),'context_characters':total},))


def register(registry):
    registry.register(Plugin(ref=PluginRef(name='pilot.deepseek_workspace',version='1'),kind='actor',
        options=ChatOptions,factory=JsonFileProposer,source='pilot-runtime',
        implementation_hash=implementation_hash({'actor':Path(__file__)}),
        capabilities=('actor.execute','session.fresh','calls.bounded','actor.workspace','request.timeout'),
        validate=validate_chat))
