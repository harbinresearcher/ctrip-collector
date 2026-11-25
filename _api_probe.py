"""Probe /api/start with malformed payloads; any 5xx or connection drop is a bug."""
import json, sys, tempfile, threading, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import logging
logging.disable(logging.CRITICAL)

from workbench_server import create_server

tmp = tempfile.mkdtemp()
server = create_server(tmp, adb_path=None, port=0)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{port}'

CASES = {
    'no body keys': {},
    'unknown mode': {'mode': 'nope'},
    'mode null': {'mode': None},
    'mode list': {'mode': ['reviews']},
    'urls null': {'mode': 'reviews', 'urls': None},
    'urls string': {'mode': 'reviews', 'urls': 'https://you.ctrip.com/sight/a/1.html'},
    'urls huge': {'mode': 'reviews', 'urls': ['https://you.ctrip.com/sight/a/1.html'] * 500},
    'limit text': {'mode': 'reviews', 'urls': ['https://you.ctrip.com/sight/a1/1.html'], 'limit': 'many'},
    'limit negative': {'mode': 'reviews', 'urls': ['https://you.ctrip.com/sight/a1/1.html'], 'limit': -5},
    'limit float': {'mode': 'reviews', 'urls': ['https://you.ctrip.com/sight/a1/1.html'], 'limit': 1.5},
    'limit bool': {'mode': 'reviews', 'urls': ['https://you.ctrip.com/sight/a1/1.html'], 'limit': True},
    'refresh string': {'mode': 'reviews', 'urls': ['https://you.ctrip.com/sight/a1/1.html'], 'refresh': 'yes'},
    'app no adb': {'mode': 'app_reviews', 'name': 'x', 'kind': 'hotel'},
    'app kind bad': {'mode': 'app_reviews', 'name': 'x', 'kind': 'plane'},
    'app name long': {'mode': 'app_reviews', 'name': 'x' * 500, 'kind': 'hotel'},
    'app name int': {'mode': 'app_reviews', 'name': 42, 'kind': 'hotel'},
    'category bad': {'mode': 'app_gallery', 'name': 'x', 'category': 'nope'},
    'media limit 0': {'mode': 'media', 'source_file': 'x.jsonl', 'media_limit': 0},
    'capture path': {'mode': 'capture_import', 'capture_file': '../../etc/passwd'},
    'coverage': {'mode': 'coverage'},
    'public profile evil': {'mode': 'public_poi_profile', 'urls': ['https://evil.com/sight/a/1.html']},
    'public profile ftp': {'mode': 'public_poi_profile', 'urls': ['ftp://you.ctrip.com/sight/a/1.html']},
    'discovery kind bad': {'mode': 'auto_discovery', 'kind': 'plane'},
    'regions negative': {'mode': 'reviews', 'urls': ['https://you.ctrip.com/sight/a1/1.html'], 'max_regions': -1},
    'unicode name': {'mode': 'app_reviews', 'name': '😀' * 60, 'kind': 'hotel'},
    'nested json': {'mode': {'a': 1}},
}

def post(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(base + '/api/start', data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()[:120].decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:120].decode('utf-8', 'replace')
    except Exception as e:
        return 'EXC', f'{type(e).__name__}: {e}'

bad = []
for name, payload in CASES.items():
    status, body = post(payload)
    if status not in (200, 400, 409):
        bad.append((name, status, body))
    print(f'{status}\t{name}')

# Non-JSON and wrong content type
for label, raw, ctype in [('not json', b'{oops', 'application/json'),
                          ('wrong ctype', b'{}', 'text/plain'),
                          ('empty', b'', 'application/json')]:
    req = urllib.request.Request(base + '/api/start', data=raw, headers={'Content-Type': ctype})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f'{r.status}\t{label}')
    except urllib.error.HTTPError as e:
        print(f'{e.code}\t{label}')
        if e.code >= 500:
            bad.append((label, e.code, ''))
    except Exception as e:
        print(f'EXC\t{label}: {e}')
        bad.append((label, 'EXC', str(e)))

server.shutdown()
print('\nUNEXPECTED:', bad)