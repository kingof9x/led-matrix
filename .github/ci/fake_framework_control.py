# CI only: stands in for Framework Control's local service, answering /api/thermal with an answer
# captured on a Framework 16 (Ryzen 9 7940HS), so the built exe's Framework Control source can be checked.
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

ANSWER = {
    "fans": [{"name": "Left Fan", "rpm": 0}, {"name": "Right Fan", "rpm": 0}],
    "temps": {"ambient_f75303@4d": 41, "apu_f75303@4d": 43, "charger_f75303@4d": 40, "cpu@4c": 44,
              "gpu_amb_f75303@4d": 0, "gpu_vr_f75303@4d": 0, "gpu_vram_f75303@4d": 0},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/api/thermal":
            self.send_error(404)
            return
        body = json.dumps(ANSWER).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
