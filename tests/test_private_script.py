import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_private_lx_script_contract_without_network():
    path = os.environ.get("PRIVATE_LX_SCRIPT")
    if not path:
        pytest.skip(
            "Set PRIVATE_LX_SCRIPT for a local-only private script contract check"
        )
    if not shutil.which("node"):
        pytest.skip("Node is required")
    harness = r"""
const fs=require('node:fs'),vm=require('node:vm');
let handler,init,requests=0;
const lx={EVENT_NAMES:{request:'request',inited:'inited',updateAlert:'updateAlert'},env:'desktop',version:'2.0.0',
  on:(event,fn)=>{if(event==='request')handler=fn},
  send:(event,value)=>{if(event==='inited')init=value},
  request:(url,options,callback)=>{requests++;callback(null,{statusCode:200,body:{code:200,url:'https://fixture.invalid/audio.flac'}});return ()=>{};}
};
const context=vm.createContext({lx,console:{log(){},error(){},warn(){}},URL,URLSearchParams});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context,{timeout:1000});
(async()=>{
 if(!init?.status||!handler)throw new Error('initialization');
 const sources=Object.keys(init.sources);
 const source=sources[0];const qualities=init.sources[source].qualitys;
 const url=await handler({action:'musicUrl',source,info:{type:qualities[0],musicInfo:{hash:'fixture',songmid:'fixture',id:'fixture'}}});
 if(url!=='https://fixture.invalid/audio.flac')throw new Error('resolve');
 process.stdout.write(JSON.stringify({sources,requests,initialized:true}));
})().catch(()=>{process.stderr.write('Private script contract failed');process.exitCode=1});
"""
    result = subprocess.run(
        ["node", "-e", harness, str(Path(path).resolve())],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, (
        "Private script contract failed (contents and credentials omitted)"
    )
    data = json.loads(result.stdout)
    assert data["initialized"] and data["requests"] >= 1
    assert {"kw", "kg", "tx", "wy", "mg"}.issubset(data["sources"])
