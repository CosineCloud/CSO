import tempfile
import threading
import time
import unittest
from pathlib import Path
from job_scheduler import JobScheduler
from operation_log import OperationLog


class SchedulerTests(unittest.TestCase):
    def wait_status(self,log,oid,status):
        for _ in range(200):
            row=log.detail(oid)['operation']
            if row['status']==status:return row
            time.sleep(.01)
        self.fail(f'Operation never became {status}: {row}')

    def test_limit_queue_pause_resume_and_dynamic_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=OperationLog(Path(tmp)/'ops.sqlite3')
            scheduler=JobScheduler(log,limit=1,workers=2)
            a=log.start('upload','a','admin');b=log.start('upload','b','admin')
            entered=threading.Event();continue_a=threading.Event();advanced=threading.Event();b_entered=threading.Event();release_b=threading.Event();done_a=threading.Event()
            def job_a():
                entered.set()
                continue_a.wait(3)
                scheduler.checkpoint()
                advanced.set()
                log.finish(a,True,'done');done_a.set()
            def job_b():
                b_entered.set();release_b.wait(3);log.finish(b,True,'done')
            try:
                scheduler.submit(a,job_a);self.assertTrue(entered.wait(2))
                scheduler.submit(b,job_b)
                self.wait_status(log,b,'queued')
                self.assertFalse(b_entered.is_set())
                self.assertTrue(scheduler.pause(a))
                continue_a.set()
                self.wait_status(log,a,'paused')
                self.assertFalse(advanced.is_set())
                self.assertTrue(b_entered.wait(2))
                scheduler.set_limit(2)
                self.assertTrue(scheduler.resume(a))
                self.assertTrue(done_a.wait(2))
                release_b.set()
                self.wait_status(log,b,'completed')
                self.assertFalse(scheduler.pause(9999))
            finally:
                continue_a.set();release_b.set();scheduler.close()
