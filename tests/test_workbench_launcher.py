import io
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
import workbench_server


class WorkbenchLauncherTests(unittest.TestCase):
    def test_app_passes_explicit_emulator_to_workbench(self):
        argv = ['app.py', '--workbench', '--output', 'data', '--adb', 'adb.exe',
                '--adb-serial', 'emulator-5556', '--port', '0']
        with patch('sys.argv', argv), patch.object(workbench_server, 'serve_workbench', create=True) as serve:
            app.main()
        serve.assert_called_once_with(Path('data'), 'adb.exe', 0, adb_serial='emulator-5556', open_browser=True)

    def test_app_can_launch_workbench_without_adb(self):
        with (patch('sys.argv', ['app.py', '--workbench', '--no-browser']),
              patch.object(app, 'HOME', Path('missing-app-folder')),
              patch('app.shutil.which', return_value=None),
              patch.object(workbench_server, 'serve_workbench', create=True) as serve):
            app.main()
        serve.assert_called_once_with(Path('missing-app-folder/data'), None, 8765,
                                      adb_serial=None, open_browser=False)

    def test_workbench_starts_browser_and_closes_resources_after_interrupt(self):
        server = Mock(server_port=32100)
        server.serve_forever.side_effect = KeyboardInterrupt
        with (patch.object(workbench_server, 'create_server', return_value=server),
              patch('webbrowser.open', return_value=True) as browser,
              patch('sys.stdout', new_callable=io.StringIO) as stdout):
            workbench_server.serve_workbench(Path('data'), port=0, open_browser=True)
        self.assertIn('http://127.0.0.1:32100', stdout.getvalue())
        browser.assert_called_once_with('http://127.0.0.1:32100', new=2)
        server.manager.close.assert_called_once_with()
        server.server_close.assert_called_once_with()
