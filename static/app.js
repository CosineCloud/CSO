'use strict';
let state=null,page='overview';
let operationsTimer=null,operationsController=null,operationsGeneration=0,operationsOffset=0,operationsView=null,operationsDirty=false,operationsStateRefresh=null;
const operationsLimit=50;
const operationControlRequests=new Set();
let operationDetailsView=null,operationDetailsTimer=null,operationDetailsController=null,operationDetailsGeneration=0;
let operationsSearchTimer=null,settingsController=null,settingsView=null;
const operationsFilters={status:'',q:'',action:''};
const $=id=>document.getElementById(id),GB=1000**3;const titles={overview:['Dashboard',''],spaces:['Storage spaces',''],clouds:['Cloud accounts',''],files:['Files',''],operations:['Operations',''],settings:['Settings',''],team:['Users',''],api:['API reference','']};
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}function append(n,...children){children.flat().forEach(c=>{if(c)n.append(c)});return n}function btn(text,action,cls='button secondary'){if(state&&state.user.role!=='admin'&&(action===createCloud||action===createSpace))return null;const n=el('button',cls,text);n.type='button';n.onclick=action;return n}function badge(text,type='blue'){return el('span','badge '+type,text)}function bytes(n){n=Number(n)||0;if(n===0)return '0 B';const units=['B','KB','MB','GB','TB'];const i=Math.min(Math.floor(Math.log(n)/Math.log(1000)),4);return (n/1000**i).toLocaleString(undefined,{maximumFractionDigits:1})+' '+units[i]}function bar(used,total){const n=el('div','progress'),fill=el('span');fill.style.width=Math.min(100,total?used/total*100:0)+'%';n.append(fill);return n}function notice(msg){$('toast').textContent=msg;$('toast').hidden=false;setTimeout(()=>$('toast').hidden=true,4000)}async function api(path,options={}){const headers={'X-CSRF-Token':state?.csrf,...options.headers};if(options.body&&!(options.body instanceof FormData))headers['Content-Type']='application/json';const r=await fetch(path,{...options,headers});if(r.status===401){location.href='/';throw Error('Please sign in again')}const data=await r.json();if(!r.ok)throw Error(data.error||'The request could not be completed.');return data}async function refresh(){try{state=await api('/api/admin/state');$('username').textContent=state.user.username;$('role').textContent=state.user.role;$('initial').textContent=state.user.username.slice(0,1).toUpperCase();$('error').hidden=true;render()}catch(e){$('error').textContent=e.message;$('error').hidden=false}}function panel(title,subtitle,action){const p=el('section','panel'),head=el('div','panel-head'),left=el('div');append(left,el('h2','',title),subtitle?el('p','',subtitle):null);append(head,left,action);p.append(head);return p}function empty(title,text,action){return append(el('div','empty'),el('h3','',title),el('p','',text),action)}function cloudBy(id){return state.clouds.find(c=>String(c.id)===String(id))}function spaceBy(id){return state.spaces.find(s=>String(s.id)===String(id))}function navigate(next){if(operationDetailsView)closeModal();if((next==='operations'||next==='settings')&&state?.user.role!=='admin')return;const previous=page;stopOperationsPolling();page=next;render();if(previous==='operations'&&next!=='operations'&&operationsDirty){operationsDirty=false;refreshAfterOperations()}}function cloudRow(c){const row=el('div','cloud-row');const title=append(el('div','row-title'),el('div','cloud-icon',c.provider==='google'?'△':c.provider==='dropbox'?'◇':'▣'),append(el('div'),el('div','row-name',c.name),el('div','row-sub',c.provider==='google'?'Google Drive':c.provider==='dropbox'?'Dropbox':c.provider==='mega'?'MEGA':c.provider==='b2'?'Backblaze B2':'Local storage')));append(row,append(el('div','row-top'),title,badge('Configured','blue')),bar(c.used,c.capacity),append(el('div','row-meta'),el('span','',bytes(c.used)+' used'),el('span','',bytes(c.capacity)+' capacity')));if(page==='clouds'&&state.user.role==='admin')row.append(append(el('div','row-meta'),el('span','',''),append(el('div','actions'),btn('Test drive',()=>testSavedCloud(c),'text-button'),c.provider==='google'?btn('Reauthorize',()=>reauthorizeCloud(c),'text-button'):null,c.provider!=='b2'?btn('Refresh capacity',()=>refreshCapacity(c),'text-button'):null,btn('Edit account',()=>editCloud(c),'text-button'),btn('Delete account',()=>deleteCloud(c),'text-button danger'))));return row}function spaceRow(s){const row=el('div','space-row'),cloud=cloudBy(s.cloud_id);append(row,append(el('div','row-top'),append(el('div','row-title'),el('div','cloud-icon','▣'),append(el('div'),el('div','row-name',s.name),el('div','row-sub',cloud?'Pinned to '+cloud.name:'Cloud assigned on first upload'))),badge(s.encrypted?'Encrypted':'Standard',s.encrypted?'green':'gray')),bar(s.used,s.quota),append(el('div','row-meta'),el('span','',bytes(s.used)+' of '+bytes(s.quota)),el('span','',s.bandwidth_limit?bytes(s.bandwidth_used)+' / '+bytes(s.bandwidth_limit)+' monthly transfer':'Unlimited monthly transfer')));if(page==='spaces'){const tools=el('div','row-meta');append(tools,el('span','',state.files.filter(f=>String(f.space_id)===String(s.id)).length+' files'),state.user.role==='admin'?append(el('div','actions'),btn('Edit limits',()=>editLimits(s),'text-button'),btn('Rotate API key',()=>rotate(s),'text-button'),btn('Delete space',()=>deleteSpace(s),'text-button danger')):null);row.append(tools)}return row}function overview(){const root=el('div');let capacity=state.clouds.reduce((n,c)=>n+c.capacity,0),used=state.clouds.reduce((n,c)=>n+c.used,0);const metrics=el('div','summary-grid');const a=el('div','summary-item');append(a,append(el('div','summary-label'),el('span','','Storage capacity'),badge('All clouds','blue')),el('div','summary-value',bytes(capacity)),bar(used,capacity),el('div','summary-foot',bytes(used)+' used · '+bytes(Math.max(0,capacity-used))+' available'));const b=append(el('div','summary-item'),el('div','summary-label','Connected clouds'),el('div','summary-value',String(state.clouds.length)),el('div','summary-foot',state.clouds.length+' configured accounts'));const c=append(el('div','summary-item'),el('div','summary-label','Storage spaces'),el('div','summary-value',String(state.spaces.length)),el('div','summary-foot',state.files.length+' files'));append(metrics,a,b,c);root.append(metrics);const cols=el('div','dashboard-columns'),left=el('div'),right=el('div');const spaces=panel('Storage spaces',state.spaces.length+' configured',btn('View all →',()=>navigate('spaces'),'text-button'));state.spaces.length?state.spaces.slice(0,4).forEach(s=>spaces.append(spaceRow(s))):spaces.append(empty('No storage spaces','Create a space to configure storage limits and an API key.',btn('＋ Create space',createSpace,'button primary')));left.append(spaces);const files=panel('Recent files',state.files.length+' files',btn('View all →',()=>navigate('files'),'text-button'));files.append(fileTable(state.files.slice(0,5)));left.append(files);const clouds=panel('Cloud accounts',state.clouds.length+' configured',btn('＋ Connect',createCloud,'text-button'));state.clouds.length?state.clouds.forEach(c=>clouds.append(cloudRow(c))):clouds.append(empty('No cloud accounts configured','Connect a provider or configure local storage.',btn('Connect a cloud',createCloud)));right.append(clouds);append(cols,left,right);root.append(cols);return root}function fileTable(files){if(!files.length)return empty('No files','Upload a file or use the REST API.',state.spaces.length?btn('Upload a file',upload):null);const wrap=el('div','table-wrap'),table=el('table'),thead=el('thead'),hr=el('tr');['File name','Space','Size','Actions'].forEach(t=>hr.append(el('th','',t)));thead.append(hr);table.append(thead);const body=el('tbody');files.forEach(f=>{const row=el('tr'),name=el('td');name.append(append(el('div','file-name'),el('span','file-icon','▤'),el('span','',f.path)));const actions=el('td');const down=el('a','','Download');down.href='/api/admin/files/'+encodeURIComponent(f.id)+'/download';append(actions,append(el('div','actions'),down,btn('Delete',()=>deleteFile(f),'text-button')));append(row,name,el('td','muted',spaceBy(f.space_id)?.name||'—'),el('td','muted',bytes(f.size)),actions);body.append(row)});table.append(body);wrap.append(table);return wrap}const filesViewState={query:'',expanded:new Set()};
function buildFileTree(files){
  const roots=new Map();
  for(const file of files){
    const space=spaceBy(file.space_id),cloudId=file.cloud_id??space?.cloud_id,cloud=cloudBy(cloudId);
    const driveKey=cloudId==null?'unassigned':String(cloudId);
    if(!roots.has(driveKey))roots.set(driveKey,{key:JSON.stringify(['drive',driveKey]),name:cloud?.name||(cloudId==null?'Unassigned drive':'Unavailable drive'),kind:'drive',folders:new Map(),files:[],count:0,size:0});
    const drive=roots.get(driveKey),spaceKey=String(file.space_id);
    if(!drive.folders.has(spaceKey))drive.folders.set(spaceKey,{key:JSON.stringify(['space',driveKey,spaceKey]),name:space?.name||'Unavailable storage space',kind:'space',folders:new Map(),files:[],count:0,size:0});
    let node=drive.folders.get(spaceKey);const ancestors=[drive,node],parts=file.path.split('/');
    for(let i=0;i<parts.length-1;i++){
      const part=parts[i];
      if(!node.folders.has(part))node.folders.set(part,{key:JSON.stringify(['folder',driveKey,spaceKey,...parts.slice(0,i+1)]),name:part||'/',kind:'folder',folders:new Map(),files:[],count:0,size:0});
      node=node.folders.get(part);ancestors.push(node);
    }
    node.files.push({file,name:parts.at(-1)||file.path});
    for(const ancestor of ancestors){ancestor.count++;ancestor.size+=Number(file.size)||0;}
  }
  return roots;
}
function fileTree(files,searching=false){
  const tree=el('div','file-tree');
  const sorted=items=>[...items].sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true,sensitivity:'base'}));
  function branch(node){
    const details=el('details','file-tree-branch file-tree-'+node.kind),summary=el('summary','file-tree-summary');
    details.open=searching||filesViewState.expanded.has(node.key);
    const marker=el('span','file-tree-marker',node.kind==='drive'?'▣':node.kind==='space'?'▦':'▱');marker.setAttribute('aria-hidden','true');
    append(summary,marker,el('span','file-tree-label',node.name),el('span','file-tree-count',node.count+' '+(node.count===1?'file':'files')),el('span','file-tree-size',bytes(node.size)));
    summary.title=node.name;details.append(summary);
    details.addEventListener('toggle',()=>{if(!searching){if(details.open)filesViewState.expanded.add(node.key);else filesViewState.expanded.delete(node.key);}});
    const children=el('div','file-tree-children');
    sorted(node.folders.values()).forEach(child=>children.append(branch(child)));
    sorted(node.files).forEach(({file,name})=>{
      const row=el('div','file-tree-file'),label=el('span','file-tree-label',name);label.title=file.path;
      const icon=el('span','file-tree-marker','▤');icon.setAttribute('aria-hidden','true');
      const down=el('a','','Download');down.href='/api/admin/files/'+encodeURIComponent(file.id)+'/download';down.setAttribute('aria-label','Download '+file.path);
      const remove=btn('Delete',()=>deleteFile(file),'text-button danger');remove.setAttribute('aria-label','Delete '+file.path);
      append(row,icon,label,el('span','file-tree-size',bytes(file.size)),append(el('div','actions'),down,remove));children.append(row);
    });
    details.append(children);return details;
  }
  sorted(buildFileTree(files).values()).forEach(node=>tree.append(branch(node)));
  return tree;
}
function filesPage(){
  const body=el('div'),tools=el('div','section-tools file-library-tools'),search=el('input'),holder=panel('File library','');
  search.type='search';search.placeholder='Search files, folders, drives…';search.setAttribute('aria-label','Search files, folders, drives and storage spaces');search.value=filesViewState.query;
  const count=holder.querySelector('.panel-head p')||el('p');holder.querySelector('.panel-head>div').append(count);
  count.setAttribute('role','status');count.setAttribute('aria-live','polite');
  const contents=el('div');holder.append(contents);
  function update(){
    const query=filesViewState.query.trim().toLocaleLowerCase();
    const filtered=state.files.filter(file=>{const space=spaceBy(file.space_id),cloud=cloudBy(file.cloud_id??space?.cloud_id);return !query||[file.path,space?.name||'',cloud?.name||''].some(value=>value.toLocaleLowerCase().includes(query));});
    count.textContent=query?filtered.length+' of '+state.files.length+' files match':state.files.length+' files · Grouped by drive and storage space';
    contents.replaceChildren(!state.files.length?empty('No files','Upload a file or use the REST API.',state.spaces.length?btn('Upload a file',upload):null):!filtered.length?empty('No matching files','Try a different file, folder, drive or storage space name.'):fileTree(filtered,Boolean(query)));
  }
  search.oninput=()=>{filesViewState.query=search.value;update();};
  append(tools,search);append(body,tools,holder);update();return body;
}

