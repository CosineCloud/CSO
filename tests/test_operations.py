import tempfile
import unittest
from pathlib import Path
from operation_log import OperationLog


class OperationLogTests(unittest.TestCase):
    def test_history_progress_duplicate_jobs_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'operations.sqlite3'
            log=OperationLog(path)
            first=log.start('delete_space','Space #1','admin')
            self.assertIsNone(log.start('delete_space','Space #1','admin'))
            log.update(first,total=3,completed=1,message='One file deleted.')
            log.finish(first,False,'Provider rejected deletion.')
            second=log.start('delete_space','Space #1','admin')
            log=OperationLog(path)
            rows=log.list()['operations']
            self.assertEqual(rows[0]['id'],second)
            self.assertEqual(rows[0]['status'],'interrupted')
            self.assertEqual(rows[1]['completed'],1)
            self.assertEqual(rows[1]['status'],'failed')
            self.assertTrue(log.list(limit=1)['has_more'])
            third=log.start('upload','file','admin')
            log.update(third,total=1)
            log.finish(third,True,'Done.')
            self.assertEqual(log.list()['operations'][0]['completed'],1)

    def test_filters_apply_before_pagination_and_search_is_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=OperationLog(Path(tmp)/'ops.sqlite3')
            first=log.start('upload','report 100%','alice')
            log.finish(first,True,'done')
            second=log.start('delete_space','Test','bob')
            log.finish(second,False,'retention denied')
            self.assertEqual(log.list(status='failed')['operations'][0]['id'],second)
            self.assertEqual(log.list(action='upload',query='alice')['operations'][0]['id'],first)
            self.assertEqual(len(log.list(query='%')['operations']),1)
            self.assertEqual(log.list(query='retention')['operations'][0]['id'],second)
            self.assertFalse(log.list(status='completed',limit=1)['has_more'])

    def test_event_history_incremental_pagination_dedup_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ops.sqlite3'
            log=OperationLog(path)
            oid=log.start('admin_upload','file','admin')
            log.update(oid,total=42,completed=7,message='Uploaded part 7.')
            log.update(oid,total=42,completed=7,message='Uploaded part 7.')
            detail=log.detail(oid,limit=1)
            self.assertEqual(len(detail['events']),1)
            self.assertTrue(detail['has_more'])
            cursor=detail['events'][0]['id']
            update=log.detail(oid,after=cursor)
            self.assertEqual(len(update['events']),1)
            self.assertEqual(update['events'][0]['completed'],7)
            self.assertEqual(update['operation']['total'],42)
            log.finish(oid,True,'Upload complete.')
            finished=log.detail(oid,after=update['events'][0]['id'])
            self.assertEqual(finished['events'][0]['status'],'completed')
            self.assertEqual(finished['operation']['completed'],42)
            self.assertIsNone(log.detail(99999))
            waiting=log.start('upload','other','admin')
            log=OperationLog(path)
            self.assertEqual(log.detail(waiting)['events'][-1]['status'],'interrupted')
            self.assertEqual(log.detail(oid)['operation']['status'],'completed')

    def test_late_pause_cannot_resurrect_completed_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=OperationLog(Path(tmp)/'ops.sqlite3')
            oid=log.start('upload','file','admin')
            log.finish(oid,True,'done')
            log.update(oid,status='paused',message='late pause')
            self.assertEqual(log.detail(oid)['operation']['status'],'completed')
