#!/usr/bin/env python3
"""
ClipForge Local Web App — Free, Unlimited OpusClip-Style Website on Your PC
Launches a local website at http://127.0.0.1:8000 where you can paste any
YouTube, YouTube Live, or Twitch link, auto-detect hype moments via Heatmap +
Live Chat spikes, and render & watch 9:16 split-screen MP4 clips right in the browser.

One-time setup (Windows / Mac):
    pip install yt-dlp faster-whisper
    # Plus ffmpeg installed (Windows: winget install Gyan.FFmpeg | Mac: brew install ffmpeg)

Run:
    python clipforge_app.py
"""

import json
import mimetypes
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import os
PORT = int(os.environ.get("PORT", 8000))
HOST = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
OUT_DIR = Path.cwd() / "clipforge_exports"
OUT_DIR.mkdir(exist_ok=True)
JOBS: dict[str, dict] = {}


def sec_to_hms(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = min(99, int(round((sec % 1) * 100)))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def analyze_url(url: str, clip_len: int = 35, top_n: int = 6) -> dict:
    info_raw = subprocess.check_output(
        [sys.executable, "-m", "yt_dlp", "--dump-json", "--skip-download", url],
        text=True,
    )
    info = json.loads(info_raw)
    title = info.get("title") or "Untitled Video"
    duration = float(info.get("duration") or 0)
    heatmap = info.get("heatmap") or []

    chat_buckets: Counter[int] = Counter()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        subprocess.run(
            [
                sys.executable, "-m", "yt_dlp",
                "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                "-o", str(tmp_dir / "chat"), url,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for chat_file in tmp_dir.glob("*.live_chat.json"):
            with chat_file.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                        offset_ms = (
                            obj.get("replayChatItemAction", {}).get("videoOffsetTimeMsec")
                            or obj.get("videoOffsetTimeMsec")
                        )
                        if offset_ms is not None:
                            chat_buckets[int(float(offset_ms) // 10000)] += 1
                    except Exception:
                        continue

    candidates = []
    if heatmap:
        max_chat = max(chat_buckets.values(), default=1)
        for pt in heatmap:
            t_mid = (float(pt["start_time"]) + float(pt["end_time"])) / 2.0
            heat_score = float(pt.get("value", 0.0))
            bucket = int(t_mid // 10)
            chat_density = (
                chat_buckets[bucket - 1] + chat_buckets[bucket] + chat_buckets[bucket + 1]
            ) / (3.0 * max_chat)
            combined = (0.65 * heat_score + 0.35 * chat_density) if chat_buckets else heat_score
            start_s = max(0.0, t_mid - clip_len * 0.35)
            end_s = min(duration or (start_s + clip_len), start_s + clip_len)
            candidates.append({
                "start": round(start_s, 1),
                "end": round(end_s, 1),
                "start_hms": sec_to_hms(start_s),
                "end_hms": sec_to_hms(end_s),
                "score": round(combined * 100, 1),
                "heat": round(heat_score * 100, 1),
                "chat_msgs": chat_buckets[bucket],
            })
    elif chat_buckets:
        max_chat = max(chat_buckets.values(), default=1)
        for bucket, count in chat_buckets.most_common(top_n * 4):
            t_mid = bucket * 10.0
            start_s = max(0.0, t_mid - clip_len * 0.35)
            end_s = min(duration or (start_s + clip_len), start_s + clip_len)
            candidates.append({
                "start": round(start_s, 1),
                "end": round(end_s, 1),
                "start_hms": sec_to_hms(start_s),
                "end_hms": sec_to_hms(end_s),
                "score": round((count / max_chat) * 100, 1),
                "heat": 0.0,
                "chat_msgs": count,
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    picked = []
    for cand in candidates:
        if any(abs(cand["start"] - p["start"]) < clip_len * 0.8 for p in picked):
            continue
        picked.append(cand)
        if len(picked) >= top_n:
            break

    # Fallback evenly-spaced suggestions if neither heatmap nor live chat exists
    if not picked and duration > 0:
        step = max(clip_len, duration / max(1, top_n))
        for i in range(top_n):
            st = i * step
            if st >= duration - 5:
                break
            en = min(duration, st + clip_len)
            picked.append({
                "start": round(st, 1),
                "end": round(en, 1),
                "start_hms": sec_to_hms(st),
                "end_hms": sec_to_hms(en),
                "score": 80 - i * 3,
                "heat": 0.0,
                "chat_msgs": 0,
            })

    return {"title": title, "duration": duration, "duration_hms": sec_to_hms(duration), "clips": picked}


def render_job(job_id: str, payload: dict) -> None:
    job = JOBS[job_id]
    try:
        url = payload["url"]
        start_hms = payload["start_hms"]
        end_hms = payload["end_hms"]
        layout = payload.get("layout", "split")
        cam = payload.get("cam", "top-left")
        hook = payload.get("hook", "")
        hook_dur = float(payload.get("hook_dur", 3.5))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            src_mp4 = tmp_dir / "source.mp4"

            job["status"] = "Downloading clip section via yt-dlp..."
            subprocess.run(
                [
                    sys.executable, "-m", "yt_dlp",
                    "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
                    "--merge-output-format", "mp4",
                    "--download-sections", f"*{start_hms}-{end_hms}",
                    "-o", str(src_mp4),
                    url,
                ],
                check=True,
            )

            words = []
            try:
                from faster_whisper import WhisperModel
                job["status"] = "Transcribing captions with Whisper..."
                model = WhisperModel("base", device="auto", compute_type="int8")
                segments, _ = model.transcribe(str(src_mp4), word_timestamps=True)
                for seg in segments:
                    for w in (seg.words or []):
                        t = w.word.strip()
                        if t:
                            words.append({"start": w.start, "end": w.end, "text": t})
            except ImportError:
                job["status"] = "Whisper not installed (rendering without auto-captions)..."

            ass_path = None
            if words or hook.strip():
                ass_path = tmp_dir / "captions.ass"
                header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: PopWord,Arial,82,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,4,2,60,60,260,1
Style: HookBox,Arial,54,&H0009090B,&H0009090B,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,26,0,5,80,80,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
                lines = [header]
                if hook.strip():
                    safe_hook = hook.strip().upper().replace("{", "").replace("}", "")
                    lines.append(
                        f"Dialogue: 1,0:00:00.00,{format_ass_time(hook_dur)},HookBox,,0,0,0,,{{\\pos(540,672)}}{safe_hook}\n"
                    )
                for w in words:
                    st = format_ass_time(w["start"])
                    en = format_ass_time(max(w["end"], w["start"] + 0.12))
                    txt = w["text"].upper().replace("{", "").replace("}", "")
                    lines.append(f"Dialogue: 0,{st},{en},PopWord,,0,0,0,,{txt}\n")
                ass_path.write_text("".join(lines), encoding="utf-8")

            job["status"] = "Rendering 9:16 MP4 with FFmpeg..."
            out_name = f"clip_{job_id[:8]}.mp4"
            out_path = OUT_DIR / out_name

            if layout == "split":
                cam_crop_map = {
                    "top-left": "crop=iw*0.32:ih*0.38:0:0",
                    "top-right": "crop=iw*0.32:ih*0.38:iw-iw*0.32:0",
                    "bottom-left": "crop=iw*0.32:ih*0.38:0:ih-ih*0.38",
                    "bottom-right": "crop=iw*0.32:ih*0.38:iw-iw*0.32:ih-ih*0.38",
                    "center": "crop=iw*0.32:ih*0.38:(iw-iw*0.32)/2:(ih-ih*0.38)/2",
                }
                cam_crop = cam_crop_map.get(cam, cam_crop_map["top-left"])
                vf = (
                    f"[0:v]{cam_crop},scale=1080:672:force_original_aspect_ratio=increase,crop=1080:672[top];"
                    f"[0:v]scale=1080:1248:force_original_aspect_ratio=increase,crop=1080:1248[bot];"
                    f"[top][bot]vstack=inputs=2[stacked]"
                )
                if ass_path:
                    esc = str(ass_path).replace("\\", "/").replace(":", "\\:")
                    vf += f";[stacked]ass='{esc}'[vout]"
                else:
                    vf += ";[stacked]null[vout]"
            else:
                vf = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[base]"
                if ass_path:
                    esc = str(ass_path).replace("\\", "/").replace(":", "\\:")
                    vf += f";[base]ass='{esc}'[vout]"
                else:
                    vf += ";[base]null[vout]"

            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(src_mp4),
                    "-filter_complex", vf,
                    "-map", "[vout]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    str(out_path),
                ],
                check=True,
            )

            job["done"] = True
            job["status"] = "Ready!"
            job["file"] = out_name
    except Exception as exc:
        job["done"] = True
        job["error"] = str(exc)
        job["status"] = f"Error: {exc}"


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ClipForge Local — Free Unlimited Clipper</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#09090f] text-zinc-100 min-h-screen font-sans">
  <header class="border-b border-white/10 bg-[#0d0d16] px-5 py-3 flex items-center justify-between">
    <div class="flex items-center gap-2.5">
      <div class="w-8 h-8 rounded-lg bg-gradient-to-br from-violet-500 to-fuchsia-500 flex items-center justify-center font-bold">CF</div>
      <div>
        <h1 class="font-bold text-base">ClipForge Local <span class="text-[11px] px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 ml-1">Unlimited • Free</span></h1>
        <p class="text-xs text-zinc-400">Paste any YouTube, YouTube Live, or Twitch link — renders 9:16 clips directly on your PC</p>
      </div>
    </div>
  </header>

  <main class="max-w-6xl mx-auto p-4 sm:p-6 space-y-5">
    <!-- URL + Settings Card -->
    <section class="bg-[#12121e] border border-white/10 rounded-xl p-4 space-y-4">
      <div class="flex flex-col sm:flex-row gap-2">
        <input id="urlInput" type="url" placeholder="Paste YouTube, YouTube Live, or Twitch URL..." class="flex-1 bg-[#181828] border border-white/10 rounded-lg px-3.5 py-2.5 text-sm text-white focus:outline-none focus:border-violet-500" />
        <button id="analyzeBtn" class="px-5 py-2.5 rounded-lg bg-violet-600 hover:bg-violet-500 font-semibold text-sm transition">Find Best Clips</button>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-4 gap-3 text-xs">
        <div>
          <label class="text-zinc-400 block mb-1">Layout</label>
          <select id="layoutSelect" class="w-full bg-[#181828] border border-white/10 rounded-lg px-2.5 py-2">
            <option value="split" selected>Split (Facecam Top + Game Bottom)</option>
            <option value="fill">Fill (Center 9:16 Crop)</option>
          </select>
        </div>
        <div>
          <label class="text-zinc-400 block mb-1">Facecam Corner</label>
          <select id="camSelect" class="w-full bg-[#181828] border border-white/10 rounded-lg px-2.5 py-2">
            <option value="top-left" selected>Top-Left</option>
            <option value="top-right">Top-Right</option>
            <option value="bottom-left">Bottom-Left</option>
            <option value="bottom-right">Bottom-Right</option>
            <option value="center">Center</option>
          </select>
        </div>
        <div class="sm:col-span-2">
          <label class="text-zinc-400 block mb-1">Hook Banner Text (optional)</label>
          <input id="hookInput" type="text" value="GTA 6 HAS US LOSING IT" class="w-full bg-[#181828] border border-white/10 rounded-lg px-2.5 py-2" />
        </div>
      </div>

      <div class="flex flex-wrap items-center gap-2 pt-2 border-t border-white/10 text-xs">
        <span class="text-zinc-400">Or render custom timestamps:</span>
        <input id="manualStart" type="text" value="00:02:25" class="w-24 bg-[#181828] border border-white/10 rounded px-2 py-1 font-mono" />
        <span>→</span>
        <input id="manualEnd" type="text" value="00:03:08" class="w-24 bg-[#181828] border border-white/10 rounded px-2 py-1 font-mono" />
        <button id="manualRenderBtn" class="px-3.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 font-semibold transition">Render Custom Clip</button>
      </div>
    </section>

    <!-- Results Grid -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-5">
      <section class="lg:col-span-6 bg-[#12121e] border border-white/10 rounded-xl p-4 space-y-3">
        <h2 class="font-semibold text-sm">Detected Hype Moments (Heatmap + Chat Spikes)</h2>
        <div id="videoMeta" class="text-xs text-zinc-400">Paste a link and click Find Best Clips.</div>
        <div id="clipsList" class="space-y-2.5 max-h-[480px] overflow-y-auto"></div>
      </section>

      <section class="lg:col-span-6 bg-[#12121e] border border-white/10 rounded-xl p-4 flex flex-col items-center justify-center min-h-[420px] space-y-3">
        <div id="renderStatus" class="text-xs text-violet-300 font-medium">No clip rendered yet.</div>
        <video id="outputVideo" controls class="hidden max-h-[460px] rounded-xl border border-white/15 bg-black"></video>
        <a id="downloadLink" href="#" download class="hidden px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-xs font-semibold">Download MP4</a>
      </section>
    </div>
  </main>

  <script>
    const clipsList = document.getElementById('clipsList');
    const videoMeta = document.getElementById('videoMeta');
    const renderStatus = document.getElementById('renderStatus');
    const outputVideo = document.getElementById('outputVideo');
    const downloadLink = document.getElementById('downloadLink');

    async function startRender(start_hms, end_hms) {
      const url = document.getElementById('urlInput').value.trim();
      if (!url) return alert('Paste a YouTube or Twitch URL first.');
      renderStatus.textContent = 'Starting render...';
      outputVideo.classList.add('hidden');
      downloadLink.classList.add('hidden');

      const res = await fetch('/api/render', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url,
          start_hms,
          end_hms,
          layout: document.getElementById('layoutSelect').value,
          cam: document.getElementById('camSelect').value,
          hook: document.getElementById('hookInput').value
        })
      });
      const { job_id } = await res.json();
      const poll = setInterval(async () => {
        const jRes = await fetch('/api/job?id=' + encodeURIComponent(job_id));
        const job = await jRes.json();
        renderStatus.textContent = job.status;
        if (job.done) {
          clearInterval(poll);
          if (job.file) {
            const mediaUrl = '/exports/' + encodeURIComponent(job.file);
            outputVideo.src = mediaUrl;
            outputVideo.classList.remove('hidden');
            downloadLink.href = mediaUrl;
            downloadLink.download = job.file;
            downloadLink.classList.remove('hidden');
          }
        }
      }, 1000);
    }

    document.getElementById('analyzeBtn').addEventListener('click', async () => {
      const url = document.getElementById('urlInput').value.trim();
      if (!url) return;
      videoMeta.textContent = 'Scanning heatmap & live chat spikes...';
      clipsList.innerHTML = '';
      const res = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url })
      });
      const data = await res.json();
      if (data.error) {
        videoMeta.textContent = 'Error: ' + data.error;
        return;
      }
      videoMeta.textContent = `${data.title} (${data.duration_hms})`;
      data.clips.forEach((c, idx) => {
        const card = document.createElement('div');
        card.className = 'p-3 rounded-lg bg-[#181828] border border-white/10 flex items-center justify-between gap-3';
        card.innerHTML = `
          <div>
            <div class="text-xs font-bold text-emerald-400">Clip #${idx + 1} • Score ${c.score}</div>
            <div class="text-xs font-mono text-zinc-200 mt-0.5">${c.start_hms} → ${c.end_hms}</div>
            <div class="text-[11px] text-zinc-400">Heat: ${c.heat}% • Chat: ${c.chat_msgs} msgs/10s</div>
          </div>
          <button class="render-btn px-3 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-500 text-xs font-semibold">Render MP4</button>
        `;
        card.querySelector('.render-btn').addEventListener('click', () => startRender(c.start_hms, c.end_hms));
        clipsList.appendChild(card);
      });
    });

    document.getElementById('manualRenderBtn').addEventListener('click', () => {
      startRender(document.getElementById('manualStart').value, document.getElementById('manualEnd').value);
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            data = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path == "/api/job":
            qs = parse_qs(parsed.query)
            job_id = qs.get("id", [""])[0]
            self._send_json(JOBS.get(job_id, {"done": True, "status": "Not found"}))
        elif parsed.path.startswith("/exports/"):
            fname = Path(parsed.path.replace("/exports/", "", 1)).name
            fpath = OUT_DIR / fname
            if not fpath.exists():
                self.send_error(404)
                return
            data = fpath.read_bytes()
            mime = mimetypes.guess_type(str(fpath))[0] or "video/mp4"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path == "/api/analyze":
            try:
                self._send_json(analyze_url(body["url"]))
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
        elif self.path == "/api/render":
            job_id = uuid.uuid4().hex
            JOBS[job_id] = {"done": False, "status": "Queued..."}
            threading.Thread(target=render_job, args=(job_id, body), daemon=True).start()
            self._send_json({"job_id": job_id})
        else:
            self.send_error(404)


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    print(f"ClipForge Web App running at {url}")
    if HOST == "127.0.0.1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()