function render(){if(!state)return;stopOperationsPolling();if(settingsController){settingsController.abort();settingsController=null;}if((page==='operations'||page==='settings')&&state.user.role!=='admin')page='overview';document.querySelectorAll('[data-admin-only]').forEach(n=>n.hidden=state.user.role!=='admin');document.querySelectorAll('#nav button').forEach(n=>n.classList.toggle('active',n.dataset.page===page));$('breadcrumb').textContent=titles[page][0];$('page-title').textContent=titles[page][0];$('page-subtitle').textContent=titles[page][1];$('page-subtitle').hidden=!titles[page][1];const primary=$('primary');primary.hidden=false;primary.textContent=page==='clouds'?'＋ Connect cloud':page==='files'?'↑ Upload file':page==='team'?'＋ Invite member':'＋ Create storage space';primary.onclick=page==='clouds'?createCloud:page==='files'?upload:page==='team'?invite:createSpace;if(page==='api'||page==='operations'||page==='settings'||(state.user.role!=='admin'&&page!=='files'))primary.hidden=true;let body;if(page==='overview')body=overview();if(page==='spaces'){body=panel('Storage spaces',state.spaces.length+' configured spaces');state.spaces.length?state.spaces.forEach(s=>body.append(spaceRow(s))):body.append(empty('No storage spaces','Create a space to configure storage limits and an API key.',btn('Create storage space',createSpace,'button primary')))}if(page==='clouds'){body=panel('Cloud accounts',state.clouds.length+' configured accounts');state.clouds.length?state.clouds.forEach(c=>body.append(cloudRow(c))):body.append(empty('No cloud accounts configured','Connect a provider or configure local storage.',btn('Connect cloud',createCloud,'button primary')))}if(page==='files')body=filesPage();if(page==='team'){body=panel('Users','Administrators manage access. Managers manage files.');if(state.user.role!=='admin')body.append(empty('Administrator access required','Contact an administrator to manage users.'));else{const table=el('table'),head=el('tr');['User','Role','Actions'].forEach(x=>head.append(el('th','',x)));table.append(head);state.users.forEach(u=>{const actions=el('td');if(String(u.id)!==String(state.user.id)&&u.username!==state.user.username)append(actions,append(el('div','actions'),btn(u.role==='revoked'?'Restore access':'Change role',()=>changeRole(u),'text-button'),u.role!=='revoked'?btn('Revoke access',()=>revokeUser(u),'text-button'):null));else actions.append(el('span','muted','You'));table.append(append(el('tr'),el('td','',u.username),append(el('td'),badge(u.role,u.role==='admin'?'blue':u.role==='revoked'?'amber':'gray')),actions))});body.append(table)}}if(page==='api')body=apiGuide();if(page==='operations')body=operationsPage();if(page==='settings')body=settingsPage();$('content').replaceChildren(body);if(page==='operations')loadOperations();if(page==='settings')loadSettings()}
function field(name,label,type='text',value='',hint=''){const l=el('label','',label),input=el('input');input.name=name;input.type=type;if(type==='checkbox'){l.className='checkbox';input.checked=!!value;l.prepend(input)}else{input.value=value;l.append(input)}if(hint)l.append(el('small','',hint));return l}function selectField(name,label,options){const l=el('label','',label),s=el('select');s.name=name;options.forEach(([value,text])=>{const o=el('option','',text);o.value=value;s.append(o)});l.append(s);return l}function modal(title,fields,submit,label='Save'){stopOperationDetails();const form=$('modal-form');form.reset();$('modal-body').oninput=null;$('modal-title').textContent=title;$('modal-body').replaceChildren(...fields);$('modal-error').hidden=true;$('modal-submit').classList.remove('danger');$('modal-submit').textContent=label;$('modal-submit').hidden=!submit;$('cancel-modal').textContent=submit?'Cancel':'Done';form.onsubmit=async e=>{e.preventDefault();if(!submit)return;const b=$('modal-submit');if(b.disabled)return;b.disabled=true;try{await submit(new FormData(form),form)}catch(err){$('modal-error').textContent=err.message;$('modal-error').hidden=false}finally{b.disabled=false}};$('modal').showModal()}function closeModal(){stopOperationDetails();$('modal').close()}function secret(title,value,description){modal(title,[el('p','form-hint',description),el('div','secret-value',value),btn('Copy to clipboard',async()=>{try{await navigator.clipboard.writeText(value);notice('Copied to clipboard')}catch{notice('Select and copy the value above.')}})],null)}function createSpace(){if(state.user.role!=='admin'){notice('Ask an administrator to create a storage space.');return}const name=field('name','Space name','text','','Used in CSO/<space name>/<file path>.');name.querySelector('input').required=true;const quota=field('quota','Maximum storage (GB)','number','15');Object.assign(quota.querySelector('input'),{min:'0.001',step:'0.001',required:true});const bw=field('bandwidth','Monthly transfer limit (GB)','number','0','Counts uploads and downloads. Enter 0 for unlimited.');Object.assign(bw.querySelector('input'),{min:'0',step:'0.001'});const uploadLimit=uploadLimitField(),compression=spaceCompressionField('inherit');modal('Create storage space',[name,quota,bw,uploadLimit,compression,field('encrypted','Encrypt files before storing','checkbox',false,'Encryption is fixed when the space is created.')],async(data)=>{const r=await api('/api/admin/spaces',{method:'POST',body:JSON.stringify({name:data.get('name'),quota:Math.round(Number(data.get('quota'))*GB),encrypted:data.has('encrypted'),bandwidth_limit:Math.round(Number(data.get('bandwidth'))*GB),max_upload_mib:Number(data.get('max_upload_mib')),compression_mode:data.get('compression_mode')})});closeModal();await refresh();secret('Your new API key',r.key,'Save this key now. It is shown only once and grants access to this storage space.')},'Create space')}
function readCredentials(form){const creds={};['access_token','refresh_token','client_id','client_secret','email','password','key_id','application_key','bucket_id','bucket_name'].forEach(k=>{const value=form.elements.namedItem(k)?.value;if(value)creds[k]=value});return creds}
function credentialJsonPicker(onImport){
 const label=field('client_json','Google OAuth client JSON (.json or .txt)','file','','Choose your downloaded file, such as gd_api.txt. File contents stay in this form until you continue.');
 const input=label.querySelector('input');let sequence=0;input.accept='.json,.txt,application/json,text/plain';
 const feedback=el('div','form-hint');feedback.setAttribute('role','status');label.append(feedback);
 input.onchange=async()=>{
  const current=++sequence;onImport(null);feedback.textContent='';const file=input.files[0];if(!file)return;
  try{if(file.size>65536)throw Error('Choose an OAuth client JSON file smaller than 64 KB.');const config=JSON.parse(await file.text());if(current!==sequence||!input.isConnected)return;const client=config?.installed||config?.web;if(!client||typeof client.client_id!=='string'||typeof client.client_secret!=='string'||!client.client_id||!client.client_secret)throw Error('This file needs an installed or web OAuth client with client_id and client_secret.');onImport(config);feedback.textContent='OAuth client loaded. Credentials will not be displayed.';feedback.className='form-hint test-success'}catch(error){if(current!==sequence||!input.isConnected)return;feedback.textContent=error instanceof SyntaxError?'This file does not contain valid JSON.':error.message;feedback.className='form-hint test-error'}
 };return label;
}
function testControl(run){
 const root=el('div','test-control'),result=el('div','test-result');result.setAttribute('role','status');result.setAttribute('aria-live','polite');result.hidden=true;let revision=0,pending=false;
 const button=btn('Test drive',async()=>{const own=++revision;pending=true;button.disabled=true;button.textContent='Testing…';result.hidden=false;result.className='test-result';result.textContent='Checking access and storage information…';try{const data=await run();if(own!==revision)return;if(!data.ok)throw Error(data.message||'The storage test did not succeed.');result.className='test-result test-success';const parts=[data.message||'Storage is reachable.'];if(data.account)parts.push('Account: '+data.account);if(Number.isFinite(data.capacity))parts.push('Total: '+bytes(data.capacity));if(Number.isFinite(data.usage))parts.push('Used: '+bytes(data.usage));if(Number.isFinite(data.available))parts.push('Available: '+bytes(data.available));parts.push('Read-only check; file uploads have not been tested.');result.textContent=parts.join(' · ')}catch(error){if(own===revision){result.className='test-result test-error';result.textContent=error.message}}finally{pending=false;button.disabled=false;button.textContent='Test drive'}});
 append(root,button,el('p','form-hint','Checks account access and quota without writing a file.'),result);
 return {root,invalidate(){revision++;result.hidden=true;result.textContent='';if(!pending)button.disabled=false}};
}
function createCloud(){
 if(state.user.role!=='admin'){notice('Ask an administrator to connect a cloud account.');return}
 let clientConfig=null;
 const name=field('name','Account name','text','','A label for this connection.');name.querySelector('input').required=true;
 const provider=selectField('provider','Provider',[['local','Local storage'],['google','Google Drive'],['dropbox','Dropbox'],['mega','MEGA'],['b2','Backblaze B2']]);
 const capacity=field('capacity','Capacity to contribute (GB)','number','15','Set this to the capacity available to CSO in this account.');Object.assign(capacity.querySelector('input'),{min:'0.001',step:'0.001',required:true});
 const capacityMode=selectField('capacity_mode','Storage capacity',[['auto','Detect available storage automatically'],['manual','Set a storage budget manually']]);
 function capacityFields(){const p=provider.querySelector('select').value;const auto=p!=='b2'&&capacityMode.querySelector('select').value==='auto';capacityMode.hidden=p==='b2';capacity.hidden=auto;capacity.querySelector('input').required=!auto;}
 capacityMode.querySelector('select').onchange=capacityFields;
 const credentials=el('div');let activeTest=null;
 const googleMode=selectField('google_mode','Connect with',[['json','Google sign-in (JSON)'],['token','Access token']]);
 function tokenFields(p){
  if(p==='b2'){
   credentials.append(field('key_id','Application key ID'),field('application_key','Application key','password'),field('bucket_id','Bucket ID'),field('bucket_name','Bucket name'),el('p','form-hint','Use a key restricted to this bucket, with list, read, write and delete permissions and no filename prefix restriction. Keys are encrypted at rest. Capacity is your CSO storage budget.'));
   credentials.querySelectorAll('input').forEach(n=>n.required=true);return;
  }
  if(p==='mega'){
   credentials.append(field('email','MEGA email','email'),field('password','MEGA password','password'),el('p','form-hint','Requires rclone on the CSO server. Credentials are encrypted at rest. Accounts requiring 2FA are not supported. Sign in once at mega.nz before connecting.'));
   credentials.querySelectorAll('input').forEach(n=>n.required=true);return;
  }
  credentials.append(field('access_token',p==='google'?'Google access token (not OAuth client ID or client secret)':'Access token','password'));credentials.querySelector('[name=access_token]').required=true;
  if(p==='google'){
   credentials.append(field('refresh_token','Refresh token (optional)','password'),field('client_id','OAuth client ID (for token refresh)'),field('client_secret','OAuth client secret (for token refresh)','password'));
   credentials.append(credentialJsonPicker(config=>{clientConfig=config;activeTest?.invalidate();if(config){const client=config.installed||config.web;credentials.querySelector('[name=client_id]').value=client.client_id;credentials.querySelector('[name=client_secret]').value=client.client_secret}}));
  }
  credentials.append(el('p','form-hint','Credentials are sent to your CSO server and encrypted at rest. Obtain tokens with file read/write permissions from your provider.'));
 }
 function providerFields(){
  capacityFields();
  activeTest?.invalidate();activeTest=null;clientConfig=null;credentials.replaceChildren();const p=provider.querySelector('select').value,isJson=p==='google'&&googleMode.querySelector('select').value==='json';
  if(p==='google')credentials.append(googleMode);
  if(isJson){
   const picker=credentialJsonPicker(config=>{clientConfig=config});picker.querySelector('input').required=true;credentials.append(picker);
   credentials.append(el('div','alert info','Continue with Google to choose an account and authorize Drive access. The account is saved only after Google returns you to CSO.'));
   credentials.append(el('p','form-hint','In Google Cloud Console, enable the Google Drive API and add your Google account as a test user if the OAuth app is in testing. Desktop client JSON works only with local CSO and a browser on the same machine. For a web client, register the CSO callback URI shown below.'));
   const redirect=el('code','oauth-redirect',location.origin+'/auth/google/callback');credentials.append(redirect);
   credentials.append(append(el('div','test-control'),btn('Authorize & test drive',()=>{$('modal-form').requestSubmit()}),el('p','form-hint','Google authorization connects the account and runs a read-only test. You can test it again after connecting. The account is saved when Google authorization succeeds.')));
  }else{
   if(p!=='local')tokenFields(p);else credentials.append(el('p','form-hint','Files are stored on this server. This is useful for development and private storage.'));
   activeTest=testControl(async()=>{const form=$('modal-form'),creds=readCredentials(form);if(p!=='local'&&p!=='mega'&&p!=='b2'&&!creds.access_token)throw Error('Enter an access token before testing.');return api('/api/admin/clouds/test',{method:'POST',body:JSON.stringify({provider:p,credentials:creds})})});credentials.append(activeTest.root);
  }
  $('modal-submit').textContent=isJson?'Continue with Google':'Connect account';
 }
 provider.querySelector('select').onchange=providerFields;googleMode.querySelector('select').onchange=providerFields;
 modal('Connect a cloud account',[name,provider,capacityMode,capacity,credentials],async(data,form)=>{
  const p=data.get('provider'),payload={name:data.get('name'),auto_capacity:data.get('provider')!=='b2'&&data.get('capacity_mode')==='auto'};if(!payload.auto_capacity)payload.capacity=Math.round(Number(data.get('capacity'))*GB);
  if(p==='google'&&data.get('google_mode')==='json'){
   if(!clientConfig)throw Error('Choose a valid Google OAuth client JSON file first.');
   const response=await api('/api/admin/google/connect',{method:'POST',body:JSON.stringify({...payload,client_config:clientConfig})});
   if(typeof response.authorization_url!=='string')throw Error('The server did not return a Google authorization URL.');location.assign(response.authorization_url);return;
  }
  await api('/api/admin/clouds',{method:'POST',body:JSON.stringify({...payload,provider:p,credentials:readCredentials(form)})});closeModal();await refresh();notice('Cloud account configured');
 },'Connect account');
 $('modal-body').oninput=()=>activeTest?.invalidate();providerFields();
}

