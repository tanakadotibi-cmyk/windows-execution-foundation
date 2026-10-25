import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src.windows_execution_foundation import cityheaven_daily_metrics as d


class DailyAuthRecovery(unittest.TestCase):
    def result(self, code=0, error=''):
        return subprocess.CompletedProcess(['reader'],code,'{}',error)

    def test_auth_failure_recovers_once_then_reads_same_command(self):
        command=['reader','2026-10-12']
        recovery={'status':'AUTHENTICATED','submit_count':1,'logout_notice_ack_count':1,'owned_target_closed':True}
        with patch.object(d.subprocess,'run',side_effect=[self.result(20,'AUTH_REQUIRED'),self.result()]) as run,patch.object(d,'recover_saved_login',return_value=recovery) as auth:
            result,details=d.run_reader_with_auth_recovery(command,{},d.RESERVATION_CHROME_REUSE_ROUTE)
        self.assertEqual(result.returncode,0);self.assertEqual(details,recovery)
        auth.assert_called_once();self.assertEqual([call.args[0] for call in run.call_args_list],[command,command])

    def test_login_rejected_or_unknown_is_never_retried(self):
        with patch.object(d.subprocess,'run',return_value=self.result(20,'AUTH_REQUIRED')) as run,patch.object(d,'recover_saved_login',return_value={'status':'OWNER_AUTH_REQUIRED','submit_count':1}) as auth:
            result,_=d.run_reader_with_auth_recovery(['reader'],{},d.RESERVATION_CHROME_REUSE_ROUTE)
        self.assertEqual(result.returncode,20);run.assert_called_once();auth.assert_called_once()

    def test_second_auth_failure_never_recovers_again(self):
        with patch.object(d.subprocess,'run',return_value=self.result(20,'AUTH_REQUIRED')) as run,patch.object(d,'recover_saved_login',return_value={'status':'AUTHENTICATED','owned_target_closed':True}) as auth:
            result,_=d.run_reader_with_auth_recovery(['reader'],{},d.RESERVATION_CHROME_REUSE_ROUTE)
        self.assertEqual(result.returncode,20);self.assertEqual(run.call_count,2);auth.assert_called_once()

    def test_access_denial_or_schema_failure_does_not_login(self):
        for error in ['SOURCE_MISSING','SCHEMA_DRIFT','BROWSER_UNAVAILABLE']:
            with self.subTest(error=error),patch.object(d.subprocess,'run',return_value=self.result(20,error)) as run,patch.object(d,'recover_saved_login') as auth:
                d.run_reader_with_auth_recovery(['reader'],{},d.RESERVATION_CHROME_REUSE_ROUTE)
                run.assert_called_once();auth.assert_not_called()

    def test_authenticated_reader_never_calls_login(self):
        with patch.object(d.subprocess,'run',return_value=self.result()) as run,patch.object(d,'recover_saved_login') as auth:
            d.run_reader_with_auth_recovery(['reader'],{},d.RESERVATION_CHROME_REUSE_ROUTE)
            run.assert_called_once();auth.assert_not_called()

    def test_expired_auth_receipt_can_retry_and_retains_prior(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); receipt=root/'receipts/20261012.json';receipt.parent.mkdir()
            before={'schema_version':d.SCHEMA,'target_date':'2026-10-12','status':'FAILED','failure_code':'AUTH_REQUIRED','source_payload_persisted':False}
            receipt.write_text(json.dumps(before),encoding='utf-8')
            with patch.object(d,'DEFAULT_RUNTIME',root),patch.object(d,'_read_saved_source',return_value=None):
                def fetch(day):
                    self.assertEqual(day,'2026-10-12');raise d.CollectorError('AUTH_REQUIRED')
                result=d.run('2026-10-12',write_airtable=False,runtime=root,fetcher=fetch)
            self.assertEqual(result['cityheaven_fetch_count'],1)
            history=list((receipt.parent/'history'/receipt.stem).glob('*.json'))
            self.assertTrue(any(json.loads(item.read_text())==before for item in history))
            self.assertEqual(json.loads(receipt.read_text())['cityheaven_fetch_count'],1)


if __name__=='__main__':unittest.main()
