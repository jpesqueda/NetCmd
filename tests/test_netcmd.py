"""Unit tests for NetCmd 1.2.1 without live network devices."""
from __future__ import annotations
import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, main, mock
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netcmd


class FakeConn:
    def __init__(self, chunks=None):
        self.chunks = list(chunks or [])
        self.sent = []
        self.closed = False
    def find_prompt(self): return 'SW01#'
    def write_channel(self, data): self.sent.append(data)
    def read_channel(self): return self.chunks.pop(0) if self.chunks else ''
    def is_alive(self): return True
    def check_enable_mode(self): return True
    def disconnect(self): self.closed = True


def args(**overrides):
    values = dict(mode='exec', on_error='continue', timeout=5., test_connection=False,
                  retries=0, port=22, conn_timeout=5, device_type='cisco_ios',
                  secret=None, ssh_config=None, known_hosts=None, strict_host_key=False)
    values.update(overrides)
    return SimpleNamespace(**values)


class NetCmdTests(TestCase):
    def test_session_output_removes_duplicate_echo_and_prompt(self):
        raw = 'SW01#show version\r\nCisco IOS XE Software\r\nSW01#'
        self.assertEqual(netcmd.session_transcript('SW01#', 'show version', raw),
                         'SW01#show version\nCisco IOS XE Software\n')

    def test_session_output_keeps_unusual_echo(self):
        self.assertEqual(netcmd.session_transcript('NEXUS(config)#', 'feature ospf',
                                                        'feature ospf\nCompleted\nNEXUS(config)#'),
                         'NEXUS(config)#feature ospf\nCompleted\n')

    def test_error_categories(self):
        cases = [('NetmikoAuthenticationException', 'Authentication failed', 'AUTHENTICATION FAILED'),
                 ('gaierror', '[Errno 11001] getaddrinfo failed', 'HOST UNKNOWN'),
                 ('NetmikoTimeoutException', 'TCP connection failed', 'SSH CONNECTION FAILED'),
                 ('BadHostKeyException', 'Host key mismatch', 'SSH HOST KEY ERROR')]
        for name, msg, expected in cases:
            ex = type(name, (Exception,), {})(msg)
            self.assertEqual(netcmd.classify_connection_error(ex)[0], expected)

    def test_worker_auth_failure_produces_no_text_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/'SW01.txt'
            ex = type('NetmikoAuthenticationException',(Exception,),{})('Authentication failed')
            logger = mock.MagicMock()
            with mock.patch.object(netcmd, 'detect_and_connect', side_effect=ex):
                result = netcmd.device_worker(netcmd.DeviceJob('SW01', None, [netcmd.CommandStep('show version',1)]), args(), logger, path, 'user', 'pw')
            self.assertFalse(path.exists())
            self.assertEqual(result.connection_error, 'AUTHENTICATION FAILED')
            self.assertEqual(result.status, 'FAILED')

    def test_worker_cli_output_contains_only_command_and_output(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/'SW01.txt'
            conn = FakeConn(['SW01#show version\r\nIOS-XE 17.12\r\nSW01#',
                             'SW01#show module\r\nModule OK\r\nSW01#'])
            job = netcmd.DeviceJob('SW01','cisco_ios', [netcmd.CommandStep('show version',1),netcmd.CommandStep('show module',2)])
            with mock.patch.object(netcmd, 'detect_and_connect', return_value=(conn, 'cisco_ios')):
                result = netcmd.device_worker(job, args(), mock.MagicMock(), path, 'user','pw')
            self.assertEqual(result.status,'PASS')
            self.assertEqual(path.read_text(), 'SW01#show version\nIOS-XE 17.12\n\nSW01#show module\nModule OK\n\n')

    def test_test_connection_no_txt_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/'SW01.txt'
            with mock.patch.object(netcmd, 'detect_and_connect', return_value=(FakeConn(),'cisco_ios')):
                result = netcmd.device_worker(netcmd.DeviceJob('SW01',None,[]), args(test_connection=True), mock.MagicMock(), path,'u','p')
            self.assertEqual(result.status,'PASS')
            self.assertFalse(path.exists())

    def test_same_commands_multifile(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/'mf.txt'
            path.write_text('HostName:SW1\nshow version\n!\nHostName:SW2\nshow module\n!\n')
            jobs = netcmd.load_multifile(str(path))
            self.assertEqual(len(jobs),2)
            self.assertEqual(jobs[0].steps[0].command,'show version')

    def test_test_connection_without_cmd_file(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'hosts.txt'
            path.write_text('SW1\nSW2\n')
            jobs=netcmd.load_standard(None,str(path),None,require_commands=False)
            self.assertEqual(len(jobs),2)
            self.assertEqual(jobs[0].steps,[])

    def test_interactive_wizard_prompt(self):
        with mock.patch('builtins.input', side_effect=['SW01','commands.txt','admin','checks']):
            with mock.patch('getpass.getpass', return_value='secret'):
                argv,pw = netcmd.interactive_options()
        self.assertEqual(pw,'secret')
        self.assertEqual(argv,['-ho','SW01','-cmd','commands.txt','-u','admin','--suffix','checks'])

    def test_end_to_end_main_success_with_mock_ssh(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            commands = root/'commands.txt'
            commands.write_text('show version\n')
            out = root/'out'
            conn = FakeConn(['SW01#show version\nIOS XE\nSW01#'])
            with mock.patch.dict(sys.modules, {'netmiko': mock.MagicMock()}):
                with mock.patch.object(netcmd, 'detect_and_connect', return_value=(conn, 'cisco_ios')):
                    with contextlib.redirect_stdout(io.StringIO()) as buf:
                        code = netcmd.main(['-ho','SW01','-cmd',str(commands),'-u','admin','-p','test', '-o',str(out)])
            self.assertEqual(code,0)
            self.assertIn('All Success',buf.getvalue())
            self.assertEqual(len(list((out/'Results').glob('*.txt'))),1)
            self.assertEqual(len(list((out/'Logs').glob('*.log'))),2)
            self.assertFalse(list(out.rglob('*.csv')))
            self.assertIn('SW01#show version\nIOS XE',next((out/'Results').glob('*.txt')).read_text())

    def test_end_to_end_connection_test_failure_no_result_dir(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            out=root/'out'
            err=type('gaierror',(Exception,),{})('getaddrinfo failed')
            with mock.patch.dict(sys.modules, {'netmiko': mock.MagicMock()}):
                with mock.patch.object(netcmd, 'detect_and_connect',side_effect=err):
                    with contextlib.redirect_stdout(io.StringIO()) as buf:
                        code=netcmd.main(['-ho','unknown.example','--test-connection','-u','admin','-p','test','-o',str(out)])
            self.assertEqual(code,1)
            self.assertIn('HOST UNKNOWN',buf.getvalue())
            self.assertIn('Some Devices Failed',buf.getvalue())
            self.assertFalse((out/'Results').exists())
            self.assertEqual(len(list((out/'Logs').glob('*.log'))),2)

    def test_status(self):
        self.assertEqual(netcmd.overall_status([netcmd.DeviceResult('SW1', 1,passed=1,status='PASS')])[0],'All Success')
        self.assertEqual(netcmd.overall_status([netcmd.DeviceResult('SW1', 1,failed=1,status='FAILED')])[0],'Some Commands Failed')
        self.assertEqual(netcmd.overall_status([netcmd.DeviceResult('SW1', 1,status='FAILED', connection_error='HOST UNKNOWN')])[0],'Some Devices Failed')


if __name__ == '__main__': main()
