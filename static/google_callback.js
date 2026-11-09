'use strict';
(async()=>{
 const status=document.getElementById('google-status'),heading=document.getElementById('google-heading');
 const formatBytes=value=>(Number(value)/1e9).toLocaleString(undefined,{maximumFractionDigits:2})+' GB';
 // Remove the one-use authorization code from browser history immediately.
 history.replaceState(null,'','/auth/google/callback');
 try {
  const current=await fetch('/api/admin/state');
  if(!current.ok)throw Error('Your CSO session expired. Sign in and start connecting the drive again.');
  const state=await current.json();
  const response=await fetch('/api/admin/google/complete',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf},body:JSON.stringify(window.CSO_GOOGLE_RESULT)});
  delete window.CSO_GOOGLE_RESULT;
  const result=await response.json();
  if(!response.ok)throw Error(result.error||'Google Drive could not be connected.');
  heading.textContent='Google Drive connected';
  const details=['Account access verified.'];
  if(result.test?.account)details.push('Account: '+result.test.account+'.');
  if(Number.isFinite(result.test?.available))details.push('Available: '+formatBytes(result.test.available)+'.');
  if(Number.isFinite(result.test?.capacity))details.push('Provider capacity: '+formatBytes(result.test.capacity)+'.');
  details.push('Return to your workspace to view it. The test did not upload files.');
  status.textContent=details.join(' ');
 }catch(error){heading.textContent='Drive connection failed';status.textContent=error.message;status.className='alert error';}
 finally{delete window.CSO_GOOGLE_RESULT;}
})();
