(()=>{'use strict';
const $=id=>document.getElementById(id);let busy=false;let current={};let dirty=false;
const base=new URL('./',location.href);
function message(text,error=false){$('message').textContent=text;$('message').className='visible'+(error?' error':'');}
async function api(path,body){const response=await fetch(new URL('api/'+path,base),{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:body===undefined?{}:{'Content-Type':'application/json','X-Fnmusic-Request':'1'},body:body===undefined?undefined:JSON.stringify(body)});let result;try{result=await response.json();}catch{throw Error('管理服务没有返回有效响应，请从飞牛桌面重新打开应用。');}if(!response.ok)throw Error(result.error||result.detail||'操作失败，请稍后重试');return result;}
function render(state){current=state;$('takeover').checked=state.takeover;document.querySelector('.takeover').classList.toggle('active',state.takeover);$('takeover-status').textContent=state.takeover?'在线扩展已接入':'官方直连';$('route-middle').textContent=state.takeover?'在线音源 · 飞牛中转':'官方音乐服务';$('route-end').textContent=state.takeover?'在线音乐 / 本地曲库':'本地曲库';$('source-name').textContent=state.source_name;$('source-version').textContent=state.source_version?'版本 '+state.source_version+(state.source_ready?' · 已就绪':' · 等待服务就绪'):'支持 .js 格式的 LX 自定义音源';$('source-dot').classList.toggle('ready',state.source_ready);if(!dirty){$('download-dir').value=state.download_dir;$('play-mode').value=state.play_mode;}$('connection').querySelector('span').textContent='管理服务已连接';$('connection').querySelector('i').style.background='#518b62';}
function lock(value){busy=value;document.querySelectorAll('button,input,select').forEach(el=>el.disabled=value);}
async function action(fn){if(busy)return;lock(true);try{await fn();}catch(error){message(error.message,true);if(current.takeover!==undefined)$('takeover').checked=current.takeover;}finally{lock(false);}}
$('source-file').addEventListener('change',event=>action(async()=>{const file=event.target.files[0];if(!file)return;if(file.size>2*1024*1024)throw Error('请选择不超过 2MB 的音源脚本。');message('正在加载音源，请稍候…');render(await api('source',{name:file.name,content:await file.text()}));event.target.value='';message('音源已保存。配置下载目录后，可以开启接管。');}));
$('settings-form').addEventListener('input',()=>{dirty=true;});
$('settings-form').addEventListener('change',()=>{dirty=true;});
$('settings-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{const saved=await api('settings',{download_dir:$('download-dir').value.trim(),play_mode:$('play-mode').value});dirty=false;render(saved);message('设置已保存。');});});
$('takeover').addEventListener('change',event=>{const enabled=event.target.checked;action(async()=>{message(enabled?'正在接入在线扩展…':'正在恢复官方直连…');render(await api('takeover',{enabled}));message(enabled?'在线扩展已开启，回到飞牛音乐搜索歌曲即可。':'已恢复官方直连，管理页面继续运行。');});});
async function refresh(){if(busy)return;try{render(await api('state'));}catch(error){$('connection').querySelector('span').textContent='管理服务连接失败';message(error.message,true);}}
refresh();setInterval(refresh,10000);
})();