function compressionOptions(inherit=false){
 const options=[['off','Off'],['zip','ZIP — maximum'],['bz2','Bzip2 — maximum'],['xz','LZMA/XZ — extreme'],['auto','Automatic — smallest result']];
 return inherit?[['inherit','Use workspace default'],...options]:options;
}
function spaceCompressionField(value){
 const control=selectField('compression_mode','Upload compression',compressionOptions(true));control.querySelector('select').value=value;
 control.append(el('small','','Applies to new files. Automatic tries each format and keeps the smallest result. XZ and automatic can use substantial memory and CPU.'));
 return control;
}
function uploadLimitField(value){
 const ceiling=Number(state.server_upload_limit_mib)||512;
 const limit=field('max_upload_mib','Maximum single file size (MiB)','number',String(value??ceiling),'Up to '+ceiling+' MiB per file on this server. A 317 MB file fits within a 512 MiB limit.');
 Object.assign(limit.querySelector('input'),{min:'1',max:String(ceiling),step:'1',required:true});
 return limit;
}
function upload(){
 if(!state.spaces.length){createSpace();return;}
 const file=field('file','Choose a file','file');file.querySelector('input').required=true;
 const space=selectField('space_id','Storage space',state.spaces.map(s=>[s.id,s.name]));
 const limitHint=el('p','form-hint');
 function updateLimitHint(){const selected=spaceBy(space.querySelector('select').value);limitHint.textContent='Maximum file size: '+(selected?.max_upload_mib||state.server_upload_limit_mib||512)+' MiB.';}
 space.querySelector('select').onchange=updateLimitHint;updateLimitHint();
 const hint=el('p','form-hint',state.user.role==='admin'?'Keep this tab open until the file reaches CSO. Cloud transfer continues in Operations.':'Keep this tab open until the file reaches CSO. Cloud transfer then continues in the background.');
 modal('Upload a file',[space,file,limitHint,field('path','File path (optional)','text','','For example: documents/report.pdf. Defaults to the original filename.'),hint],data=>{
  const selected=spaceBy(data.get('space_id')),selectedFile=data.get('file');
  if(!selected||!(selectedFile instanceof File)||!selectedFile.name)throw Error('Choose a storage space and a file.');
  const limit=Number(selected.max_upload_mib)||Number(state.server_upload_limit_mib)||512;
  if(selectedFile.size>limit*1024**2)throw Error('This file exceeds the '+limit+' MiB limit for this space. Ask an administrator to increase its maximum file size.');
  const path=String(data.get('path')||selectedFile.name),spaceId=Number(selected.id),administrator=state.user.role==='admin';
  const transfer=new FormData();transfer.set('space_id',String(spaceId));transfer.set('file',selectedFile);transfer.set('path',path);transfer.set('background','true');
  closeModal();
  if(administrator){operationsFilters.status='';operationsFilters.q='';operationsFilters.action='';operationsOffset=0;operationsDirty=true;navigate('operations');}
  else if(page!=='files')navigate('files');
  notice('Sending file to CSO. Keep this tab open until it is received.');
  void sendUpload(transfer,{spaceId,path,size:selectedFile.size,name:selectedFile.name,administrator});
 },'Start upload');
}
let transferSequence=0;
const browserTransfers=new Map();
function updateTransferFeedback(id,message,failed=false){
 browserTransfers.set(id,{message,failed});
 let root=$('browser-transfer-feedback');
 if(!root){root=el('aside','browser-transfer-feedback');root.id='browser-transfer-feedback';root.setAttribute('aria-label','File uploads');root.setAttribute('aria-live','polite');document.body.append(root);}
 let row=root.querySelector('[data-transfer="'+id+'"]');
 if(!row){row=el('div','browser-transfer-row');row.dataset.transfer=String(id);root.append(row);}
 row.classList.toggle('transfer-failed',failed);row.textContent=message;if(failed)row.append(btn('Dismiss',()=>clearTransferFeedback(id),'text-button'));
}
function clearTransferFeedback(id){
 browserTransfers.delete(id);const root=$('browser-transfer-feedback');if(!root)return;
 root.querySelector('[data-transfer="'+id+'"]')?.remove();if(!root.children.length)root.remove();
}
async function sendUpload(transfer,details){
 const feedbackId=++transferSequence;let token=null;
 updateTransferFeedback(feedbackId,'Sending “'+details.name+'” to CSO. Keep this tab open.');
 try{
  const started=await api('/api/admin/uploads/start',{method:'POST',body:JSON.stringify({space_id:details.spaceId,path:details.path,size:details.size})});
  if(typeof started.upload_token!=='string'||!started.upload_token)throw Error('Upload could not be started.');
  token=started.upload_token;
  const queued=await api('/api/admin/files',{method:'POST',headers:{'X-CSO-Upload-Token':token},body:transfer});
  if(!queued.operation_id||queued.status!=='queued')throw Error('Upload confirmation was not received.');
  updateTransferFeedback(feedbackId,'“'+details.name+'” reached CSO. '+(details.administrator?'Follow cloud transfer in Operations.':'Cloud transfer is queued in the background.'));
  notice('File received by CSO. Cloud transfer is queued.');
  setTimeout(()=>clearTransferFeedback(feedbackId),10000);
 }catch(error){
  const message='Could not confirm upload of “'+details.name+'”. Check the space limits and your connection. '+(details.administrator?'Check Operations before retrying to avoid a duplicate.':'Ask an administrator to check its status before retrying.');
  updateTransferFeedback(feedbackId,message,true);notice('Upload could not be confirmed. See the upload message for next steps.');
  if(token){try{await api('/api/admin/uploads/failed',{method:'POST',body:JSON.stringify({upload_token:token})});}catch{/* The server may already have queued the cloud transfer. */}}
 }
}

