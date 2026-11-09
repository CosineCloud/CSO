"""Bounded cooperative scheduler for this single-process server."""
import threading
from collections import deque


class JobScheduler:
    def __init__(self, operations, limit=1, workers=4):
        self.operations=operations
        self.limit=limit
        self.workers=workers
        self.condition=threading.Condition(threading.RLock())
        self.queue=deque()
        self.jobs={}
        self.active=0
        self.threads=[]
        self.closed=False
        self.local=threading.local()

    def set_limit(self,value):
        with self.condition:
            self.limit=value
            self.condition.notify_all()

    def register(self,oid,pausable=True):
        job={'id':oid,'paused':False,'active':False,'message':'Waiting for an available worker.'}
        if oid is not None:
            if pausable:
                self.jobs[oid]=job
            self.operations.update(oid,status='queued',can_pause=int(pausable),pause_requested=0,message=job['message'])
        return job

    def submit(self,oid,function):
        with self.condition:
            if self.closed:
                raise RuntimeError('Scheduler closed')
            job=self.register(oid)
            self.queue.append((job,function))
            if not self.threads:
                for index in range(self.workers):
                    thread=threading.Thread(target=self.worker,daemon=True,name=f'cso-worker-{index}')
                    self.threads.append(thread)
                    thread.start()
            self.condition.notify_all()

    def worker(self):
        while True:
            with self.condition:
                while not self.queue and not self.closed:
                    self.condition.wait()
                if self.closed:
                    return
                job,function=self.queue.popleft()
            try:
                self.execute(job,function)
            except Exception:
                if job['id'] is not None:
                    self.operations.finish(job['id'],False,'Worker interrupted. Check operation state before retrying.')

    def execute(self,job,function):
        self.local.job=job
        try:
            self.checkpoint()
            return function()
        finally:
            with self.condition:
                if job['active']:
                    self.active-=1
                if job['id'] is not None:
                    self.jobs.pop(job['id'],None)
                    self.operations.update(job['id'],can_pause=0,pause_requested=0)
                self.condition.notify_all()
            self.local.job=None

    def run(self,oid,function):
        if getattr(self.local,'job',None):
            return function()
        with self.condition:
            job=self.register(oid,pausable=False)
        return self.execute(job,function)

    def checkpoint(self):
        job=getattr(self.local,'job',None)
        if job is None:
            return
        with self.condition:
            if job['active'] and job['paused']:
                self.active-=1;job['active']=False
                self.condition.notify_all()
            while not job['active']:
                if self.closed:
                    raise RuntimeError('Scheduler closed')
                if job['paused']:
                    self.operations.update(job['id'],status='paused',message='Paused at a safe checkpoint.')
                elif self.active<self.limit:
                    self.active+=1;job['active']=True
                    if job['id'] is not None:
                        self.operations.update(job['id'],status='running',pause_requested=0,message='Operation is running.')
                    break
                elif job['id'] is not None:
                    self.operations.update(job['id'],status='queued',message='Waiting for an available worker.')
                self.condition.wait()

    def pause(self,oid):
        with self.condition:
            job=self.jobs.get(oid)
            if job is None:
                return False
            job['paused']=True
            self.operations.update(oid,pause_requested=1,status='running' if job['active'] else 'paused',
                message='Pause requested; finishing the current safe step.' if job['active'] else 'Paused before work starts.')
            self.condition.notify_all()
            return True

    def resume(self,oid):
        with self.condition:
            job=self.jobs.get(oid)
            if job is None:
                return False
            job['paused']=False
            self.operations.update(oid,pause_requested=0,status='running' if job['active'] else 'queued',message='Resume requested; waiting for an available worker.')
            self.condition.notify_all()
            return True

    def close(self):
        with self.condition:
            self.closed=True
            self.condition.notify_all()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
