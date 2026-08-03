#!/usr/bin/env python3
"""Static server for the skew probe.

Serves TWO roots:
  /        -> this directory (the probe: index.html)
  /track/  -> a track directory given as argv[1] (stems.json, video.mp4, stems/*.m4a)

Supports HTTP Range requests (206) — required for media seeking on every
browser we care about (Chrome, iPad Safari, Samsung/Tizen).

Usage:
  python serve.py <track_dir> [port]
  python serve.py X:/GitHub/k25/data/47bae048e13c 8077
"""
import os
import re
import sys
import posixpath
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PROBE_DIR = os.path.dirname(os.path.abspath(__file__))

MIME = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".js": "text/javascript",
    ".css": "text/css",
}

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    track_dir = None  # set in main()

    def log_message(self, fmt, *args):  # quieter: one line, no per-chunk noise
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def resolve(self, path):
        """Map URL path to filesystem path; returns None if outside roots."""
        path = path.split("?", 1)[0].split("#", 1)[0]
        path = posixpath.normpath(path)
        if path in ("/", "."):
            path = "/index.html"
        parts = [p for p in path.split("/") if p and p not in (".", "..")]
        if parts and parts[0] == "track":
            root, rel = self.track_dir, parts[1:]
        else:
            root, rel = PROBE_DIR, parts
        full = os.path.join(root, *rel)
        # containment check
        if os.path.commonpath([os.path.abspath(full), root]) != root:
            return None
        return full

    def do_GET(self):
        self.serve(head=False)

    def do_HEAD(self):
        self.serve(head=True)

    def serve(self, head):
        full = self.resolve(self.path)
        if not full or not os.path.isfile(full):
            self.send_error(404)
            return
        size = os.path.getsize(full)
        ctype = MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream")

        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        if rng:
            m = RANGE_RE.match(rng.strip())
            if m:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:  # suffix range: last N bytes
                    start = max(0, size - int(e))
                    end = size - 1
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if head:
            return
        try:
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # client hung up mid-stream (normal for media elements)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    track_dir = os.path.abspath(sys.argv[1])
    if not os.path.isfile(os.path.join(track_dir, "stems.json")):
        print("error: %s has no stems.json" % track_dir)
        sys.exit(2)
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8077
    Handler.track_dir = track_dir
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("probe:  http://localhost:%d/index.html?track=/track" % port)
    print("track:  %s -> /track/" % track_dir)
    print("LAN:    http://<this-pc-ip>:%d/index.html?track=/track" % port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
