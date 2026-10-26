"""Loopback-only HTTP adapter. All observation API calls require a session token."""
import hmac
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .core import AppError


def make_server(service, web_root, token, port=8765, pages_origin='https://azattau.github.io'):
    origins = {pages_origin, f'http://127.0.0.1:{port}', f'http://localhost:{port}'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Never log message bodies, query strings, or credentials.
            pass

        def send_body(self, status, data, mime='application/json; charset=utf-8'):
            self.send_response(status)
            origin = self.headers.get('Origin')
            if origin in origins:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            if origin in origins and self.headers.get('Access-Control-Request-Private-Network') == 'true':
                self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(data)

        def json(self, status, data):
            self.send_body(status, json.dumps(data, ensure_ascii=False).encode())

        def guard(self, auth=True):
            if self.headers.get('Host') not in {f'localhost:{port}', f'127.0.0.1:{port}'}:
                raise AppError('Недопустимый Host.', 403)
            origin = self.headers.get('Origin')
            if origin and origin not in origins:
                raise AppError('Этот сайт не разрешён для подключения.', 403)
            if auth and not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                raise AppError('Неверный код подключения. Скопируйте код из окна запуска.', 401)

        def do_OPTIONS(self):
            try:
                self.guard(auth=False)
                self.send_body(204, b'')
            except AppError as exc:
                self.json(exc.status, {'error': str(exc)})

        def do_GET(self):
            try:
                path = urlsplit(self.path).path
                self.guard(auth=path.startswith('/api/'))
                if path == '/api/status':
                    try:
                        models = service.ollama.models()
                        self.json(200, {'ollama': True, 'models': models, 'settlements': len(service.gazetteer.by_id)})
                    except AppError as exc:
                        self.json(200, {'ollama': False, 'models': [], 'settlements': len(service.gazetteer.by_id), 'error': str(exc)})
                elif path == '/api/reports':
                    self.json(200, {'reports': service.store.list()})
                elif path in {'/', '/index.html', '/app.js', '/style.css', '/favicon.svg'}:
                    name = 'index.html' if path == '/' else path[1:]
                    target = web_root / name
                    self.send_body(200, target.read_bytes(), mimetypes.guess_type(name)[0] or 'application/octet-stream')
                else:
                    raise AppError('Не найдено.', 404)
            except AppError as exc:
                self.json(exc.status, {'error': str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.json(500, {'error': 'Ошибка локального сервера. Проверьте доступность файлов проекта.'})

        def do_POST(self):
            try:
                self.guard()
                if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                    raise AppError('Требуется JSON.', 415)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise AppError('Неверная длина запроса.')
                if not 0 < length <= 32000:
                    raise AppError('Недопустимый размер запроса.', 413)
                self.connection.settimeout(10)
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    raise AppError('Некорректный JSON.')
                if not isinstance(body, dict):
                    raise AppError('Требуется JSON-объект.')
                path = urlsplit(self.path).path
                if path == '/api/reports':
                    result = service.submit(body.get('text'), body.get('model'))
                    self.json(201, result)
                elif path == '/api/resolve':
                    fields = ['report_id', 'observation_id', 'candidate_id']
                    if any(not isinstance(body.get(key), str) for key in fields):
                        raise AppError('Не указаны идентификаторы места и сообщения.')
                    self.json(200, service.store.resolve(*(body[key] for key in fields)))
                else:
                    raise AppError('Не найдено.', 404)
            except AppError as exc:
                self.json(exc.status, {'error': str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.json(500, {'error': 'Ошибка локального сервера. Проверьте доступность базы данных.'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    # Resolve ephemeral port for integration tests.
    if port == 0:
        port = server.server_port
        origins.update({f'http://127.0.0.1:{port}', f'http://localhost:{port}'})
    server.daemon_threads = True
    return server
