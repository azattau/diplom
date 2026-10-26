"""Start the local app: python run.py. No pip dependencies required."""
import argparse
import secrets
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from backend.core import Gazetteer, Ollama, Service, Store
from backend.server import make_server


def main():
    parser = argparse.ArgumentParser(description='Журнал наблюдений птиц · локальная Ollama')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--ollama-url', default='http://127.0.0.1:11434')
    parser.add_argument('--pages-origin', default='https://azattau.github.io')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    endpoint = urlsplit(args.ollama_url)
    if endpoint.scheme != 'http' or endpoint.hostname not in {'127.0.0.1', 'localhost', '::1'} or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in {'', '/'}:
        parser.error('--ollama-url должен указывать на локальную Ollama (http://127.0.0.1:11434).')
    if not 1024 <= args.port <= 65535 or args.timeout < 10:
        parser.error('Порт: 1024–65535; timeout: не менее 10 секунд.')
    root = Path(__file__).resolve().parent
    service = Service(Gazetteer(root / 'data/settlements.csv.gz'),
                      Store(root / 'runtime/observations.sqlite3'), Ollama(args.ollama_url, args.timeout))
    token = secrets.token_urlsafe(24)
    try:
        server = make_server(service, root / 'web', token, args.port, args.pages_origin)
    except OSError as exc:
        parser.exit(1, f'Не удалось открыть порт {args.port}: {exc}. Закройте другой экземпляр или укажите --port.\n')
    address = f'http://127.0.0.1:{args.port}'
    print('\nПТИЦЫ · Журнал наблюдений\n')
    print('Локальный сайт:', address + '/#token=' + token)
    print('Адрес сервера для GitHub Pages:', address)
    print('Код подключения:', token)
    print('\nНе закрывайте это окно во время работы. Для остановки: Ctrl+C.\n', flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(address + '/#token=' + token)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nСервер остановлен. Сообщения сохранены в runtime/observations.sqlite3.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
