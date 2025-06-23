# scanner_api.py
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import cgi, json, logging, asyncio, mimetypes
from urllib.parse import parse_qs, urlparse
from io import BytesIO

from main import ModuleManager
from modules import (
    image_storage,
    nsfw_scanner,
    statistics,
    tagging,
    deepdanbooru_tags,
)
import token_manager
from gif_batch import scan_batch                                  # << batch helper

logging.basicConfig(
    filename="scanner.log",
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

manager = ModuleManager()

MAX_IMAGE_SIZE  = 10 * 1024 * 1024   # 10 MB  – Einzelbild
MAX_BATCH_SIZE  = 25 * 1024 * 1024   # 25 MB  – GIF / Video


# ─────────────────────────────────── helpers ───────────────────────────────────
def _is_valid_image(data: bytes) -> bool:
    try:
        from PIL import Image
        with Image.open(BytesIO(data)) as img:
            img.verify()
        return True
    except Exception:
        return False


def process_image(image_bytes: bytes) -> dict:
    """Pipeline für Einzelbilder – unverändert zu deiner Original-Version."""
    try:
        results = {}

        # 1 NSFW
        try:
            nsfw_result = nsfw_scanner.process_image(image_bytes)
        except Exception as e:
            nsfw_result = {"error": str(e)}
        results["modules.nsfw_scanner"] = nsfw_result

        # 2 Tagging
        try:
            tag_result = tagging.process_image(image_bytes)
        except Exception as e:
            tag_result = {"error": str(e)}
        results["modules.tagging"] = tag_result
        tags = [t.get("label") for t in tag_result.get("tags", [])
                if isinstance(t, dict)]

        # 3 DeepDanbooru
        try:
            ddb_result = deepdanbooru_tags.process_image(image_bytes)
        except Exception as e:
            ddb_result = {"error": str(e)}
        results["modules.deepdanbooru_tags"] = ddb_result
        ddb_tags = [t.get("label") for t in ddb_result.get("tags", [])
                    if isinstance(t, dict)]

        # 4 Stats
        all_labels = tags + ddb_tags
        try:
            statistics.record_tags(all_labels)
            results["modules.statistics"] = {"recorded": len(all_labels)}
        except Exception as e:
            results["modules.statistics"] = {"error": str(e)}

        # 5 Storage
        try:
            storage_result = image_storage.process_image(
                image_bytes,
                tags=tag_result.get("tags"),
                nsfw_meta=nsfw_result,
                danbooru_tags=ddb_result.get("tags"),
            )
        except Exception as e:
            storage_result = {"error": str(e)}
        results["modules.image_storage"] = storage_result

        # weitere Module
        skip = {
            "modules.nsfw_scanner",
            "modules.tagging",
            "modules.deepdanbooru_tags",
            "modules.image_storage",
            "modules.statistics",
        }
        for name, mod in manager.get_modules().items():
            if name in skip or not hasattr(mod, "process_image"):
                continue
            try:
                results[name] = mod.process_image(image_bytes)
            except Exception as e:
                results[name] = {"error": str(e)}
                logger.exception("Module %s failed", name)

        return results
    except Exception as e:
        logger.exception("process_image failed")
        return {"error": str(e)}


# ─────────────────────────────── HTTP Handler ────────────────────────────────
class ScannerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- token ----
    def _validate_token(self) -> bool:
        tok = self.headers.get("Authorization")
        if not tok or not token_manager.is_valid_token(tok):
            self.send_response(403); self.end_headers()
            return False
        return True

    # ---- GET ----
    def do_GET(self):
        if self.path.startswith("/token"):
            parsed = urlparse(self.path)
            email  = parse_qs(parsed.query).get("email", [None])[0]
            renew  = "renew" in parsed.query
            if not email:
                self.send_response(400); self.end_headers(); return
            token = token_manager.get_token(email, renew=renew)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(token.encode()); return

        if self.path != "/stats":
            self.send_response(404); self.end_headers(); return
        if not self._validate_token(): return

        stats = statistics.get_statistics()
        body  = json.dumps(stats).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    # ---- POST ----
    def do_POST(self):
        if self.path == "/check":  self._handle_check();  return
        if self.path == "/batch":  asyncio.run(self._handle_batch()); return
        self.send_response(404); self.end_headers()

    # ---------- /check ----------
    def _handle_check(self):
        if not self._validate_token(): return
        form = self._parse_multipart()
        part = form and form.getfirst("image")  # FieldStorage -> getfirst
        file_item = form["image"] if form and "image" in form else None
        if file_item is None or getattr(file_item, "file", None) is None:
            self.send_response(400); self.end_headers(); return

        buf = file_item.file.read()
        if len(buf) > MAX_IMAGE_SIZE:
            self.send_response(413); self.end_headers(); return
        if not _is_valid_image(buf):
            self.send_response(400); self.end_headers(); return

        body = json.dumps(process_image(buf)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    # ---------- /batch ----------
    async def _handle_batch(self):
        if not self._validate_token(): return
        if "multipart/form-data" not in self.headers.get("Content-Type", ""):
            self.send_response(400); self.end_headers(); return

        form = self._parse_multipart()
        item = form["file"] if form and "file" in form else None
        if item is None or getattr(item, "file", None) is None:
            self.send_response(400); self.end_headers(); return

        raw = item.file.read()
        if len(raw) > MAX_BATCH_SIZE:
            self.send_response(413); self.end_headers(); return

        mime = item.type or mimetypes.guess_type(item.filename or "")[0] or ""
        try:
            result = await scan_batch(raw, mime)
            body   = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        except Exception as e:
            logger.exception("batch failed")
            self.send_response(500); self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    # ---- helper ----
    def _parse_multipart(self):
        try:
            return cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type"),
                },
            )
        except Exception:
            logger.exception("multipart parse failed"); return None

    def log_message(self, *a):  # quiet
        return


# ───────────────────────────── Server start ────────────────────────────────
def run(port: int = 8000):
    ThreadingHTTPServer(("", port), ScannerHandler).serve_forever()

if __name__ == "__main__":
    run()
