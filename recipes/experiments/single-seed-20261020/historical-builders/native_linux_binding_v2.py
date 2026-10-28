"""Transport-only Linux binding over unmodified staged PiHarness transactions."""
from dataclasses import dataclass,field
import hashlib,json,os
from pathlib import Path
import time
from proteus.adapters.pi import PiHarness,SOURCE_TAR
from proteus.core.contracts import DIGEST,TEXT,require
from proteus.core.serialization import Contract,content_hash
from proteus.sandbox import DockerSandbox,SandboxConfig

class LoggedSandbox(DockerSandbox):
    def __init__(self,config,case):
        super().__init__(config);self.case=Path(case)
    def run(self,run_root,command,**kwargs):
        self.case.mkdir(parents=True,exist_ok=True)
        with (self.case/'admission.jsonl').open('a') as file:
            file.write(json.dumps({'time_ns':time.time_ns(),'argv_sha256':hashlib.sha256(json.dumps(command).encode()).hexdigest(),'image':self.config.image,'network':self.config.network,'CPU':self.config.cpus,'memory':self.config.mem_limit})+'\n')
        return super().run(run_root,command,**kwargs)

class BudgetedPi(PiHarness):
    def __init__(self,config):
        self.control_config=config;case=Path(config['case']);models=Path(config['models_dir'])
        sandbox=LoggedSandbox(SandboxConfig(image=config['image_id'],network='bridge',cpus='1',mem_limit='2g',user=f'{os.getuid()}:{os.getgid()}',
            env_passthrough=('DEEPSEEK_API_KEY',),env={'PI_CODING_AGENT_DIR':'/native-agent','HOME':'/tmp/native-home'},
            extra_args=('--label','org.proteus.experiment='+config['label'],'--cap-drop=ALL','--security-opt=no-new-privileges:true','--pids-limit=256',
                '--add-host','host.docker.internal:host-gateway','-v',str(models)+':/native-agent',
                '-v',str(models/'models.json')+':/native-agent/models.json:ro')),case)
        super().__init__(image=config['image_id'],provider='proteus-budgeted',model='deepseek-flash',key=Path(config['local_token_file']).read_text().strip(),sandbox=sandbox,phase_timeout_s=900)
    def _extract_self_code(self,dest):
        if dest.exists() and any(dest.iterdir()):return
        dest.mkdir(parents=True,exist_ok=True)
        sandbox=LoggedSandbox(SandboxConfig(image=self.image,network='none',cpus='1',mem_limit='2g',user=f'{os.getuid()}:{os.getgid()}',
            extra_args=('--entrypoint','sh','--pids-limit=256','--cap-drop=ALL','--security-opt=no-new-privileges:true','--label','org.proteus.experiment='+self.control_config['label'])),self.control_config['case'])
        result=sandbox.run(dest.parent,['-c',f'tar -xf {SOURCE_TAR} -C /proteus-out --strip-components=1'],env={},timeout_s=300,mounts=((str(dest),'/proteus-out'),))
        require(result.returncode==0,'Exact upstream H0 extraction failed')

@dataclass(frozen=True,kw_only=True)
class Options(Contract):
    config_path:str=field(metadata=TEXT)
    config_hash:str=field(metadata=DIGEST)
class Actor:
    def __init__(self,options):self.options=options
    def adapter(self,spec):
        config=json.loads(Path(self.options.config_path).read_text())
        require(content_hash(config)==self.options.config_hash,'Frozen native transport config changed')
        return BudgetedPi(config)

def model_configuration(port):
    provider={'baseUrl':f'http://host.docker.internal:{port}/v1','api':'openai-completions','apiKey':'$DEEPSEEK_API_KEY','authHeader':True,
        'compat':{'supportsDeveloperRole':False,'supportsReasoningEffort':False,'maxTokensField':'max_tokens'},
        'models':[{'id':'deepseek-flash','name':'Common DeepSeek through original Mac budget authority','reasoning':False,'input':['text'],'contextWindow':128000,'maxTokens':8192,
            'samplingParams':{'temperature':0,'thinking':{'type':'disabled'}},'cost':{'input':.3,'output':1.2,'cacheRead':.006,'cacheWrite':.3}}]}
    return {'providers':{'proteus-budgeted':provider}}
