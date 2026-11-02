from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlsplit
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('--port',type=int,default=8000)
args=parser.parse_args()
ROOT=Path(__file__).resolve().parents[1]
SOURCES={'china':Path('C:/Workspace/China-history'),'nfc':Path('C:/Workspace/NFC'),'deport':Path('C:/Workspace/deportation-suite-kazax')}
class Handler(SimpleHTTPRequestHandler):
    def translate_path(self,path):
        parts=unquote(urlsplit(path).path).strip('/').split('/')
        base=ROOT
        if parts[0]=='capture' and len(parts)>1:
            base=SOURCES.get(parts[1],ROOT/'.forbidden')
            parts=parts[2:]
        if any(p.startswith('.') or p=='..' for p in parts if p): return str(ROOT/'.forbidden')
        target=base.joinpath(*parts).resolve()
        if not target.is_relative_to(base.resolve()): return str(ROOT/'.forbidden')
        if target.is_dir(): target=target/'index.html'
        if target.suffix.lower() not in {'.html','.css','.js','.png','.jpg','.jpeg','.webp','.gif','.svg','.ico','.woff','.woff2','.mp3','.ogg','.mp4','.webm','.vtt','.json'}: return str(ROOT/'.forbidden')
        return str(target)
    def list_directory(self,path): self.send_error(404)
    def log_message(self,*args): pass
print(f'Portfolio review: http://127.0.0.1:{args.port}',flush=True)
ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