function rotate(s){modal('Rotate API key',[el('p','form-hint','The current key for “'+s.name+'” will stop working immediately. Update applications with the new key.')],async()=>{const r=await api('/api/admin/spaces/'+encodeURIComponent(s.id)+'/rotate',{method:'POST',body:'{}'});closeModal();secret('Your replacement API key',r.key,'Save this key now. The previous key has been revoked.')},'Rotate key')}
function invite(){modal('Invite a team member',[selectField('role','Workspace role',[['manager','Manager — manage files'],['admin','Administrator — also manage team']]),el('p','form-hint','Share the generated invitation securely. The recipient links their existing OuthMan account on the login page.')],async data=>{const r=await api('/api/admin/invitations',{method:'POST',body:JSON.stringify({role:data.get('role')})});closeModal();secret('Team invitation',r.token,'Share this token with the new member. They should choose “Link account” on the login page.')},'Create invitation')}
function deleteFile(f){modal('Delete file',[el('p','form-hint','Permanently delete “'+f.path+'” from its cloud storage? This action cannot be undone.')],async()=>{await api('/api/admin/files/'+encodeURIComponent(f.id),{method:'DELETE'});closeModal();await refresh();notice('File deleted')},'Delete file')}
function apiGuide(){const root=el('div'),intro=panel('Authentication','Use the storage space API key in each request.');const content=el('div','api-intro');append(content,el('p','','Create a storage space and save its generated key. Files stay inside that space, with the cloud chosen automatically on the first upload. Limits and encryption are enforced on the server.'),el('div','alert info','Keep API keys on your application server. Rotate a key from Storage spaces if it is exposed.'));intro.append(content);root.append(intro);const endpoints=el('section','panel');const base=location.origin;const samples=[['POST','/api/v1/files','Upload a file','Upload multipart form data. Supply a relative path to preserve folders.',`curl '${base}/api/v1/files' \\\n  -H 'Authorization: Bearer YOUR_SPACE_KEY' \\\n  -F 'file=@report.pdf' \\\n  -F 'path=documents/report.pdf'`],['GET','/api/v1/files','List your files','Returns file metadata for the storage space identified by the key.',`curl '${base}/api/v1/files' \\\n  -H 'Authorization: Bearer YOUR_SPACE_KEY'`],['GET','/api/v1/files/FILE_ID','Download a file','Use the file ID returned by upload or listing. Encrypted files are decrypted before delivery.',`curl '${base}/api/v1/files/FILE_ID' \\\n  -H 'Authorization: Bearer YOUR_SPACE_KEY' \\\n  --output report.pdf`],['DELETE','/api/v1/files/FILE_ID','Delete a file','Permanently removes the file and releases its storage usage.',`curl -X DELETE '${base}/api/v1/files/FILE_ID' \\\n  -H 'Authorization: Bearer YOUR_SPACE_KEY'`]];samples.forEach(([method,path,title,desc,sample])=>{const item=el('div','endpoint');append(item,append(el('h3'),el('span','method',method),el('code','',path)),el('h3','',title),el('p','',desc),el('pre','',sample));endpoints.append(item)});root.append(endpoints);return root}
document.querySelectorAll('#nav button').forEach(n=>n.onclick=()=>navigate(n.dataset.page));$('close-modal').onclick=closeModal;$('cancel-modal').onclick=closeModal;$('logout').onclick=async()=>{try{await api('/auth/logout',{method:'POST',body:'{}'});location.href='/'}catch(e){notice(e.message)}};refresh();

