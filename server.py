import threading
import http.server
import socketserver
from tg_event_bot import main  # <-- импорт из нового файла

PORT = 8443  # Render Free видит открытый порт

def run_http():
    with socketserver.TCPServer(("", PORT), http.server.SimpleHTTPRequestHandler) as httpd:
        print(f"HTTP server running on port {PORT}")
        httpd.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_http, daemon=True).start()
    main()