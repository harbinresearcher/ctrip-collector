import http.client
import json
import subprocess
from contextlib import closing
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from workbench_server import create_server


class AndroidReadinessTests(unittest.TestCase):
    def check_devices(self, replies=(), *, adb='adb', serial=None, origin=None):
        with TemporaryDirectory() as folder, patch('runner.subprocess.run', side_effect=replies) as run:
            server = create_server(folder, adb, port=0, adb_serial=serial)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with closing(http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)) as conn:
                    conn.request('GET', '/api/android-readiness', headers={'Origin': origin} if origin else {})
                    response = conn.getresponse()
                    payload = json.loads(response.read())
                    return response.status, payload, [call.args[0] for call in run.call_args_list]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    @staticmethod
    def adb_output(stdout):
        return subprocess.CompletedProcess([], 0, stdout, '')

    def test_installed_foreground_app_uses_configured_emulator_without_querying_phone(self):
        reply = self.adb_output
        status, payload, commands = self.check_devices([
            reply('List of devices attached\nphone-private-id\tdevice\nemulator-5554\tdevice\n'),
            reply('package:ctrip.android.view\n'),
            reply('  mCurrentFocus=Window{aa u0 ctrip.android.view/.Page}\n'),
        ], serial='emulator-5554')
        self.assertEqual((status, payload), (200, {'state': 'foreground', 'device': 'emulator'}))
        self.assertEqual(commands, [['adb', 'devices'],
                                    ['adb', '-s', 'emulator-5554', 'shell', 'pm', 'list', 'packages', 'ctrip.android.view'],
                                    ['adb', '-s', 'emulator-5554', 'shell', 'dumpsys', 'window']])

    def test_multi_device_and_selected_offline_stop_before_inspecting_any_app(self):
        reply = self.adb_output
        for serial, listing, expected in [
            (None, '', 'disconnected'),
            (None, 'emulator-5554\tdevice\nphone\tdevice\n', 'ambiguous'),
            ('emulator-5554', 'emulator-5554\toffline\nphone\tdevice\n', 'offline'),
            ('emulator-5554', 'phone\tdevice\n', 'disconnected'),
            (None, 'phone\tunauthorized\n', 'unauthorized'),
        ]:
            with self.subTest(expected=expected):
                status, payload, commands = self.check_devices([reply('List of devices attached\n' + listing)], serial=serial)
                self.assertEqual((status, payload['state']), (200, expected))
                self.assertEqual(commands, [['adb', 'devices']])

    def test_missing_app_background_and_unknown_focus_are_distinct(self):
        reply = self.adb_output
        for tail, expected in [
            ([reply('')], 'not_installed'),
            ([reply('package:ctrip.android.view\n'), reply('  mCurrentFocus=Window{aa u0 com.other/.Page}\n')], 'background'),
            ([reply('package:ctrip.android.view\n'), reply('  mCurrentFocus=null\n')], 'focus_unknown'),
        ]:
            with self.subTest(expected=expected):
                status, payload, _ = self.check_devices([reply('List of devices attached\nphone\tdevice\n'), *tail])
                self.assertEqual((status, payload), (200, {'state': expected, 'device': 'android'}))

    def test_no_configuration_origin_and_failed_command(self):
        status, payload, commands = self.check_devices(adb=None)
        self.assertEqual((status, payload['state'], commands), (200, 'not_configured', []))
        status, _, commands = self.check_devices(origin='https://example.com')
        self.assertEqual((status, commands), (403, []))
        status, payload, _ = self.check_devices([subprocess.TimeoutExpired(['private-tool-path'], 1)], serial='emulator-5554')
        self.assertEqual((status, payload), (200, {'state': 'check_failed', 'device': 'emulator'}))