function changeRole(u){const roles=selectField('role','Workspace role',[['manager','Manager'],['admin','Administrator']]);roles.querySelector('select').value=u.role==='revoked'?'manager':u.role;modal((u.role==='revoked'?'Restore access for ':'Change role for ')+u.username,[roles],async data=>{await api('/api/admin/users/'+encodeURIComponent(u.id),{method:'PATCH',body:JSON.stringify({role:data.get('role')})});closeModal();await refresh();notice('Role updated')},'Update role')}
function revokeUser(u){modal('Revoke workspace access',[el('p','form-hint','Remove '+u.username+' from this workspace? Their active sessions will lose access.')],async()=>{await api('/api/admin/users/'+encodeURIComponent(u.id),{method:'DELETE'});closeModal();await refresh();notice('Workspace access revoked')},'Revoke access')}

document.getElementById('mobile-logout').onclick=()=>document.getElementById('logout').click();

function editLimits(s){const quota=field('quota','Maximum storage (GB)','number',String(s.quota/GB));Object.assign(quota.querySelector('input'),{min:'0.000000001',step:'any',required:true});const bandwidth=field('bandwidth','Monthly transfer limit (GB)','number',String(s.bandwidth_limit/GB),'Enter 0 for unlimited. Counts uploads and downloads.');Object.assign(bandwidth.querySelector('input'),{min:'0',step:'any',required:true});const uploadLimit=uploadLimitField(s.max_upload_mib),compression=spaceCompressionField(s.compression_mode||'inherit');modal('Edit limits for '+s.name,[quota,bandwidth,uploadLimit,compression,el('p','form-hint','Existing files, encryption, and the assigned cloud remain unchanged.')],async data=>{await api('/api/admin/spaces/'+encodeURIComponent(s.id),{method:'PATCH',body:JSON.stringify({quota:Math.round(Number(data.get('quota'))*GB),bandwidth_limit:Math.round(Number(data.get('bandwidth'))*GB),max_upload_mib:Number(data.get('max_upload_mib')),compression_mode:data.get('compression_mode')})});closeModal();await refresh();notice('Storage limits updated')},'Update limits')}

function editCloud(c){
 const capacity=field('capacity','Capacity to contribute (GB)','number',String(c.capacity/GB));
 Object.assign(capacity.querySelector('input'),{min:'0.000000001',step:'any',required:true});
 const fields=[el('p','form-hint',c.name+' · '+(c.provider==='google'?'Google Drive':c.provider==='dropbox'?'Dropbox':c.provider==='mega'?'MEGA':c.provider==='b2'?'Backblaze B2':'Local storage')),capacity];
 if(c.provider!=='local'){
  fields.push(el('div','alert info','Renew credentials for this same cloud account only. Do not substitute a different account: existing files remain assigned here.'));
  if(c.provider==='b2')fields.push(field('key_id','Replacement application key ID'),field('application_key','Replacement application key','password'),field('bucket_id','Bucket ID'),field('bucket_name','Bucket name'));
  else if(c.provider==='mega')fields.push(field('email','Replacement MEGA email (optional)','email'),field('password','Replacement MEGA password (optional)','password'));
  else fields.push(field('access_token',c.provider==='google'?'Replacement Google access token (optional)':'Replacement access token (optional)','password','',c.provider==='google'?'Use a Google access token, not the OAuth client ID or client secret. Leave blank to preserve existing credentials.':'Leave blank to preserve all existing credentials.'));
  if(c.provider==='google'){
   fields.push(btn('Reauthorize with Google',()=>{closeModal();reauthorizeCloud(c)}));
   fields.push(field('refresh_token','Replacement refresh token (optional)','password'),field('client_id','OAuth client ID (for token refresh)'),field('client_secret','OAuth client secret (for token refresh)','password'));
   fields.push(credentialJsonPicker(config=>{test.invalidate();if(config){const client=config.installed||config.web;$('modal-form').elements.client_id.value=client.client_id;$('modal-form').elements.client_secret.value=client.client_secret}}));
  }
  fields.push(el('p','form-hint','If you enter replacement credentials, the full credentials record is replaced. For MEGA, supply both email and password. For Backblaze, supply all four connection fields. Include refresh credentials if automatic token renewal is needed.'));
 }
 const test=testControl(async()=>{const credentials=readCredentials($('modal-form'));return (Object.keys(credentials).length)?api('/api/admin/clouds/test',{method:'POST',body:JSON.stringify({provider:c.provider,credentials})}):api('/api/admin/clouds/'+encodeURIComponent(c.id)+'/test',{method:'POST',body:'{}'})});
 fields.push(test.root,el('p','form-hint','With blank replacement credentials, Test drive checks the saved account. Otherwise it checks the entered replacement credentials.'));
 modal('Edit cloud account',fields,async(data,form)=>{
  const payload={capacity:Math.round(Number(data.get('capacity'))*GB)},credentials=readCredentials(form);
  if(Object.keys(credentials).length)payload.credentials=credentials;
  await api('/api/admin/clouds/'+encodeURIComponent(c.id),{method:'PATCH',body:JSON.stringify(payload)});
  closeModal();await refresh();notice('Cloud account updated');
 },'Update account');
 $('modal-body').oninput=()=>test.invalidate();
}
function testSavedCloud(c){
 const test=testControl(()=>api('/api/admin/clouds/'+encodeURIComponent(c.id)+'/test',{method:'POST',body:'{}'}));
 modal('Test drive: '+c.name,[el('p','form-hint','Check the saved account credentials and available storage. This does not upload, modify, or delete any files.'),test.root],null);
 test.root.querySelector('button').click();
}

