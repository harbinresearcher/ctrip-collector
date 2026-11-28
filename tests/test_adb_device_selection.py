import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from runner import Adb, AndroidReadinessError
from workbench_jobs import make_job


class AdbDeviceSelectionTests(unittest.TestCase):
    def test_managed_device_sessions_share_the_lease_for_the_selected_serial(self):
        serial = 'emulator-55697'

        def reply(*args):
            if args == ('devices',):
                return f'List of devices attached\n{serial}\tdevice\n'
            return 'package:ctrip.android.view\n'

        with TemporaryDirectory() as state, patch.dict('os.environ', LOCALAPPDATA=state), \
                patch.object(Adb, 'run', side_effect=reply):
            with Adb('adb') as collecting, Adb('different-adb', serial=serial) as duplicate:
                self.assertEqual(collecting.connect(), serial)
                self.assertEqual(collecting.connect(), serial)
                with self.assertRaises(AndroidReadinessError) as busy:
                    duplicate.connect()
                self.assertEqual(busy.exception.state, 'device_busy')
                self.assertEqual(Adb('adb', serial=serial).connect(), serial)
            with Adb('adb', serial=serial) as next_task:
                self.assertEqual(next_task.connect(), serial)

    def test_device_lease_survives_until_fast_service_cleanup_finishes(self):
        serial = 'emulator-55698'

        def reply(*args):
            if args == ('devices',):
                return f'List of devices attached\n{serial}\tdevice\n'
            return 'package:ctrip.android.view\n'

        with TemporaryDirectory() as state, patch.dict('os.environ', LOCALAPPDATA=state), \
                patch.object(Adb, 'run', side_effect=reply):
            with Adb('adb', serial=serial) as collecting, Adb('adb', serial=serial) as duplicate:
                collecting.connect()
                states = []

                def shutdown():
                    try:
                        duplicate.connect()
                    except AndroidReadinessError as busy:
                        states.append(busy.state)
                    raise RuntimeError('模拟读取服务退出失败')

                collecting._ui = Mock()
                collecting._ui.stop_uiautomator.side_effect = shutdown
                collecting.close()
                self.assertEqual(states, ['device_busy'])
                self.assertEqual(duplicate.connect(), serial)

    def test_explicit_device_is_selected_while_another_device_is_present(self):
        device = Adb('adb', serial='emulator-5554')
        with patch.object(device, 'run', side_effect=[
            'List of devices attached\nphone\tdevice\nemulator-5554\tdevice\n',
            'package:ctrip.android.view\n',
        ]) as run:
            self.assertEqual(device.connect(), 'emulator-5554')
            self.assertEqual(run.call_args.args, ('shell', 'pm', 'list', 'packages', 'ctrip.android.view'))
        with patch.object(device, 'run', return_value='List of devices attached\nphone\tdevice\nemulator-5554\toffline\n') as run:
            with self.assertRaisesRegex(RuntimeError, 'emulator-5554'):
                device.connect()
            run.assert_called_once()

    def test_server_configuration_is_used_by_app_snapshot_and_reviews(self):
        with TemporaryDirectory() as folder, patch('workbench_jobs.Adb') as snapshot_adb, \
             patch('workbench_app_jobs.Adb') as reviews_adb, \
             patch('workbench_jobs.capture_hotel_snapshot', return_value={}), \
             patch('workbench_app_jobs.collect', return_value={}):
            for mode, factory in [('app_hotel', snapshot_adb), ('app_reviews', reviews_adb)]:
                job = make_job({'mode': mode, 'name': '酒店', 'resource_id': '1',
                                'adb_serial': 'unexpected-phone'},
                               Path(folder), 'adb', adb_serial='emulator-5554')
                job(Event(), lambda _: None)
                factory.assert_called_once_with('adb', serial='emulator-5554', ui_backend='uiautomator2')
