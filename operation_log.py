"""Persistent operational history, separate from the file catalog's write lock."""
import sqlite3
import time
from contextlib import contextmanager


class OperationLog:
    def __init__(self, path):
        self.path = str(path)
        with self.connection() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS operations(
                id INTEGER PRIMARY KEY, action TEXT NOT NULL, resource TEXT NOT NULL,
                actor TEXT NOT NULL, status TEXT NOT NULL, completed INTEGER DEFAULT 0,
                total INTEGER, message TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)''')
            fields=[r['name'] for r in c.execute('PRAGMA table_info(operations)')]
            for name in ('can_pause','pause_requested'):
                if name not in fields:
                    c.execute(f'ALTER TABLE operations ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
            c.execute('CREATE INDEX IF NOT EXISTS operation_status ON operations(status)')
            c.execute('''CREATE TABLE IF NOT EXISTS operation_events(
                id INTEGER PRIMARY KEY,operation_id INTEGER NOT NULL,created REAL NOT NULL,
                status TEXT NOT NULL,completed INTEGER NOT NULL,total INTEGER,message TEXT NOT NULL)''')
            c.execute('CREATE INDEX IF NOT EXISTS operation_event_cursor ON operation_events(operation_id,id)')
            # Existing history has only a snapshot; do not invent prior steps.
            c.execute('''INSERT INTO operation_events(operation_id,created,status,completed,total,message)
                SELECT id,updated,status,completed,total,message FROM operations
                WHERE NOT EXISTS(SELECT 1 FROM operation_events WHERE operation_id=operations.id)''')
            interrupted=c.execute("SELECT id FROM operations WHERE status IN ('running','queued','paused')").fetchall()
            c.execute("UPDATE operations SET status='interrupted',can_pause=0,pause_requested=0,message='Server restarted before completion. Check remaining files before retrying.',updated=? WHERE status IN ('running','queued','paused')", (time.time(),))
            for row in interrupted:
                self.event(c,row['id'])

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def event(self,c,oid):
        c.execute('''INSERT INTO operation_events(operation_id,created,status,completed,total,message)
            SELECT id,updated,status,completed,total,message FROM operations WHERE id=?''',(oid,))

    def start(self, action, resource, actor):
        now = time.time()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            if action == 'delete_space' and c.execute("SELECT 1 FROM operations WHERE action=? AND resource=? AND status IN ('queued','running')", (action,resource)).fetchone():
                return None
            total=10000 if action in ('admin_upload','api_files') else None
            oid=c.execute('INSERT INTO operations(action,resource,actor,status,total,message,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                (action, resource, actor, 'running',total,'Request accepted.', now, now)).lastrowid
            self.event(c,oid)
            return oid

    def update(self, oid, *, status=None, completed=None, total=None, message=None, resource=None, can_pause=None, pause_requested=None):
        changes = {}
        for key,value in dict(status=status,completed=completed,total=total,message=message,resource=resource,can_pause=can_pause,pause_requested=pause_requested).items():
            if value is not None:
                changes[key] = value
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM operations WHERE id=?',(oid,)).fetchone()
            if not old or not any(old[key]!=value for key,value in changes.items()):
                return
            if old['status'] in ('completed','failed','interrupted') and changes.get('status') in ('running','queued','paused'):
                return
            changes['updated']=time.time()
            c.execute('UPDATE operations SET '+','.join(k+'=?' for k in changes)+' WHERE id=?', (*changes.values(), oid))
            self.event(c,oid)

    def finish(self, oid, success, message):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('''UPDATE operations SET status=?, message=?, updated=?,
                completed=CASE WHEN ? AND total IS NOT NULL THEN total ELSE completed END WHERE id=?''',
                ('completed' if success else 'failed',message,time.time(),int(success),oid))
            self.event(c,oid)

    def expire(self,c):
        rows=c.execute("SELECT id FROM operations WHERE action='admin_upload' AND status='running' AND message LIKE 'Receiving file from browser.%' AND updated<?",(time.time()-3600,)).fetchall()
        for row in rows:
            c.execute("UPDATE operations SET status='failed',message='Browser upload expired before the file reached CSO. Start a new upload.',updated=? WHERE id=?",(time.time(),row['id']))
            self.event(c,row['id'])

    def detail(self,oid,after=0,limit=200):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self.expire(c)
            operation=c.execute('SELECT * FROM operations WHERE id=?',(oid,)).fetchone()
            if not operation:
                return None
            rows=c.execute('SELECT * FROM operation_events WHERE operation_id=? AND id>? ORDER BY id LIMIT ?',
                (oid,after,limit+1)).fetchall()
            return dict(operation=dict(operation),events=[dict(r) for r in rows[:limit]],has_more=len(rows)>limit)

    def list(self, limit=50, offset=0, *, status='', query='', action=''):
        clauses=[]
        params=[]
        if status:
            clauses.append('status=?');params.append(status)
        if action:
            clauses.append('action=?');params.append(action)
        if query:
            clauses.append("(instr(lower(resource),lower(?))>0 OR instr(lower(actor),lower(?))>0 OR instr(lower(action),lower(?))>0 OR instr(lower(message),lower(?))>0)")
            params.extend([query]*4)
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self.expire(c)
            rows=c.execute('SELECT * FROM operations'+where+' ORDER BY id DESC LIMIT ? OFFSET ?',(*params,limit+1,offset)).fetchall()
        return dict(operations=[dict(r) for r in rows[:limit]],has_more=len(rows)>limit)