const cloudRedirect=new URLSearchParams(location.search);if(cloudRedirect.has('cloud_connected')){page='clouds';notice('Google Drive account connected. You can now test it from Cloud accounts.');history.replaceState(null,'',location.pathname+location.hash)}else if(cloudRedirect.has('cloud_error')){$('error').textContent=cloudRedirect.get('cloud_error')||'Google authorization could not be completed. Please try again.';$('error').hidden=false;history.replaceState(null,'',location.pathname+location.hash)}

function reauthorizeCloud(c){
 let clientConfig=null;
 const picker=credentialJsonPicker(config=>{clientConfig=config});picker.querySelector('input').required=true;
 modal('Reauthorize '+c.name,[el('div','alert info','Select the SAME Google account originally used for this cloud. Existing files and storage spaces stay assigned to this account.'),picker,el('p','form-hint','Choose your Google OAuth client JSON file, such as gd_api.txt. Google sign-in replaces the saved credentials and runs a read-only access check. The account name and capacity stay unchanged.'),el('p','form-hint','Enable the Google Drive API and add your account as a test user in Google Cloud Console if needed. Desktop JSON requires local CSO and a browser on the same machine.')],async()=>{
  if(!clientConfig)throw Error('Choose a valid Google OAuth client JSON file before continuing.');
  const response=await api('/api/admin/google/connect',{method:'POST',body:JSON.stringify({cloud_id:c.id,client_config:clientConfig})});
  if(typeof response.authorization_url!=='string')throw Error('The server did not return a Google authorization URL.');
  location.assign(response.authorization_url);
 },'Continue with Google');
}

function deleteCloud(c){
 const assigned=state.spaces.filter(s=>String(s.cloud_id)===String(c.id));
 const hasFiles=state.files.some(f=>assigned.some(s=>String(s.id)===String(f.space_id)));
 if(c.used||hasFiles||assigned.some(s=>s.used)){
  modal('Cloud account has files',[el('p','form-hint','Delete the files stored in “'+c.name+'” before removing this cloud account. Your storage spaces and API keys will be kept.')],null);return;
 }
 modal('Delete cloud account',[el('p','form-hint','Remove “'+c.name+'” and its saved credentials from CSO? Empty storage spaces will choose another cloud on their next upload. This does not delete the bucket or account at your storage provider.')],async()=>{
  await api('/api/admin/clouds/'+encodeURIComponent(c.id),{method:'DELETE'});closeModal();await refresh();notice('Cloud account deleted');
 },'Delete account');
 $('modal-submit').classList.add('danger');
}

function deleteSpace(s){
 const files=state.files.filter(f=>String(f.space_id)===String(s.id)),hasFiles=Boolean(s.used||files.length);
 const fields=[el('p','form-hint','Delete “'+s.name+'” and revoke its API key permanently? The connected cloud account will be kept.')];
 if(hasFiles){
  fields.push(el('div','alert error',files.length+' files will be deleted from cloud storage. This cannot be undone. If a provider deletion fails, the space and remaining files are kept.'));
  const all=field('delete_files','Delete all files in this space','checkbox',false);all.querySelector('input').required=true;
  const confirm=field('confirm_name','Type the space name to confirm','text','','Enter '+s.name+' exactly.');confirm.querySelector('input').required=true;
  fields.push(all,confirm);
 }
 modal('Delete storage space',fields,async(data)=>{
  if(hasFiles&&(!data.has('delete_files')||data.get('confirm_name')!==s.name))throw Error('Select Delete all files and enter the exact space name.');
  const result=await api('/api/admin/spaces/'+encodeURIComponent(s.id),{method:'DELETE',body:JSON.stringify({delete_files:hasFiles&&data.has('delete_files'),confirm_name:data.get('confirm_name'),background:true})});
  closeModal();operationsDirty=true;operationsOffset=0;operationsFilters.status='';operationsFilters.q='';operationsFilters.action='';navigate('operations');notice('Deletion queued. Follow its progress in Operations.');
 },hasFiles?'Delete all files and space':'Delete space');$('modal-submit').classList.add('danger');
}
function refreshCapacity(c){
 modal('Refresh cloud capacity',[el('p','form-hint','Read the available storage for “'+c.name+'” from the provider and update its CSO capacity? This replaces any manually configured storage budget.')],async()=>{
  await api('/api/admin/clouds/'+encodeURIComponent(c.id)+'/capacity',{method:'POST',body:'{}'});closeModal();await refresh();notice('Cloud capacity updated');
 },'Refresh capacity');
}


