from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from batch_generator import generate_batch, parse_quotes
from config import ANIMATIONS, FORMATS, MUSIC, OUTPUT, ROOT, STYLES, find_encoder, find_ffmpeg
from renderer import render_video

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
RENDER_SLOT = threading.Semaphore(1)
STATIC = ROOT / "static"


def options_from(data: dict) -> dict:
    music_name = str(data.get("music", "")).strip()
    music = (MUSIC / Path(music_name).name) if music_name else None
    if music and not music.is_file():
        raise ValueError("Выбранная музыка не найдена в библиотеке.")
    font = str(data.get("font", "")).strip()
    if font and (not Path(font).is_file() or Path(font).suffix.lower() not in {".ttf", ".otf", ".ttc"}):
        raise ValueError("Укажите существующий файл шрифта TTF/OTF/TTC.")
    return dict(style=data.get("style", "Reference Mode"), animation=data.get("animation", "Как в референсе"), format_name=data.get("format", "Горизонтальный 1920×1080"), fps=int(data.get("fps", 30)), speed=float(data.get("speed", 1)), music=music, volume=float(data.get("volume", 0.7)), fade=float(data.get("fade", 0.3)), duration=float(data["duration"]) if data.get("duration") else None, font_override=font, foreground=str(data.get("foreground", "")), accent=str(data.get("accent", "")), preview=bool(data.get("quick_preview", False)), delay=float(data.get("delay", 0)), opacity=float(data.get("opacity", 1)), scale=float(data.get("scale", 1)), position_x=float(data.get("position_x", 0)), position_y=float(data.get("position_y", 0)), sync_mode=str(data.get("sync_mode", "Равномерно")))


def start_job(data: dict, batch: bool) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "state": "queued", "progress": 0, "message": "Ожидание", "files": [], "report": "", "errors": [], "cancel": threading.Event()}
    with LOCK:
        JOBS[job_id] = job

    def update(fraction: float, message: str = "Рендеринг") -> None:
        with LOCK:
            job["progress"] = round(100 * fraction, 1)
            job["message"] = message

    def work() -> None:
        try:
            options = options_from(data)
            with RENDER_SLOT:
                if job["cancel"].is_set():
                    raise InterruptedError("Генерация остановлена.")
                job["state"] = "running"
                if batch:
                    quotes = parse_quotes(data["filename"], data["content"])
                    report, report_path = generate_batch(quotes, options, random_music=bool(data.get("random_music")), progress=update, cancel=job["cancel"])
                    job["files"] = [Path(row["file"]).name for row in report if row["status"] == "ok"]
                    job["errors"] = [f"#{row['index']}: {row['error']}" for row in report if row["status"] == "error"]
                    job["report"] = report_path.name
                else:
                    path = render_video(str(data.get("text", "")), **options, progress=lambda fraction: update(fraction), cancel=job["cancel"])
                    job["files"] = [path.name]
                job["state"] = "stopped" if job["cancel"].is_set() else "done"
                job["progress"] = 100 if job["state"] == "done" else job["progress"]
                job["message"] = "Готово" if job["state"] == "done" else "Остановлено"
        except InterruptedError:
            job["state"] = "stopped"
            job["message"] = "Остановлено"
        except Exception as exc:
            job["state"] = "error"
            job["message"] = str(exc)
    threading.Thread(target=work, daemon=True).start()
    return job_id


class Handler(BaseHTTPRequestHandler):
    def _json(self, value: dict, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, download: bool = False) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        start = 0
        end = size - 1
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes="):
            spec = range_header[6:].split(",", 1)[0]
            left, _, right = spec.partition("-")
            try:
                start = int(left) if left else max(0, size - int(right))
                end = min(size - 1, int(right)) if right and left else size - 1
            except ValueError:
                self.send_error(416)
                return
        if start > end or start >= size:
            self.send_error(416)
            return
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                block = stream.read(min(65536, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/":
            return self._file(STATIC / "index.html")
        if route == "/static/style.css":
            return self._file(STATIC / "style.css")
        if route == "/static/app.js":
            return self._file(STATIC / "app.js")
        if route == "/api/info":
            try:
                ffmpeg = find_ffmpeg()
                engine = {"ffmpeg": str(ffmpeg), "encoder": find_encoder(ffmpeg)}
            except Exception as exc:
                engine = {"error": str(exc)}
            return self._json({"styles": list(STYLES), "themes": STYLES, "animations": ANIMATIONS, "formats": list(FORMATS), "music": [p.name for p in MUSIC.iterdir() if p.suffix.lower() in {".mp3", ".wav"}], "engine": engine, "output": str(OUTPUT)})
        if route.startswith("/api/job/"):
            job_id = route.rsplit("/", 1)[-1]
            job = JOBS.get(job_id)
            if not job:
                return self._json({"error": "Задача не найдена"}, 404)
            return self._json({k: v for k, v in job.items() if k != "cancel"})
        if route.startswith("/output/"):
            return self._file(OUTPUT / Path(route).name, download=route.endswith(".csv"))
        self.send_error(404)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        if length > 30_000_000:
            return self._json({"error": "Файл слишком большой (предел 30 МБ)."}, 413)
        body = self.rfile.read(length)
        try:
            if route == "/api/upload":
                filename = Path(unquote(self.headers.get("X-Filename", ""))).name
                if not filename or Path(filename).suffix.lower() not in {".mp3", ".wav"}:
                    raise ValueError("Загрузите MP3 или WAV.")
                safe = f"{uuid.uuid4().hex[:8]}_{filename}"
                target = MUSIC / safe
                target.write_bytes(body)
                return self._json({"filename": safe})
            if route == "/api/open-output":
                os.startfile(OUTPUT)
                return self._json({"ok": True})
            if route.startswith("/api/stop/"):
                job = JOBS.get(route.rsplit("/", 1)[-1])
                if not job:
                    raise ValueError("Задача не найдена.")
                job["cancel"].set()
                return self._json({"ok": True})
            if route in {"/api/render", "/api/batch"}:
                data = json.loads(body.decode("utf-8"))
                if route == "/api/batch":
                    parse_quotes(data.get("filename", ""), data.get("content", ""))
                elif not str(data.get("text", "")).strip():
                    raise ValueError("Введите текст цитаты.")
                options_from(data)
                return self._json({"job": start_job(data, route == "/api/batch")})
            self.send_error(404)
        except Exception as exc:
            self._json({"error": str(exc)}, 400)

    def log_message(self, format: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), format % args))


def main() -> None:
    host = "127.0.0.1"
    port = int(os.environ.get("AUTO_SHORTS_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"AUTO SHORTS STUDIO: {url}", flush=True)
    if os.environ.get("AUTO_SHORTS_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
