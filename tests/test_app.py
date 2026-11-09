import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from app import create_app
from encrypted_payload import decrypt_content

class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'CSO_DATA_DIR':self.temp.name})
        self.env.start()
        self.app=create_app({'TESTING':True,'OUTHMAN_APP_ID':'a'*16,'OUTHMAN_APP_SECRET':'b'*32,'BOOTSTRAP_TOKEN':'bootstrap'})
        self.client=self.app.test_client()
        self.client.get('/') if os.path.exists('templates/login.html') else None
        with self.client.session_transaction() as s:
            s['csrf']='csrf'
        self.csrf={'X-CSRF-Token':'csrf'}
        self.db=sqlite3.connect(self.app.config['DATABASE'])
        self.db.row_factory=sqlite3.Row
    def tearDown(self):
        self.app.extensions['scheduler'].close()
        self.db.close()
        self.env.stop()
        self.temp.cleanup()
    def admin(self):
        cipher=Fernet(Path(self.temp.name+'/encryption.key').read_bytes())
        self.db.execute('INSERT INTO users VALUES(1,?,?,?,?,?,?)',('admin',generate_password_hash('password'),'admin',self.app.config['OUTHMAN_BASE_URL'],42,cipher.encrypt(('c'*32).encode()).decode()))
        self.db.execute('INSERT INTO sessions VALUES(?,?,?)',('session',1,9999999999))
        self.db.commit()
        with self.client.session_transaction() as s:
            s['login']='session'
    def post(self,path,data):
        return self.client.post(path,json=data,headers=self.csrf)
    def space(self,**kw):
        r=self.post('/api/admin/spaces',{'name':'Application','quota':100000,'encrypted':True,'bandwidth_limit':0}|kw)
        self.assertEqual(r.status_code,201,r.json)
        return r.json['key']
    def cloud(self,name='Cloud',capacity=15000000000):
        r=self.post('/api/admin/clouds',{'name':name,'capacity':capacity,'provider':'local','credentials':{}})
        self.assertEqual(r.status_code,201,r.json)
        return r.json['id']
    def upload(self,key,path='docs/test.txt',body=b'private content'):
        return self.client.post('/api/v1/files',headers={'Authorization':'Bearer '+key},data={'file':(io.BytesIO(body),'test.txt'),'path':path})
    def test_parallel_uploads_have_real_overlap_and_reserved_accounting(self):
        import threading,time
        self.admin()
        self.cloud(capacity=1000)
        self.space(encrypted=False,quota=1000)
        sid=self.db.execute('SELECT id FROM spaces').fetchone()[0]
        response=self.client.patch('/api/admin/settings',json={'execution':{'max_concurrent':2}},headers=self.csrf)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.client.get('/api/admin/settings').json['execution']['max_concurrent'],2)
        entered=threading.Barrier(3);release=threading.Event()
        def upload_provider(path,content):
            entered.wait(timeout=3)
            release.wait(3)
            return path
        ids=[]
        with patch('providers.Provider.put',side_effect=upload_provider):
            try:
                for path in ('a','b'):
                    ticket=self.post('/api/admin/uploads/start',dict(space_id=sid,path=path,size=400)).json
                    ids.append(ticket['operation_id'])
                    result=self.client.post('/api/admin/files',headers=self.csrf|{'X-CSO-Upload-Token':ticket['upload_token']},data={'space_id':str(sid),'path':path,'background':'true','file':(io.BytesIO(b'x'*400),path)})
                    self.assertEqual(result.status_code,202,result.json)
                entered.wait(timeout=3)
                self.assertEqual(self.db.execute('SELECT sum(stored_size) FROM upload_reservations').fetchone()[0],800)
                self.assertEqual(self.client.delete('/api/admin/clouds/1',headers=self.csrf).status_code,409)
                self.assertEqual(self.client.delete(f'/api/admin/spaces/{sid}',headers=self.csrf).status_code,409)
            finally:release.set()
            for attempt in range(200):
                rows=self.client.get('/api/admin/operations').json['operations']
                jobs=[r for r in rows if r['id'] in ids]
                if len(jobs)==2 and all(r['status']=='completed' for r in jobs):break
                time.sleep(.01)
            self.assertTrue(all(r['status']=='completed' for r in jobs),jobs)
        self.assertEqual(self.db.execute('SELECT used FROM clouds').fetchone()[0],800)
        self.assertEqual(self.db.execute('SELECT used FROM spaces').fetchone()[0],800)
        self.assertEqual(self.db.execute('SELECT count(*) FROM upload_reservations').fetchone()[0],0)

    def test_space_compression_override_persists_and_restores_content(self):
        self.admin()
        self.cloud()
        key=self.space(compression_mode='xz')
        sid=self.db.execute('SELECT id FROM spaces').fetchone()[0]
        original=b'lossless compressed data '*1000
        result=self.upload(key,'compressed',original)
        self.assertEqual(result.status_code,201,result.json)
        self.assertEqual(result.json['compression'],'xz')
        self.assertEqual(self.client.get('/api/v1/files/'+str(result.json['id']),headers={'X-API-Key':key}).data,original)
        response=self.client.patch(f'/api/admin/spaces/{sid}',json={'quota':100000,'bandwidth_limit':0,'compression_mode':'auto','max_upload_mib':400},headers=self.csrf)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.db.execute('SELECT compression_mode,max_upload_mib FROM spaces').fetchone()[:],('auto',400))
        result=self.upload(key,'auto',original)
        self.assertEqual(result.status_code,201,result.json)
        self.assertIn(result.json['compression'],('zip','bz2','xz'))
        self.assertEqual(self.client.get('/api/v1/files/'+str(result.json['id']),headers={'X-API-Key':key}).data,original)

    def test_pause_resume_endpoint_safe_checkpoint_and_permissions(self):
        import threading,time
        self.admin()
        log=self.app.extensions['operations'];scheduler=self.app.extensions['scheduler']
        oid=log.start('upload','file','admin')
        entered=threading.Event();continue_work=threading.Event();done=threading.Event()
        def job():
            entered.set();continue_work.wait(3);scheduler.checkpoint();log.finish(oid,True,'done');done.set()
        try:
            scheduler.submit(oid,job);self.assertTrue(entered.wait(2))
            self.assertEqual(self.post(f'/api/admin/operations/{oid}/pause',{}).status_code,200)
            continue_work.set()
            for _ in range(200):
                state=self.client.get(f'/api/admin/operations/{oid}').json['operation']
                if state['status']=='paused':break
                time.sleep(.01)
            self.assertEqual(state['status'],'paused')
            self.assertFalse(done.is_set())
            self.assertEqual(self.post(f'/api/admin/operations/{oid}/resume',{}).status_code,200)
            self.assertTrue(done.wait(2))
        finally:continue_work.set();scheduler.resume(oid)
        self.assertEqual(self.post('/api/admin/operations/99999/pause',{}).status_code,409)
        self.assertEqual(self.client.post(f'/api/admin/operations/{oid}/pause',json={}).status_code,403)

    def test_operation_details_permissions_cursor_and_persistent_steps(self):
        self.admin()
        log=self.app.extensions['operations']
        oid=log.start('upload','report','admin')
        log.update(oid,total=42,completed=7,message='Uploaded part 7 of 41.')
        result=self.client.get(f'/api/admin/operations/{oid}?limit=1')
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['operation']['completed'],7)
        self.assertTrue(result.json['has_more'])
        after=result.json['events'][0]['id']
        next_page=self.client.get(f'/api/admin/operations/{oid}?after={after}')
        self.assertEqual(next_page.json['events'][0]['message'],'Uploaded part 7 of 41.')
        self.assertEqual(self.client.get('/api/admin/operations/99999').status_code,404)
        self.assertEqual(self.client.get(f'/api/admin/operations/{oid}?after=invalid').status_code,400)
        self.db.execute("UPDATE users SET role='manager' WHERE id=1");self.db.commit()
        self.assertEqual(self.client.get(f'/api/admin/operations/{oid}').status_code,403)

    def test_space_upload_limits_enforced_for_preflight_and_api(self):
        self.admin()
        self.cloud()
        key=self.space(max_upload_mib=1,quota=10000000)
        sid=self.db.execute('SELECT id FROM spaces').fetchone()[0]
        self.assertEqual(self.client.get('/api/admin/state').json['spaces'][0]['max_upload_mib'],1)
        self.assertEqual(self.post('/api/admin/uploads/start',{'space_id':sid,'path':'large','size':2*1024*1024}).status_code,413)
        self.assertEqual(self.upload(key,body=b'x'*(1024*1024+1)).status_code,413)
        response=self.client.patch(f'/api/admin/spaces/{sid}',json={'quota':10000000,'bandwidth_limit':0,'max_upload_mib':2},headers=self.csrf)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.upload(key,body=b'x'*(1024*1024+1)).status_code,201)
        self.assertEqual(self.client.patch(f'/api/admin/spaces/{sid}',json={'quota':10000000,'bandwidth_limit':0,'max_upload_mib':999999},headers=self.csrf).status_code,400)

    def test_upload_ticket_async_worker_progress_and_cleanup(self):
        import threading,time
        self.admin()
        self.cloud()
        self.space()
        sid=self.db.execute('SELECT id FROM spaces').fetchone()[0]
        started=self.post('/api/admin/uploads/start',dict(space_id=sid,path='report.txt',size=7))
        self.assertEqual(started.status_code,200,started.json)
        token=started.json['upload_token'];oid=started.json['operation_id']
        self.assertNotIn(token,self.client.get('/api/admin/operations').get_data(as_text=True))
        entered=threading.Event();release=threading.Event()
        def blocking_put(path,content):
            entered.set()
            if not release.wait(5): raise RuntimeError('Test worker timed out')
            return 'opaque-object'
        with patch('providers.Provider.put',side_effect=blocking_put):
            result=self.client.post('/api/admin/files',headers=self.csrf|{'X-CSO-Upload-Token':token},data={'space_id':str(sid),'path':'report.txt','background':'true','file':(io.BytesIO(b'payload'),'report.txt')})
            self.assertEqual(result.status_code,202,result.json)
            try:
                self.assertTrue(entered.wait(2))
                job=next(r for r in self.client.get('/api/admin/operations').json['operations'] if r['id']==oid)
                self.assertEqual(job['status'],'running')
            finally: release.set()
            for attempt in range(100):
                job=next(r for r in self.client.get('/api/admin/operations').json['operations'] if r['id']==oid)
                if job['status'] in ('completed','failed'): break
                time.sleep(.02)
            self.assertEqual(job['status'],'completed',job)
        for attempt in range(100):
            if not list(Path(self.temp.name,'pending_uploads').glob('*.upload')): break
            time.sleep(.01)
        self.assertFalse(list(Path(self.temp.name,'pending_uploads').glob('*.upload')))
        self.assertEqual(self.db.execute('SELECT path FROM files').fetchone()[0],'report.txt')
        replay=self.client.post('/api/admin/files',headers=self.csrf|{'X-CSO-Upload-Token':token},data={'space_id':str(sid),'file':(io.BytesIO(b'payload'),'report.txt')})
        self.assertEqual(replay.status_code,409)

    def test_background_upload_failure_keeps_specific_error_and_no_catalog(self):
        import time
        from providers import ProviderError
        self.admin()
        self.cloud()
        self.space()
        sid=self.db.execute('SELECT id FROM spaces').fetchone()[0]
        ticket=self.post('/api/admin/uploads/start',dict(space_id=sid,path='file',size=3)).json
        with patch('providers.Provider.put',side_effect=ProviderError('Backblaze transfer timed out.')):
            response=self.client.post('/api/admin/files',headers=self.csrf|{'X-CSO-Upload-Token':ticket['upload_token']},data={'space_id':str(sid),'path':'file','background':'true','file':(io.BytesIO(b'abc'),'file')})
            self.assertEqual(response.status_code,202,response.json)
            for attempt in range(100):
                job=next(r for r in self.client.get('/api/admin/operations').json['operations'] if r['id']==ticket['operation_id'])
                if job['status']=='failed': break
                time.sleep(.02)
            self.assertEqual(job['status'],'failed',job)
            self.assertIn('timed out',job['message'])
        for attempt in range(100):
            if not list(Path(self.temp.name,'pending_uploads').glob('*.upload')): break
            time.sleep(.01)
        self.assertFalse(list(Path(self.temp.name,'pending_uploads').glob('*.upload')))
        self.assertEqual(self.db.execute('SELECT count(*) FROM files').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT used FROM clouds').fetchone()[0],0)

    def test_upload_ticket_binding_and_browser_failure(self):
        self.admin()
        self.cloud()
        self.space()
        sid=self.db.execute('SELECT id FROM spaces').fetchone()[0]
        start=self.post('/api/admin/uploads/start',dict(space_id=sid,path='file',size=3)).json
        response=self.client.post('/api/admin/files',headers=self.csrf|{'X-CSO-Upload-Token':start['upload_token']},data={'space_id':str(sid),'path':'different','background':'true','file':(io.BytesIO(b'abc'),'file')})
        self.assertEqual(response.status_code,409)
        start=self.post('/api/admin/uploads/start',dict(space_id=sid,path='file',size=3)).json
        self.assertEqual(self.post('/api/admin/uploads/failed',{'upload_token':start['upload_token']}).status_code,200)
        job=next(r for r in self.client.get('/api/admin/operations').json['operations'] if r['id']==start['operation_id'])
        self.assertEqual(job['status'],'failed')
        self.assertEqual(self.db.execute('SELECT count(*) FROM files').fetchone()[0],0)

    def test_upload_request_limit_has_clear_error_and_allows_317mb_by_default(self):
        self.assertGreater(self.app.config['MAX_CONTENT_LENGTH'],317*1024*1024)
        self.admin()
        self.cloud()
        key=self.space()
        self.app.config['MAX_CONTENT_LENGTH']=1024
        response=self.upload(key,body=b'x'*2048)
        self.assertEqual(response.status_code,413,response.json)
        self.assertIn('server limit',response.json['error'])
        self.assertIn('CSO_MAX_UPLOAD_MIB',response.json['error'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM files').fetchone()[0],0)

    def test_settings_secret_masking_persistence_permissions_and_validation(self):
        self.admin()
        self.assertEqual(self.client.get('/api/admin/settings').status_code,200)
        config=self.client.get('/api/admin/settings').json
        self.assertTrue(config['outhman']['secret_configured'])
        self.assertNotIn('app_secret',config['outhman'])
        payload={'outhman':{'base_url':self.app.config['OUTHMAN_BASE_URL'],'app_id':'a'*16,'app_secret':'d'*32},'compression':{'mode':'zip'}}
        self.assertEqual(self.client.patch('/api/admin/settings',json=payload).status_code,403)
        response=self.client.patch('/api/admin/settings',json=payload,headers=self.csrf)
        self.assertEqual(response.status_code,200,response.json)
        stored=self.db.execute("SELECT value FROM settings WHERE name='outhman'").fetchone()[0]
        self.assertNotIn('d'*32,stored)
        self.assertEqual(self.app.config['OUTHMAN_APP_SECRET'],'d'*32)
        self.assertEqual(self.client.patch('/api/admin/settings',json={'outhman':{'app_secret':''}},headers=self.csrf).status_code,200)
        restarted=create_app({'TESTING':True})
        self.assertEqual(restarted.config['OUTHMAN_APP_SECRET'],'d'*32)
        self.assertEqual(self.client.get('/api/admin/settings').json['compression']['mode'],'zip')
        self.assertEqual(self.client.patch('/api/admin/settings',json={'outhman':{'app_id':'b'*16}},headers=self.csrf).status_code,409)
        self.assertEqual(self.client.patch('/api/admin/settings',json={'compression':{'mode':'invalid'}},headers=self.csrf).status_code,400)
        self.db.execute("UPDATE users SET role='manager' WHERE id=1");self.db.commit()
        self.assertEqual(self.client.get('/api/admin/settings').status_code,403)

    def test_zip_compression_before_encryption_and_transparent_download(self):
        import zipfile
        self.admin()
        cid=self.cloud(capacity=2000)
        key=self.space(quota=1000000)
        self.assertEqual(self.client.patch('/api/admin/settings',json={'compression':{'mode':'zip'}},headers=self.csrf).status_code,200)
        original=b'compressible data '*10000
        result=self.upload(key,'large.txt',original)
        self.assertEqual(result.status_code,201,result.json)
        self.assertEqual(result.json['compression'],'zip')
        self.assertLess(result.json['stored_size'],2000)
        row=self.db.execute('SELECT * FROM files').fetchone()
        stored=Path(self.temp.name,'objects',str(cid),row['object_id']).read_bytes()
        cipher=Fernet(Path(self.temp.name,'encryption.key').read_bytes())
        with zipfile.ZipFile(io.BytesIO(decrypt_content(cipher,stored))) as archive:
            self.assertEqual(archive.read('payload'),original)
        self.assertEqual(self.client.get('/api/v1/files/'+str(row['id']),headers={'X-API-Key':key}).data,original)
        self.assertEqual(self.db.execute('SELECT used FROM clouds').fetchone()[0],row['stored_size'])
        self.assertEqual(self.db.execute('SELECT used FROM spaces').fetchone()[0],len(original))
        self.assertEqual(self.client.patch('/api/admin/settings',json={'compression':{'mode':'off'}},headers=self.csrf).status_code,200)
        self.assertEqual(self.client.get('/api/v1/files/'+str(row['id']),headers={'X-API-Key':key}).data,original)
        self.assertEqual(self.client.delete('/api/admin/files/'+str(row['id']),headers=self.csrf).status_code,200)
        self.assertEqual(self.db.execute('SELECT used FROM clouds').fetchone()[0],0)

    def test_zip_skips_incompressible_input(self):
        self.admin()
        self.cloud()
        key=self.space(encrypted=False)
        self.client.patch('/api/admin/settings',json={'compression':{'mode':'zip'}},headers=self.csrf)
        original=os.urandom(1024)
        result=self.upload(key,'random.bin',original)
        self.assertEqual(result.status_code,201,result.json)
        self.assertEqual(result.json['compression'],'none')
        self.assertEqual(result.json['stored_size'],len(original))
        self.assertEqual(self.client.get('/api/v1/files/'+str(result.json['id']),headers={'X-API-Key':key}).data,original)

    def test_async_deletion_progress_and_audit_survive_provider_work(self):
        import threading,time
        self.admin()
        self.cloud()
        key=self.space(encrypted=False)
        self.upload(key,'file.txt',b'content')
        sid=self.db.execute('SELECT id FROM spaces').fetchone()[0]
        entered=threading.Event();release=threading.Event()
        def blocking_delete(*args):
            entered.set()
            if not release.wait(5): raise RuntimeError('Test worker timed out')
        with patch('providers.Provider.delete',side_effect=blocking_delete):
            response=self.client.delete(f'/api/admin/spaces/{sid}',json={'delete_files':True,'confirm_name':'Application','background':True},headers=self.csrf)
            self.assertEqual(response.status_code,202,response.json)
            oid=response.json['operation_id']
            try:
                self.assertTrue(entered.wait(2))
                running=self.client.get('/api/admin/operations').json['operations']
                job=next(r for r in running if r['id']==oid)
                self.assertEqual(job['status'],'running')
                self.assertEqual(job['total'],2)
                # Log reads remain available while the catalog write lock is held.
                duplicate=self.client.delete(f'/api/admin/spaces/{sid}',json={'delete_files':True,'confirm_name':'Application','background':True},headers=self.csrf)
                self.assertEqual(duplicate.status_code,409)
            finally:
                release.set()
            for attempt in range(100):
                job=next(r for r in self.client.get('/api/admin/operations').json['operations'] if r['id']==oid)
                if job['status'] in ('completed','failed'): break
                time.sleep(.02)
            self.assertEqual(job['status'],'completed',job)
            self.assertEqual(job['completed'],job['total'])
        self.assertEqual(self.client.get('/api/v1/storage',headers={'X-API-Key':key}).status_code,401)
        self.db.execute("UPDATE users SET role='manager' WHERE id=1");self.db.commit()
        self.assertEqual(self.client.get('/api/admin/operations').status_code,403)

    def test_encrypted_upload_hides_name_and_detects_tampering(self):
        self.admin()
        cid=self.cloud()
        key=self.space()
        result=self.upload(key,'secret-name.pdf',b'%PDF confidential')
        self.assertEqual(result.status_code,201,result.json)
        row=self.db.execute('SELECT * FROM files').fetchone()
        remote=Path(self.temp.name,'objects',str(cid),row['object_id'])
        self.assertNotIn('secret-name',str(remote))
        self.assertEqual(remote.suffix,'')
        cipher=Fernet(Path(self.temp.name,'encryption.key').read_bytes())
        self.assertEqual(decrypt_content(cipher,remote.read_bytes()),b'%PDF confidential')
        self.assertEqual(self.client.get('/api/v1/files/'+str(row['id']),headers={'X-API-Key':key}).data,b'%PDF confidential')
        token=bytearray(remote.read_bytes());token[len(token)//2]^=1;remote.write_bytes(token)
        response=self.client.get('/api/v1/files/'+str(row['id']),headers={'X-API-Key':key})
        self.assertGreaterEqual(response.status_code,400)
        self.assertNotIn(b'%PDF confidential',response.data)
        history=self.client.get('/api/admin/operations').json['operations']
        self.assertTrue(any(r['status']=='failed' and r['action']=='download_file' for r in history))

    def test_bulk_delete_space_requires_confirmation_and_removes_provider_files(self):
        self.admin()
        cid=self.cloud()
        key=self.space()
        self.assertEqual(self.upload(key,'one.txt').status_code,201)
        self.assertEqual(self.upload(key,'two.txt').status_code,201)
        sid=self.db.execute('SELECT id FROM spaces').fetchone()[0]
        url=f'/api/admin/spaces/{sid}'
        self.assertEqual(self.client.delete(url,json={'delete_files':True,'confirm_name':'wrong'},headers=self.csrf).status_code,409)
        response=self.client.delete(url,json={'delete_files':True,'confirm_name':'Application'},headers=self.csrf)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['deleted_files'],2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM files').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT used FROM clouds WHERE id=?',(cid,)).fetchone()[0],0)
        self.assertFalse(list(Path(self.temp.name,'objects').rglob('*.txt')))
        self.assertEqual(self.client.get('/api/v1/storage',headers={'X-API-Key':key}).status_code,401)

    def test_bulk_delete_preserves_successful_progress_on_provider_failure(self):
        from providers import ProviderError
        self.admin()
        self.cloud()
        key=self.space(encrypted=False)
        self.upload(key,'one.txt',b'one')
        self.upload(key,'two.txt',b'two')
        sid=self.db.execute('SELECT id FROM spaces').fetchone()[0]
        url=f'/api/admin/spaces/{sid}'
        with patch('providers.Provider.delete',side_effect=[None,ProviderError('failure')]):
            response=self.client.delete(url,json={'delete_files':True,'confirm_name':'Application'},headers=self.csrf)
        self.assertEqual(response.status_code,502,response.json)
        self.assertIn('after 1 files',response.json['error'])
        self.assertEqual(self.db.execute('SELECT path FROM files').fetchone()[0],'two.txt')
        self.assertEqual(self.db.execute('SELECT used FROM spaces').fetchone()[0],3)
        self.assertEqual(self.db.execute('SELECT used FROM clouds').fetchone()[0],3)
        self.assertEqual(self.client.get('/api/v1/storage',headers={'X-API-Key':key}).status_code,200)
        response=self.client.delete(url,json={'delete_files':True,'confirm_name':'Application'},headers=self.csrf)
        self.assertEqual(response.status_code,200,response.json)

    def test_delete_storage_space_revokes_key_and_protects_files(self):
        self.admin()
        self.cloud()
        key=self.space(encrypted=False)
        sid=self.db.execute('SELECT id FROM spaces').fetchone()[0]
        self.assertEqual(self.client.delete(f'/api/admin/spaces/{sid}').status_code,403)
        uploaded=self.upload(key,body=b'')
        self.assertEqual(uploaded.status_code,201)
        self.assertEqual(self.client.delete(f'/api/admin/spaces/{sid}',headers=self.csrf).status_code,409)
        self.assertEqual(self.client.delete('/api/admin/files/'+str(uploaded.json['id']),headers=self.csrf).status_code,200)
        self.assertEqual(self.client.delete(f'/api/admin/spaces/{sid}',headers=self.csrf).status_code,200)
        self.assertEqual(self.client.get('/api/v1/storage',headers={'X-API-Key':key}).status_code,401)
        self.assertEqual(self.db.execute('SELECT count(*) FROM clouds').fetchone()[0],1)
        self.assertEqual(self.client.delete(f'/api/admin/spaces/{sid}',headers=self.csrf).status_code,404)

    def test_detect_capacity_and_refresh_uses_available_plus_stored(self):
        self.admin()
        with patch('providers.Provider.check',return_value={'available':12345}):
            response=self.post('/api/admin/clouds',dict(name='Auto',provider='google',credentials={'access_token':'token'},auto_capacity=True))
        self.assertEqual(response.status_code,201,response.json)
        cid=response.json['id']
        self.assertEqual(self.db.execute('SELECT capacity FROM clouds WHERE id=?',(cid,)).fetchone()[0],12345)
        self.db.execute('UPDATE clouds SET used=50 WHERE id=?',(cid,));self.db.commit()
        with patch('providers.Provider.check',return_value={'available':100}):
            response=self.post(f'/api/admin/clouds/{cid}/capacity',{})
        self.assertEqual(response.json['capacity'],150)
        with patch('providers.Provider.check',return_value={'ok':True}):
            response=self.post(f'/api/admin/clouds/{cid}/capacity',{})
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.db.execute('SELECT capacity FROM clouds WHERE id=?',(cid,)).fetchone()[0],150)
        with patch('providers.Provider.check',return_value={'available':0}):
            response=self.post('/api/admin/clouds',dict(name='Full',provider='local',credentials={},auto_capacity=True))
        self.assertEqual(response.status_code,400)

    def test_delete_empty_cloud_unpins_spaces_and_preserves_keys(self):
        self.admin()
        cid=self.cloud()
        key=self.space()
        self.db.execute('UPDATE spaces SET cloud_id=?',(cid,));self.db.commit()
        response=self.client.delete(f'/api/admin/clouds/{cid}',headers=self.csrf)
        self.assertEqual(response.status_code,200,response.json)
        self.assertIsNone(self.db.execute('SELECT cloud_id FROM spaces').fetchone()[0])
        self.assertEqual(self.client.get('/api/v1/storage',headers={'X-API-Key':key}).status_code,200)
        self.assertEqual(self.db.execute('SELECT count(*) FROM clouds').fetchone()[0],0)
        self.assertEqual(self.client.delete(f'/api/admin/clouds/{cid}',headers=self.csrf).status_code,404)
        self.cloud('Replacement')
        self.assertEqual(self.upload(key).status_code,201)

    def test_delete_cloud_blocks_files_even_when_zero_bytes(self):
        self.admin()
        cid=self.cloud()
        key=self.space(encrypted=False)
        uploaded=self.upload(key,body=b'')
        self.assertEqual(uploaded.status_code,201,uploaded.json)
        self.assertEqual(self.client.delete(f'/api/admin/clouds/{cid}',headers=self.csrf).status_code,409)
        response=self.client.delete('/api/admin/files/'+str(uploaded.json['id']),headers=self.csrf)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.client.delete(f'/api/admin/clouds/{cid}',headers=self.csrf).status_code,200)

    def test_delete_cloud_requires_admin_and_csrf_and_blocks_usage(self):
        self.admin()
        cid=self.cloud()
        self.assertEqual(self.client.delete(f'/api/admin/clouds/{cid}').status_code,403)
        self.db.execute('UPDATE clouds SET used=1 WHERE id=?',(cid,));self.db.commit()
        self.assertEqual(self.client.delete(f'/api/admin/clouds/{cid}',headers=self.csrf).status_code,409)
        self.db.execute("UPDATE users SET role='manager' WHERE id=1");self.db.commit()
        self.assertEqual(self.client.delete(f'/api/admin/clouds/{cid}',headers=self.csrf).status_code,403)

    def test_b2_credentials_and_bucket_rotation_guard(self):
        self.admin()
        credentials=dict(key_id='key',application_key='b2-secret',bucket_id='bucket',bucket_name='bucket-name')
        response=self.post('/api/admin/clouds',dict(name='B2',provider='b2',capacity=1000,credentials=credentials))
        self.assertEqual(response.status_code,201,response.json)
        cid=response.json['id']
        stored=self.db.execute('SELECT credentials FROM clouds WHERE id=?',(cid,)).fetchone()[0]
        self.assertNotIn('b2-secret',stored)
        with patch('providers.Provider.check',return_value={'ok':True}):
            self.assertEqual(self.post('/api/admin/clouds/test',dict(provider='b2',credentials=credentials)).status_code,200)
        response=self.client.patch(f'/api/admin/clouds/{cid}',json=dict(capacity=2000,credentials=credentials|{'bucket_id':'different'}),headers=self.csrf)
        self.assertEqual(response.status_code,400)
        response=self.client.patch(f'/api/admin/clouds/{cid}',json=dict(capacity=2000,credentials=credentials|{'application_key':'new-secret'}),headers=self.csrf)
        self.assertEqual(response.status_code,200,response.json)

    def test_mega_account_credentials_are_encrypted_and_editable(self):
        self.admin()
        credentials={'email':'person@example.com','password':'mega-secret'}
        response=self.post('/api/admin/clouds',{'name':'MEGA','provider':'mega','capacity':1000,'credentials':credentials})
        self.assertEqual(response.status_code,201,response.json)
        cid=response.json['id']
        stored=self.db.execute('SELECT credentials FROM clouds WHERE id=?',(cid,)).fetchone()[0]
        self.assertNotIn('mega-secret',stored)
        with patch('providers.Provider.check',return_value={'ok':True}) as check:
            self.assertEqual(self.post('/api/admin/clouds/test',{'provider':'mega','credentials':credentials}).status_code,200)
            self.assertEqual(self.post(f'/api/admin/clouds/{cid}/test',{}).status_code,200)
            self.assertEqual(check.call_count,2)
        response=self.client.patch(f'/api/admin/clouds/{cid}',json={'capacity':2000,'credentials':credentials},headers=self.csrf)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.post('/api/admin/clouds',{'name':'Bad MEGA','provider':'mega','capacity':1000,'credentials':{'email':'person@example.com'}}).status_code,400)

    def test_capacity_encryption_pin_rotation_isolation(self):
        self.admin()
        first=self.cloud('Cloud one')
        self.cloud('Cloud two')
        state=self.client.get('/api/admin/state').json
        self.assertEqual(sum(c['capacity'] for c in state['clouds']),30000000000)
        key=self.space()
        r=self.upload(key)
        self.assertEqual(r.status_code,201,r.json)
        fid=r.json['id']
        object_id=self.db.execute('SELECT object_id FROM files WHERE id=?',(fid,)).fetchone()[0]
        self.assertTrue(object_id.startswith('CSO/objects/'))
        self.assertNotIn('test.txt',object_id)
        self.assertNotIn('Application',object_id)
        disk=Path(self.temp.name,'objects',str(first),object_id).read_bytes()
        cipher=Fernet(Path(self.temp.name,'encryption.key').read_bytes())
        self.assertEqual(decrypt_content(cipher,disk),b'private content')
        self.assertNotIn(b'private content',disk)
        self.assertEqual(self.client.get(f'/api/v1/files/{fid}',headers={'X-API-Key':key}).data,b'private content')
        self.cloud('Larger cloud',99999999999)
        self.assertEqual(self.upload(key,'next.txt').status_code,201)
        self.assertEqual(self.db.execute('SELECT cloud_id FROM spaces').fetchone()[0],first)
        other=self.space(name='Other')
        self.assertEqual(self.client.get(f'/api/v1/files/{fid}',headers={'X-API-Key':other}).status_code,404)
        rotated=self.post('/api/admin/spaces/1/rotate',{}).json['key']
        self.assertEqual(len(rotated),32)
        self.assertEqual(self.client.get('/api/v1/storage',headers={'X-API-Key':key}).status_code,401)
        self.assertEqual(self.client.delete(f'/api/v1/files/{fid}',headers={'X-API-Key':rotated}).status_code,200)
    def test_rendered_templates_and_limit_editing(self):
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'OuthMan',response.data)
        self.admin()
        self.assertIn(b'id="page-title">Dashboard',self.client.get('/').data)
        cid=self.cloud()
        key=self.space()
        r=self.client.patch('/api/admin/spaces/1',headers=self.csrf,json={'quota':200000,'bandwidth_limit':1000})
        self.assertEqual(r.status_code,200,r.json)
        r=self.client.patch('/api/admin/clouds/'+str(cid),headers=self.csrf,json={'capacity':1000000})
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(self.client.get('/api/v1/storage',headers={'X-API-Key':key}).json['quota'],200000)
        for path in ('/static/app.js','/static/login.js','/static/style.css'):
            with self.client.get(path) as asset:
                self.assertEqual(asset.status_code,200)

    def test_limits_traversal_csrf(self):
        self.admin()
        self.cloud(capacity=1000)
        key=self.space(quota=10,bandwidth_limit=8)
        self.assertEqual(self.upload(key,'../escape').status_code,400)
        self.assertEqual(self.upload(key,body=b'x'*11).status_code,413)
        self.assertEqual(self.upload(key,body=b'x'*8).status_code,201)
        self.assertEqual(self.client.get('/api/v1/files/1',headers={'X-API-Key':key}).status_code,400)
        self.assertEqual(self.client.post('/api/admin/spaces',json={}).status_code,403)
    def test_enrollment_and_login_binding(self):
        linked={'app_id':'a'*16,'user_id':42,'username':'admin','user_key':'c'*32}
        response=unittest.mock.Mock(status_code=201)
        response.json.return_value=linked
        with patch('app.requests.post',return_value=response):
            r=self.post('/auth/enroll',{'username':'admin','password':'password','code':'012345','invitation':'bootstrap'})
        self.assertEqual(r.status_code,200,r.json)
        self.assertNotIn('user_key',r.json)
        with self.client.session_transaction() as s:
            s['csrf']='csrf'
        self.post('/auth/logout',{})
        with self.client.session_transaction() as s:
            s['csrf']='csrf'
        def remote(url,**kwargs):
            payload=kwargs['json']
            result=unittest.mock.Mock(status_code=200)
            if url.endswith('/verify'):
                self.assertEqual(payload['code'],'012345')
                result.json.return_value={'auth_pin':'d'*32,'expires_at':9999999999,'request_id':payload['request_id']}
            else:
                result.json.return_value={'verified':True,'app_id':'a'*16,'user_id':999,'request_id':payload['request_id']}
            return result
        with patch('app.requests.post',side_effect=remote):
            r=self.post('/auth/login',{'username':'admin','password':'password','code':'012345'})
        self.assertEqual(r.status_code,400)
        self.assertEqual(self.client.get('/api/admin/state').status_code,401)
    def test_successful_login_confirm_consumes_pending(self):
        self.admin()
        with self.client.session_transaction() as session:
            session.pop('login')
        calls=[]
        def remote(url,**kwargs):
            calls.append(url)
            payload=kwargs['json']
            result=unittest.mock.Mock(status_code=200)
            if url.endswith('/verify'):
                result.json.return_value={'auth_pin':'d'*32,'expires_at':9999999999,'request_id':payload['request_id']}
            else:
                result.json.return_value={'verified':True,'app_id':'a'*16,'user_id':42,'request_id':payload['request_id']}
            return result
        with patch('app.requests.post',side_effect=remote):
            r=self.post('/auth/login',{'username':'admin','password':'password','code':'012345'})
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(len(calls),2)
        self.assertEqual(self.db.execute('SELECT completed FROM pending').fetchone()[0],1)
        self.assertEqual(self.client.get('/api/admin/state').status_code,200)
        self.assertEqual(self.post('/auth/logout',{}).status_code,403) # session rotated its CSRF

    def test_revocation_and_manager_permissions(self):
        self.admin()
        cipher=Fernet(Path(self.temp.name+'/encryption.key').read_bytes())
        self.db.execute('INSERT INTO users VALUES(2,?,?,?,?,?,?)',('manager',generate_password_hash('password'),'manager',self.app.config['OUTHMAN_BASE_URL'],43,cipher.encrypt(('c'*32).encode()).decode()))
        self.db.execute('INSERT INTO sessions VALUES(?,?,?)',('manager_session',2,9999999999))
        self.db.commit()
        manager=self.app.test_client()
        with manager.session_transaction() as session:
            session['login']='manager_session'
            session['csrf']='csrf'
        self.assertEqual(manager.get('/api/admin/state').json['users'],[])
        self.assertEqual(manager.post('/api/admin/clouds',json={},headers=self.csrf).status_code,403)
        r=self.client.delete('/api/admin/users/2',headers=self.csrf)
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(manager.get('/api/admin/state').status_code,401)
        self.assertEqual(self.db.execute('SELECT role FROM users WHERE id=2').fetchone()[0],'revoked')
        self.assertEqual(self.client.patch('/api/admin/users/2',json={'role':'manager'},headers=self.csrf).status_code,200)
        self.assertEqual(self.client.delete('/api/admin/users/1',headers=self.csrf).status_code,400)

    def test_pinned_full_cloud_and_monthly_reset(self):
        self.admin()
        self.cloud(capacity=10)
        key=self.space(encrypted=False)
        self.assertEqual(self.upload(key,body=b'12345678').status_code,201)
        self.cloud('New cloud',100000)
        self.assertEqual(self.upload(key,'another.txt',b'123').status_code,507)
        self.db.execute("UPDATE spaces SET bandwidth_used=999999,month='2000-01'")
        self.db.commit()
        self.assertEqual(self.client.get('/api/v1/storage',headers={'X-API-Key':key}).json['bandwidth_used'],0)
        self.assertEqual(self.upload(key,'DOCS/TEST.TXT',b'1').status_code,409)

    def test_missing_configuration_does_not_consume_attempts(self):
        self.app.config['OUTHMAN_APP_SECRET']=''
        for _ in range(11):
            response=self.post('/auth/login',{'username':'admin','password':'password','code':'012345'})
            self.assertEqual(response.status_code,503)
        self.assertEqual(self.db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
        self.app.config['OUTHMAN_APP_SECRET']='b'*31
        self.assertEqual(self.post('/auth/login',{'username':'admin','password':'password','code':'012345'}).status_code,503)

    def test_throttle_reports_remaining_time_without_extending_window(self):
        import time
        expiry=time.time()+120
        self.db.execute('INSERT INTO attempts VALUES(?,?,?)',('127.0.0.1:admin',10,expiry))
        self.db.commit()
        response=self.post('/auth/login',{'username':'admin','password':'password','code':'012345'})
        self.assertEqual(response.status_code,429)
        self.assertTrue(1<=response.json['retry_after']<=120)
        self.assertEqual(int(response.headers['Retry-After']),response.json['retry_after'])
        self.assertEqual(self.db.execute('SELECT expires FROM attempts').fetchone()[0],expiry)

    def test_upstream_errors_are_actionable_without_exposing_response(self):
        cases=[(401,'Invalid app credentials.','server app credentials'),
               (401,'Verification failed or expired.','account or code'),
               (409,'Already enrolled','already linked'),
               (429,'Throttled','15-minute window'),
               (500,'internal detail','HTTP 500')]
        for status,detail,expected in cases:
            with self.subTest(status=status,detail=detail):
                response=unittest.mock.Mock(status_code=status)
                response.json.return_value={'error':detail,'user_key':'do-not-expose'}
                with patch('app.requests.post',return_value=response):
                    result=self.post('/auth/enroll',{'username':'admin','password':'password','code':'012345','invitation':'bootstrap'})
                self.assertEqual(result.status_code,400)
                self.assertIn(expected,result.json['error'])
                self.assertNotIn('do-not-expose',result.get_data(as_text=True))
                self.assertEqual(self.db.execute('SELECT count(*) FROM users').fetchone()[0],0)

    def google_start(self, **changes):
        self.admin()
        payload={'name':'Google account','capacity':15000000000,'client_config':{'installed':{'client_id':'test-client.apps.googleusercontent.com','client_secret':'client-secret','redirect_uris':['http://localhost']}}}
        payload.update(changes)
        return self.post('/api/admin/google/connect',payload)

    def test_drive_probe_routes_require_admin_and_do_not_save_credentials(self):
        self.admin()
        cid=self.cloud()
        self.assertEqual(self.post('/api/admin/clouds/'+str(cid)+'/test',{}).status_code,200)
        count=self.db.execute('SELECT count(*) FROM clouds').fetchone()[0]
        with patch('providers.Provider.check',return_value={'ok':True,'account':'test@example.com','account_id':'google-user-1'}) as probe:
            response=self.post('/api/admin/clouds/test',{'provider':'google','credentials':{'access_token':'private-token'}})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(probe.call_count,1)
        self.assertNotIn('private-token',response.get_data(as_text=True))
        self.assertEqual(self.db.execute('SELECT count(*) FROM clouds').fetchone()[0],count)
        self.assertEqual(self.client.post('/api/admin/clouds/test',json={}).status_code,403)
        self.assertEqual(self.post('/api/admin/clouds/999/test',{}).status_code,404)
        self.db.execute("UPDATE users SET role='manager' WHERE id=1")
        self.db.commit()
        self.assertEqual(self.post('/api/admin/clouds/test',{}).status_code,403)

    def test_google_oauth_detects_capacity_after_consent(self):
        from urllib.parse import urlparse,parse_qs
        from google_connect import DRIVE_SCOPE
        start=self.google_start(auto_capacity=True,capacity=None)
        self.assertEqual(start.status_code,200,start.json)
        query=parse_qs(urlparse(start.json['authorization_url']).query)
        token=unittest.mock.Mock(status_code=200)
        token.json.return_value={'access_token':'access','refresh_token':'refresh','scope':DRIVE_SCOPE}
        with patch('google_connect.requests.post',return_value=token), patch('providers.Provider.check',return_value={'ok':True,'account_id':'identity','available':987654}):
            result=self.post('/api/admin/google/complete',{'state':query['state'][0],'code':'code'})
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(self.db.execute('SELECT capacity FROM clouds').fetchone()[0],987654)

    def test_google_oauth_success_pkce_encrypted_storage_and_replay(self):
        import base64,hashlib
        from urllib.parse import urlparse,parse_qs
        from google_connect import DRIVE_SCOPE
        start=self.google_start()
        self.assertEqual(start.status_code,200,start.json)
        query=parse_qs(urlparse(start.json['authorization_url']).query)
        self.assertEqual(query['redirect_uri'],['http://localhost/auth/google/callback'])
        self.assertEqual(query['scope'],[DRIVE_SCOPE])
        self.assertNotIn('client-secret',start.get_data(as_text=True))
        # Callback bridge must not replace a Strict-cookie session when Google sends no cookie.
        callback=self.app.test_client().get('/auth/google/callback?state=test&code=secret-code')
        self.assertEqual(callback.status_code,200)
        self.assertNotIn('Set-Cookie',callback.headers)
        token_response=unittest.mock.Mock(status_code=200)
        token_response.json.return_value={'access_token':'secret-access','refresh_token':'secret-refresh','scope':DRIVE_SCOPE}
        with patch('google_connect.requests.post',return_value=token_response) as exchange, patch('providers.Provider.check',return_value={'ok':True,'account':'test@example.com','account_id':'google-user-1'}):
            result=self.post('/api/admin/google/complete',{'state':query['state'][0],'code':'secret-code'})
        self.assertEqual(result.status_code,200,result.json)
        sent=exchange.call_args.kwargs['data']
        challenge=base64.urlsafe_b64encode(hashlib.sha256(sent['code_verifier'].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(challenge,query['code_challenge'][0])
        self.assertFalse(exchange.call_args.kwargs['allow_redirects'])
        row=self.db.execute('SELECT * FROM clouds').fetchone()
        self.assertNotIn('secret-access',row['credentials'])
        self.assertNotIn('secret-refresh',result.get_data(as_text=True))
        self.assertEqual(self.db.execute('SELECT count(*) FROM google_pending').fetchone()[0],0)
        self.assertEqual(self.post('/api/admin/google/complete',{'state':query['state'][0],'code':'secret-code'}).status_code,400)

    def test_google_rejects_expired_wrong_browser_and_remote_desktop(self):
        import time
        from urllib.parse import urlparse,parse_qs
        start=self.google_start()
        state=parse_qs(urlparse(start.json['authorization_url']).query)['state'][0]
        with self.client.session_transaction() as browser:
            browser['login']='another-session'
        self.db.execute('INSERT INTO sessions VALUES(?,?,?)',('another-session',1,time.time()+1000))
        self.db.commit()
        with patch('google_connect.requests.post') as exchange:
            self.assertEqual(self.post('/api/admin/google/complete',{'state':state,'code':'code'}).status_code,400)
            exchange.assert_not_called()
        with self.client.session_transaction() as browser:
            browser['login']='session'
        self.db.execute('UPDATE google_pending SET expires=0')
        self.db.commit()
        self.assertEqual(self.post('/api/admin/google/complete',{'state':state,'code':'code'}).status_code,400)
        self.app.config['PUBLIC_URL']='https://cso.example.com'
        response=self.post('/api/admin/google/connect',{'name':'Remote','capacity':100,'client_config':{'installed':{'client_id':'id','client_secret':'secret'}}})
        self.assertEqual(response.status_code,400)
        self.assertIn('Desktop',response.json['error'])

    def test_google_denied_callback_consumes_state_without_remote_call(self):
        from urllib.parse import urlparse,parse_qs
        start=self.google_start()
        state=parse_qs(urlparse(start.json['authorization_url']).query)['state'][0]
        with patch('google_connect.requests.post') as exchange:
            result=self.post('/api/admin/google/complete',{'state':state,'denied':True})
            exchange.assert_not_called()
        self.assertEqual(result.status_code,400)
        self.assertEqual(self.db.execute('SELECT count(*) FROM google_pending').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM clouds').fetchone()[0],0)

    def test_google_client_id_cannot_be_used_as_access_token(self):
        self.admin()
        data={'name':'Bad Google','capacity':1000,'provider':'google','credentials':{'access_token':'123.apps.googleusercontent.com'}}
        with patch('providers.requests.request') as remote:
            self.assertEqual(self.post('/api/admin/clouds',data).status_code,400)
            response=self.post('/api/admin/clouds/test',data)
            self.assertEqual(response.status_code,400)
            self.assertIn('OAuth client ID',response.json['error'])
            remote.assert_not_called()
        self.assertEqual(self.db.execute('SELECT count(*) FROM clouds').fetchone()[0],0)

    def test_google_reauthorization_repairs_existing_cloud_without_duplicate(self):
        from urllib.parse import urlparse,parse_qs
        from google_connect import DRIVE_SCOPE
        self.admin()
        cid=self.cloud()
        cipher=Fernet(Path(self.temp.name+'/encryption.key').read_bytes())
        old=cipher.encrypt(json.dumps({'access_token':'123.apps.googleusercontent.com'}).encode()).decode()
        self.db.execute("UPDATE clouds SET provider='google',credentials=? WHERE id=?",(old,cid))
        self.db.commit()
        key=self.space()
        self.db.execute('UPDATE spaces SET cloud_id=?',(cid,));self.db.commit()
        response=self.post('/api/admin/google/connect',{'cloud_id':cid,'client_config':{'installed':{'client_id':'client-id','client_secret':'secret'}}})
        self.assertEqual(response.status_code,200,response.json)
        state=parse_qs(urlparse(response.json['authorization_url']).query)['state'][0]
        token=unittest.mock.Mock(status_code=200)
        token.json.return_value={'access_token':'new-token','refresh_token':'new-refresh','scope':DRIVE_SCOPE}
        with patch('google_connect.requests.post',return_value=token), patch('providers.Provider.check',return_value={'ok':True,'account_id':'identity'}):
            result=self.post('/api/admin/google/complete',{'state':state,'code':'code'})
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['id'],cid)
        self.assertEqual(self.db.execute('SELECT count(*) FROM clouds').fetchone()[0],1)
        cloud=self.db.execute('SELECT * FROM clouds').fetchone()
        self.assertEqual(cloud['capacity'],15000000000)
        credentials=json.loads(cipher.decrypt(cloud['credentials'].encode()))
        self.assertEqual(credentials['refresh_token'],'new-refresh')
        self.assertEqual(credentials['_google_account_id'],'identity')
        self.assertEqual(self.db.execute('SELECT cloud_id FROM spaces').fetchone()[0],cid)
        self.assertEqual(self.client.get('/api/v1/storage',headers={'X-API-Key':key}).status_code,200)

    def test_google_reauthorization_rejects_account_switch(self):
        from urllib.parse import urlparse,parse_qs
        from google_connect import DRIVE_SCOPE
        self.admin()
        cid=self.cloud()
        cipher=Fernet(Path(self.temp.name+'/encryption.key').read_bytes())
        old=cipher.encrypt(json.dumps({'access_token':'old-token','_google_account_id':'original-user'}).encode()).decode()
        self.db.execute("UPDATE clouds SET provider='google',credentials=? WHERE id=?",(old,cid));self.db.commit()
        start=self.post('/api/admin/google/connect',{'cloud_id':cid,'client_config':{'installed':{'client_id':'client-id','client_secret':'secret'}}})
        state=parse_qs(urlparse(start.json['authorization_url']).query)['state'][0]
        token=unittest.mock.Mock(status_code=200)
        token.json.return_value={'access_token':'new-token','refresh_token':'refresh','scope':DRIVE_SCOPE}
        with patch('google_connect.requests.post',return_value=token), patch('providers.Provider.check',return_value={'ok':True,'account_id':'another-user'}):
            response=self.post('/api/admin/google/complete',{'state':state,'code':'code'})
        self.assertEqual(response.status_code,400,response.json)
        self.assertIn('same Google account',response.json['error'])
        self.assertEqual(self.db.execute('SELECT credentials FROM clouds').fetchone()[0],old)

    def test_outhman_outage_fails_closed(self):
        import requests
        self.admin()
        with self.client.session_transaction() as s:
            s.pop('login')
        with patch('app.requests.post',side_effect=requests.Timeout):
            r=self.post('/auth/login',{'username':'admin','password':'password','code':'012345'})
        self.assertEqual(r.status_code,400)
        self.assertEqual(self.client.get('/api/admin/state').status_code,401)

if __name__=='__main__':
    unittest.main()