function stopOperationsPolling(){
 clearTimeout(operationsTimer);clearTimeout(operationsSearchTimer);operationsTimer=null;operationsSearchTimer=null;operationsGeneration++;
 if(operationsController){operationsController.abort();operationsController=null;}
}
function refreshAfterOperations(){
 if(operationsStateRefresh)return operationsStateRefresh;
 operationsStateRefresh=(async()=>{try{state=await api('/api/admin/state');if(page!=='operations'&&page!=='settings')render();}catch(error){notice(error.message)}})().finally(()=>{operationsStateRefresh=null});
 return operationsStateRefresh;
}
function operationsPage(){
 const root=panel('Operation history','Tasks and request history.');
 const error=el('div','alert error operations-error');error.hidden=true;error.setAttribute('role','alert');
 const status=el('p','form-hint operations-status','Loading operation history…');status.setAttribute('role','status');
 const statusFilter=selectField('operation_status','Status',[['','All statuses'],['queued','Queued'],['running','Running'],['paused','Paused'],['completed','Completed'],['failed','Failed'],['interrupted','Interrupted']]);
 const searchFilter=field('operation_search','Search history','search',operationsFilters.q);searchFilter.querySelector('input').placeholder='Target, action, actor or message';
 const actionOptions=[['','All actions'],['admin_upload','Upload file from console'],['api_files','File API request'],['download_file','Download file'],['admin_delete','Delete file from console'],['delete_file','Delete file through API'],['delete_space','Delete storage space'],['add_space','Create storage space'],['update_space','Update storage space'],['add_cloud','Connect cloud account'],['update_cloud','Edit cloud account'],['delete_cloud','Delete cloud account'],['refresh_cloud_capacity','Refresh cloud capacity'],['test_saved_cloud','Test saved cloud'],['test_cloud_credentials','Test cloud credentials'],['rotate','Rotate API key'],['invite','Invite team member'],['manage_user','Manage team member'],['save_settings','Save settings'],['connect','Start Google connection'],['complete','Complete Google connection'],['pause_operation','Pause operation'],['resume_operation','Resume operation']];
 const actionFilter=selectField('operation_action','Action',actionOptions);actionFilter.querySelector('select').value=operationsFilters.action;
 statusFilter.querySelector('select').value=operationsFilters.status;
 const filters=append(el('div','operations-filters'),statusFilter,searchFilter,actionFilter);
 function filterChanged(delay){
  operationsFilters.status=statusFilter.querySelector('select').value;
  operationsFilters.q=searchFilter.querySelector('input').value;
  operationsFilters.action=actionFilter.querySelector('select').value;
  operationsOffset=0;stopOperationsPolling();
  operationsSearchTimer=setTimeout(()=>{operationsSearchTimer=null;loadOperations();},delay);
 }
 statusFilter.querySelector('select').onchange=()=>filterChanged(0);
 searchFilter.querySelector('input').oninput=()=>filterChanged(250);
 actionFilter.querySelector('select').onchange=()=>filterChanged(0);
 const table=el('table','operations-table'),head=el('thead'),heading=el('tr'),list=el('tbody');
 ['Time','Action','Target','Status','Progress','Actor'].forEach(text=>{const th=el('th','',text);th.scope='col';heading.append(th);});head.append(heading);append(table,head,list);
 const tableWrap=append(el('div','table-wrap'),table);
 const previous=btn('← Previous',()=>changeOperationsPage(-1)),next=btn('Next →',()=>changeOperationsPage(1));
 previous.disabled=true;next.disabled=true;
 const range=el('span','muted','');
 const pager=append(el('div','operations-pagination'),previous,range,next);
 append(root,filters,error,status,tableWrap,pager);
 operationsView={root,error,status,list,previous,next,range,rows:new Map(),hasMore:false};
 operationsDirty=true;
 return root;
}
function operationLabel(action){return String(action||'Operation').replace(/_/g,' ').replace(/^./,letter=>letter.toUpperCase());}
function changeOperationsPage(direction){
 if(page!=='operations'||operationsController||!operationsView)return;
 if(direction>0&&!operationsView.hasMore)return;
 operationsOffset=Math.max(0,operationsOffset+direction*operationsLimit);
 clearTimeout(operationsTimer);operationsTimer=null;loadOperations();
}
function operationTime(value){
 const date=new Date(Number(value)*1000);
 return Number.isFinite(date.getTime())?date.toLocaleString():'Time unavailable';
}
function operationPercent(operation){
 if(operation.status==='completed')return 100;
 const total=operation.total===null||operation.total===undefined?null:Number(operation.total);
 if(!Number.isFinite(total)||total<=0)return null;
 const completed=Number(operation.completed),value=Number.isFinite(completed)?Math.max(0,completed):0;
 const ceiling=['running','queued','paused'].includes(operation.status)?99:100;
 return Math.min(ceiling,Math.max(0,Math.round(value/total*100)));
}
function setOperationProgress(progress,label,operation){
 const percent=operationPercent(operation),active=operation.status==='queued'||operation.status==='running';
 progress.max=100;progress.removeAttribute('value');
 if(percent!==null)progress.value=percent;else if(!active)progress.value=0;
 label.textContent=percent!==null?percent+'%':operation.status==='paused'?'Paused':active?'In progress':'Progress unavailable';
 progress.setAttribute('aria-label',operationLabel(operation.action)+' progress');
}
function operationRow(operation,existing){
 const row=existing||el('tr','operation-row');
 const status=String(operation.status||'queued'),active=status==='queued'||status==='running';
 const statusLabel={queued:'Queued',running:'Running',paused:'Paused',completed:'Completed',failed:'Failed',interrupted:'Interrupted'}[status]||status;
 const tone=status==='completed'?'green':status==='failed'||status==='interrupted'||status==='paused'?'amber':active?'blue':'gray';
 if(!existing){
  for(let i=0;i<6;i++)row.append(el('td'));
  row.children[2].append(append(el('div','operation-target'),el('span','operation-target-name'),btn('Details',()=>{},'text-button')));
  row.children[3].append(el('span','operation-status'),el('div','operation-controls'));
  row.children[4].append(append(el('div','operation-progress-block'),append(el('div','operation-progress-cell'),el('progress','operation-progress'),el('span','operation-count')),btn('View activity',()=>{},'operation-current-step')));
 }
 row.children[0].textContent=operationTime(operation.created);
 row.children[1].textContent=operationLabel(operation.action);row.children[1].title=String(operation.action||'');
 const target=row.children[2].querySelector('.operation-target-name');target.textContent=String(operation.resource||'Workspace');target.title=target.textContent;
 const details=row.children[2].querySelector('button');details.setAttribute('aria-label','Details for '+operationLabel(operation.action)+' on '+target.textContent);details.onclick=()=>openOperationDetails(operation);
 row.children[3].querySelector('.operation-status').replaceChildren(badge(statusLabel,tone));updateOperationControls(row.children[3].querySelector('.operation-controls'),operation);
 setOperationProgress(row.children[4].querySelector('progress'),row.children[4].querySelector('.operation-count'),operation);
 const step=row.children[4].querySelector('.operation-current-step');step.textContent=String(operation.message||'View operation activity');step.title=String(operation.message||'View operation activity');step.setAttribute('aria-label','View activity: '+step.textContent);step.onclick=()=>openOperationDetails(operation);step.hidden=['completed','failed','interrupted'].includes(status);
 row.children[5].textContent=String(operation.actor||'Unknown actor');row.dataset.operationId=String(operation.id);
 return row;
}
function updateOperationControls(container,operation){
 const terminal=['completed','failed','interrupted'].includes(operation.status);
 if(!operation.can_pause||terminal){container.hidden=true;return;}
 container.hidden=false;
 let button=container.querySelector('button');if(!button){button=btn('Pause',()=>{},'text-button operation-control');container.append(button);}
 const resume=operation.status==='paused'||operation.status==='queued'&&Boolean(operation.pause_requested);
 const pausing=operation.status==='running'&&Boolean(operation.pause_requested),pending=operationControlRequests.has(String(operation.id));
 button.textContent=pending?'Updating…':pausing?'Pausing…':resume?'Resume':'Pause';button.disabled=pausing||pending;
 button.title=resume?'Resume this operation':'Pause at the next safe boundary. An in-flight request or compression chunk must finish first.';
 button.onclick=()=>controlOperation(operation,resume?'resume':'pause');
}
async function controlOperation(operation,action){
 const id=String(operation.id);if(operationControlRequests.has(id))return;operationControlRequests.add(id);
 const row=operationsView?.rows.get(id);if(row)updateOperationControls(row.querySelector('.operation-controls'),operation);
 const details=operationDetailsView;if(details&&String(details.id)===id)updateOperationControls(details.controls,operation);
 try{
  await api('/api/admin/operations/'+encodeURIComponent(id)+'/'+action,{method:'POST',body:'{}'});
  notice(action==='pause'?'Pause requested. Work stops at the next safe boundary.':'Resume requested. Work will continue when a slot is available.');
 }catch(error){notice('Could not change this operation. Check its current status and try again.');}
 finally{
  operationControlRequests.delete(id);
  if(row?.isConnected)updateOperationControls(row.querySelector('.operation-controls'),operation);
  if(operationDetailsView===details&&details&&String(details.id)===id)updateOperationControls(details.controls,operation);
  if(page==='operations'&&!operationsController){clearTimeout(operationsTimer);loadOperations();}
  if(operationDetailsView&&!operationDetailsController){clearTimeout(operationDetailsTimer);loadOperationDetails();}
 }
}
function stopOperationDetails(){
 clearTimeout(operationDetailsTimer);operationDetailsTimer=null;operationDetailsGeneration++;
 if(operationDetailsController){operationDetailsController.abort();operationDetailsController=null;}
 operationDetailsView=null;$('modal')?.classList.remove('operation-details-modal');
}
function openOperationDetails(operation){
 if(state?.user.role!=='admin')return;
 const root=el('div','operation-details');
 const summary=append(el('div','operation-detail-summary'),el('progress','operation-detail-progress'),el('strong','operation-detail-percent'));
 const controls=el('div','operation-detail-controls');
 const workflowHint=el('p','form-hint','Overall workflow progress; stages take different amounts of time. Pause takes effect at a safe boundary after an in-flight request or compression chunk finishes.');
 const current=el('p','operation-detail-current');current.setAttribute('role','status');
 const metadata=el('dl','operation-detail-metadata');
 const fields={};
 [['id','Operation'],['action','Action'],['resource','Target'],['actor','Actor'],['status','Status'],['created','Started'],['updated','Updated'],['items','Items completed']].forEach(([key,label])=>{const term=el('dt','',label),value=el('dd');fields[key]=value;append(metadata,term,value);});
 const error=el('div','alert error');error.hidden=true;error.setAttribute('role','alert');
 const log=el('div','operation-activity-log');log.setAttribute('role','log');log.setAttribute('aria-label','Timestamped operation activity');log.setAttribute('aria-live','polite');log.tabIndex=0;
 const placeholder=el('p','form-hint','Loading activity…');log.append(placeholder);
 append(root,summary,current,controls,workflowHint,metadata,el('h3','operation-log-heading','Activity log'),error,log);
 modal('Operation #'+String(operation.id),[root],null);$('modal').classList.add('operation-details-modal');
 operationDetailsView={id:operation.id,root,progress:summary.querySelector('progress'),percent:summary.querySelector('strong'),current,controls,fields,error,log,placeholder,cursor:0,seen:new Set()};
 updateOperationDetails(operationDetailsView,operation);loadOperationDetails();
}
function updateOperationDetails(view,operation){
 setOperationProgress(view.progress,view.percent,operation);updateOperationControls(view.controls,operation);
 view.current.textContent=String(operation.message||'No current activity message.');
 for(const key of ['id','resource','actor'])view.fields[key].textContent=String(operation[key]??'—');
 view.fields.action.textContent=operationLabel(operation.action);view.fields.action.title=String(operation.action||'');
 view.fields.status.textContent=operationLabel(operation.status);
 view.fields.created.textContent=operationTime(operation.created);view.fields.updated.textContent=operationTime(operation.updated);
 view.fields.items.textContent=String(operation.completed??0)+(operation.total===null||operation.total===undefined?'':' of '+String(operation.total));
}
async function loadOperationDetails(){
 const view=operationDetailsView;if(!view||operationDetailsController||!$('modal').open||!view.root.isConnected)return;
 const generation=operationDetailsGeneration,controller=new AbortController();operationDetailsController=controller;let more=false;
 try{
  const data=await api('/api/admin/operations/'+encodeURIComponent(view.id)+'?after='+view.cursor+'&limit=200',{signal:controller.signal});
  if(controller.signal.aborted||generation!==operationDetailsGeneration||operationDetailsView!==view||!view.root.isConnected)return;
  if(!data.operation||!Array.isArray(data.events))throw Error('Invalid activity response.');
  const follow=view.log.scrollHeight-view.log.scrollTop-view.log.clientHeight<48,scrollTop=view.log.scrollTop,cursorBefore=view.cursor;
  updateOperationDetails(view,data.operation);view.error.hidden=true;
  data.events.forEach(event=>{
   const id=Number(event.id);if(!Number.isFinite(id)||id<=0||view.seen.has(id))return;
   view.seen.add(id);view.cursor=Math.max(view.cursor,id);view.placeholder.remove();
   const entry=el('div','operation-log-entry'),time=el('time','',operationTime(event.created)),message=el('span','',String(event.message||operationLabel(event.status)));
   const date=new Date(Number(event.created)*1000);if(Number.isFinite(date.getTime()))time.dateTime=date.toISOString();
   const percent=operationPercent(event);entry.title=operationLabel(event.status)+(percent===null?'':' · '+percent+'%');append(entry,time,message);view.log.append(entry);
  });
  if(!view.seen.size)view.placeholder.textContent='No detailed activity was recorded. The current operation status is shown above.';
  if(follow)view.log.scrollTop=view.log.scrollHeight;else view.log.scrollTop=scrollTop;
  more=Boolean(data.has_more)&&view.cursor>cursorBefore;
 }catch(error){
  if(controller.signal.aborted||generation!==operationDetailsGeneration)return;
  view.error.textContent='Could not update activity. Retrying automatically.';view.error.hidden=false;
 }finally{
  if(operationDetailsController===controller)operationDetailsController=null;
  if(generation===operationDetailsGeneration&&operationDetailsView===view&&$('modal').open&&view.root.isConnected)operationDetailsTimer=setTimeout(loadOperationDetails,more?50:1500);
 }
}
$('modal').addEventListener('close',()=>{if(!$('modal').open)stopOperationDetails();});
$('modal').addEventListener('cancel',stopOperationDetails);
async function loadOperations(){
 const view=operationsView;if(page!=='operations'||!view||operationsController)return;
 const generation=operationsGeneration,controller=new AbortController();operationsController=controller;
 view.previous.disabled=true;view.next.disabled=true;
 try{
  const query=new URLSearchParams({limit:String(operationsLimit),offset:String(operationsOffset)});
  Object.entries(operationsFilters).forEach(([key,value])=>{if(value)query.set(key,value);});
  const data=await api('/api/admin/operations?'+query,{signal:controller.signal});
  if(generation!==operationsGeneration||page!=='operations'||operationsView!==view)return;
  if(!Array.isArray(data.operations))throw Error('The server returned invalid operation history.');
  view.error.hidden=true;view.hasMore=Boolean(data.has_more);
  const ids=new Set(data.operations.map(operation=>String(operation.id)));
  for(const [id,row] of view.rows){if(!ids.has(id)){row.remove();view.rows.delete(id);}}
  data.operations.forEach(operation=>{const id=String(operation.id);const row=operationRow(operation,view.rows.get(id));view.rows.set(id,row);view.list.append(row);});
  const active=data.operations.filter(operation=>operation.status==='queued'||operation.status==='running'||operation.status==='paused').length;
  view.status.textContent=data.operations.length?(active?active+' active operation'+(active===1?'':'s')+' on this page.':'No active operations on this page.'):'No operations match the current filters.';
  view.range.textContent=data.operations.length?'Showing '+(operationsOffset+1)+'–'+(operationsOffset+data.operations.length):operationsOffset?'No more records':'No records';
 }catch(error){
  if(controller.signal.aborted||generation!==operationsGeneration)return;
  view.error.textContent='Could not update operation history. '+error.message+' Retrying automatically.';view.error.hidden=false;
 }finally{
  if(operationsController===controller)operationsController=null;
  if(generation===operationsGeneration&&page==='operations'&&operationsView===view){
   view.previous.disabled=operationsOffset===0;view.next.disabled=!view.hasMore;
   operationsTimer=setTimeout(loadOperations,1800);
  }
 }
}


