"""Deterministic integration tests use a fake HTTP Ollama, not a real LLM."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from backend.core import AppError, Gazetteer, Ollama, Service, Store, normalize, validate_extraction
from backend.server import make_server

ROOT = Path(__file__).resolve().parents[1]


def observation(place='Уфа', evidence='В Уфе видели птицу.', district=None):
    return {'place': place, 'district': district, 'evidence': evidence,
            'relation': 'in', 'observed_time_text': None}


class SystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gazetteer = Gazetteer(ROOT / 'data/settlements.csv.gz')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'observations.sqlite3')
        self.output = {'observations': [observation()]}
        self.requests = []
        case = self

        class FakeOllama(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_GET(self):
                data = {'models': [{'name': 'test-local:1'}, {'name': 'remote:cloud'}, {'name': 'remote', 'remote_host': 'cloud'}]}
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                case.requests.append(payload)
                data = {'message': {'content': json.dumps(case.output)}}
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps(data).encode())

        self.fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeOllama)
        self.fake_thread = threading.Thread(target=self.fake.serve_forever, daemon=True)
        self.fake_thread.start()
        self.ollama = Ollama(f'http://127.0.0.1:{self.fake.server_port}', timeout=2)
        self.service = Service(self.gazetteer, self.store, self.ollama)
        self.server = make_server(self.service, ROOT / 'web', 'test-token', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.fake.shutdown(); self.fake.server_close(); self.fake_thread.join()
        self.temp.cleanup()

    def call(self, path, data=None, headers=None, method=None):
        request = Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                          headers=headers if headers is not None else {'Authorization': 'Bearer test-token', 'Content-Type': 'application/json'}, method=method)
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, response.headers, json.loads(raw) if raw and 'application/json' in response.headers.get('Content-Type', '') else raw

    def test_full_http_pipeline_and_persistence(self):
        status, _, report = self.call('/api/reports', {'text': 'В Уфе видели птицу.', 'model': 'test-local:1'})
        self.assertEqual(status, 201)
        self.assertEqual(report['observations'][0]['location']['lat'], 54.735598)
        self.assertEqual(report['observations'][0]['location']['lon'], 55.957605)
        self.assertTrue(report['created_at'].endswith('+00:00'))
        self.assertEqual(Store(self.store.path).list()[0]['text'], 'В Уфе видели птицу.')
        self.assertEqual(self.requests[0]['format']['type'], 'object')
        self.assertFalse(self.requests[0]['stream'])
        self.assertEqual(self.call('/api/reports')[2]['reports'][0]['id'], report['id'])

    def test_ambiguity_requires_explicit_choice(self):
        text = 'В Михайловке видели птицу.'
        self.output = {'observations': [observation('Михайловка', text)]}
        report = self.call('/api/reports', {'text': text, 'model': 'test-local:1'})[2]
        item = report['observations'][0]
        self.assertEqual(item['status'], 'ambiguous')
        self.assertIsNone(item['location'])
        self.assertEqual(len(item['candidates']), 27)
        body = {'report_id': report['id'], 'observation_id': item['id'], 'candidate_id': 'fake-id'}
        self.assertEqual(self.call('/api/resolve', body)[0], 400)
        body['candidate_id'] = item['candidates'][0]['id']
        status, _, updated = self.call('/api/resolve', body)
        self.assertEqual(status, 200)
        self.assertEqual(updated['observations'][0]['resolved_by'], 'operator')
        self.assertEqual(self.call('/api/resolve', body)[0], 409)

    def test_no_place_not_invented(self):
        self.output = {'observations': []}
        report = self.service.submit('Птица летела в сторону Уфы.', 'test-local:1')
        self.assertEqual(report['observations'], [])

    def test_missing_place_is_preserved_without_marker(self):
        text = 'В Несуществующем видели птицу.'
        self.output = {'observations': [observation('Несуществующий', text)]}
        report = self.service.submit(text, 'test-local:1')
        self.assertEqual(report['observations'][0]['status'], 'not_found')
        self.assertIsNone(report['observations'][0]['location'])

    def test_reject_invented_evidence_and_bad_types(self):
        for output in [{'observations': [observation(evidence='Выдуманная цитата')]},
                       {'observations': 'Уфа'}, {'observations': [{**observation(), 'lat': 54.7}]},
                       {'observations': [{**observation(), 'place': 22}]}]:
            with self.subTest(output=output), self.assertRaises(AppError):
                validate_extraction(output, 'В Уфе видели птицу.')

    def test_bad_ollama_json_does_not_save(self):
        self.output = {'coordinates': [1, 2]}
        self.assertEqual(self.call('/api/reports', {'text': 'В Уфе видели птицу.', 'model': 'test-local:1'})[0], 502)
        self.assertEqual(self.store.list(), [])

    def test_auth_origin_and_host(self):
        self.assertEqual(self.call('/api/reports', headers={})[0], 401)
        self.assertEqual(self.call('/api/reports', headers={'Authorization': 'Bearer test-token', 'Origin': 'https://untrusted.example'})[0], 403)
        self.assertEqual(self.call('/api/reports', headers={'Authorization': 'Bearer test-token', 'Host': 'untrusted.example'})[0], 403)

    def test_pages_cors_preflight(self):
        status, headers, _ = self.call('/api/reports', method='OPTIONS', headers={'Origin': 'https://azattau.github.io', 'Access-Control-Request-Private-Network': 'true'})
        self.assertEqual(status, 204)
        self.assertEqual(headers['Access-Control-Allow-Origin'], 'https://azattau.github.io')
        self.assertEqual(headers['Access-Control-Allow-Private-Network'], 'true')

    def test_static_allowlist(self):
        self.assertEqual(self.call('/')[0], 200)
        self.assertEqual(self.call('/app.js')[0], 200)
        self.assertEqual(self.call('/../runtime/observations.sqlite3')[0], 404)
        self.assertEqual(self.call('/data/settlements.csv.gz')[0], 404)

    def test_input_limits_and_local_models(self):
        for text in ['', '  ', 'a' * 4001, None]:
            self.assertEqual(self.call('/api/reports', {'text': text, 'model': 'test-local:1'})[0], 400)
        self.assertEqual(self.call('/api/status')[2]['models'], ['test-local:1'])
        self.assertEqual(self.call('/api/reports', {'text': 'В Уфе видели птицу.', 'model': 'unknown'})[0], 400)

    def test_district_and_multitoken_names(self):
        found = self.gazetteer.match('Верхнее Абдряшево', 'Абзелиловский район')
        self.assertEqual(len(found), 1)
        self.assertEqual(self.gazetteer.match('Уфа', 'Несуществующий район'), [])
        self.assertEqual(normalize('Городец'), 'городец')
        self.assertEqual(normalize('г. Уфа'), 'уфа')
        self.assertEqual(len(self.gazetteer.by_id), 4561)

    def test_multiple_places_and_duplicate_extraction(self):
        text = 'В Уфе и Стерлитамаке видели птиц.'
        self.output = {'observations': [observation('Уфа', text), observation('Стерлитамак', text), observation('Уфа', text)]}
        report = self.service.submit(text, 'test-local:1')
        self.assertEqual(len(report['observations']), 2)
        self.assertTrue(all(i['status'] == 'resolved' for i in report['observations']))

    def test_text_and_commas_are_preserved(self):
        text = 'В Уфе, у парка, видели птицу. <script>alert(1)</script>'
        self.output = {'observations': [observation(evidence=text)]}
        report = self.service.submit(text, 'test-local:1')
        self.assertEqual(report['text'], text)

    def test_ollama_unavailable(self):
        self.fake.shutdown(); self.fake.server_close()
        status, _, body = self.call('/api/status')
        self.assertEqual(status, 200)
        self.assertFalse(body['ollama'])


if __name__ == '__main__':
    unittest.main()
