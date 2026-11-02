"""Preview the packaged static site on loopback, with clean case URLs and real 404 responses."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import argparse

ROOT = Path(__file__).resolve().parents[1]

class PreviewHandler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".xml": "application/xml", ".js": "text/javascript"}

    def send_head(self):
        directory = Path(self.directory).resolve()
        requested = Path(self.translate_path(self.path)).resolve()
        if not requested.is_relative_to(directory):
            return self.not_found()
        if requested.is_dir():
            if requested == directory and (requested / "index.html").is_file():
                return super().send_head()
            return self.not_found()
        if requested.is_file():
            return super().send_head()
        clean = requested.with_suffix(".html")
        if not requested.suffix and clean.is_file():
            original = self.path
            parts = urlsplit(original)
            self.path = urlunsplit(parts._replace(path=parts.path + ".html"))
            try:
                return super().send_head()
            finally:
                self.path = original
        return self.not_found()

    def not_found(self):
        page = Path(self.directory) / "404.html"
        if not page.is_file():
            self.send_error(404)
            return None
        stream = page.open("rb")
        self.send_response(404)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(page.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return stream

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--directory", type=Path, default=ROOT / "release" / "portfolio-v0.3-rc1")
    args = parser.parse_args()
    site = args.directory.resolve()
    if not (site / "index.html").is_file():
        parser.error("Build the site first: python scripts/build.py")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(PreviewHandler, directory=str(site)))
    print(f"Local preview: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