function settingsPage(){
 const root=panel('Configuration','Changes take effect immediately.');
 const error=el('div','alert error');error.hidden=true;error.setAttribute('role','alert');
 const status=el('p','form-hint','Loading settings…');status.setAttribute('role','status');
 const form=el('form','settings-form');
 const url=field('base_url','OuthMan server URL','url'),id=field('app_id','Application ID','text','','Use the existing integration identity. Once users are linked, the server URL and application ID cannot be changed.');
 const secret=field('app_secret','Replacement application secret','password','','Leave blank to keep the saved secret. Enter the full 32-character secret to rotate it.');
 const secretInput=secret.querySelector('input');secretInput.autocomplete='new-password';
 Object.assign(url.querySelector('input'),{required:true});Object.assign(id.querySelector('input'),{required:true,pattern:'[a-fA-F0-9]{16}',maxLength:16});secretInput.pattern='[a-fA-F0-9]{32}';secretInput.maxLength=32;
 const configured=el('p','form-hint','');
 const mode=selectField('compression_mode','Default upload compression',compressionOptions());
 const compressionNote=el('p','form-hint','Used by spaces set to the workspace default. New uploads keep compressed data only when it is smaller. Automatic tests each format; XZ and automatic can use substantial memory and CPU. Downloads restore the original file. Existing files are unchanged.');
 const execution=field('max_concurrent','Maximum concurrent heavy operations','number','1','Use 1 for large files or extreme compression. Queued work waits for a slot. Lowering the limit lets current work finish.');Object.assign(execution.querySelector('input'),{min:'1',max:'4',step:'1',required:true});
 const save=el('button','button primary','Save settings');save.type='submit';save.disabled=true;
 const footer=append(el('div','settings-footer'),save,status);
 append(form,el('h3','','OuthMan authentication'),url,id,secret,configured,el('h3','settings-section-title','File storage'),mode,compressionNote,el('h3','settings-section-title','Operation queue'),execution,error,footer);root.append(form);
 settingsView={root,form,error,status,url,id,secret,configured,mode,execution,save};
 setSettingsBusy(settingsView,true);
 form.onsubmit=async event=>{
  event.preventDefault();if(save.disabled)return;
  const data=new FormData(form);setSettingsBusy({form},true);error.hidden=true;status.textContent='Saving settings…';
  const outhman={base_url:String(data.get('base_url')).trim(),app_id:String(data.get('app_id')).trim()};
  if(data.get('app_secret'))outhman.app_secret=String(data.get('app_secret'));
  try{
   const result=await api('/api/admin/settings',{method:'PATCH',body:JSON.stringify({outhman,compression:{mode:data.get('compression_mode')},execution:{max_concurrent:Number(data.get('max_concurrent'))}})});
   if(!form.isConnected)return;
   secretInput.value='';if(outhman.app_secret)configured.textContent='Application secret: configured';else if(result.outhman)configured.textContent='Application secret: '+(result.outhman.secret_configured?'configured':'not configured');status.textContent='Settings saved.';notice('Settings updated');
  }catch(failure){if(!form.isConnected)return;error.textContent=failure.message;error.hidden=false;status.textContent='Settings were not saved.';}
  finally{if(form.isConnected)setSettingsBusy({form},false);}
 };
 return root;
}
async function loadSettings(){
 const view=settingsView;if(page!=='settings'||!view||state.user.role!=='admin')return;
 const controller=new AbortController();settingsController=controller;
 try{
  const data=await api('/api/admin/settings',{signal:controller.signal});
  if(controller.signal.aborted||settingsView!==view||!view.form.isConnected)return;
  view.url.querySelector('input').value=data.outhman.base_url||'';view.id.querySelector('input').value=data.outhman.app_id||'';
  view.secret.querySelector('input').value='';view.configured.textContent='Application secret: '+(data.outhman.secret_configured?'configured':'not configured');
  view.mode.querySelector('select').value=['off','zip','bz2','xz','auto'].includes(data.compression.mode)?data.compression.mode:'off';view.execution.querySelector('input').value=String(data.execution?.max_concurrent||1);view.status.textContent='';setSettingsBusy(view,false);
 }catch(error){
  if(controller.signal.aborted||!view.form.isConnected)return;
  view.form.setAttribute('aria-busy','false');view.error.textContent='Could not load settings. '+error.message;view.error.hidden=false;view.status.textContent='Reopen Settings to retry.';
 }finally{if(settingsController===controller)settingsController=null;}
}

function setSettingsBusy(view,busy){view.form.querySelectorAll('input,select,button').forEach(control=>{control.disabled=busy;});view.form.setAttribute('aria-busy',String(busy));}
