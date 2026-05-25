from pathlib import Path
import traceback

from server import Handler
from http.server import ThreadingHTTPServer


LOG = Path(__file__).resolve().parent / "server-runtime.log"


def main():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
        LOG.write_text("Servidor ativo em http://127.0.0.1:8765\n", encoding="utf-8")
        server.serve_forever()
    except Exception:
        LOG.write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
