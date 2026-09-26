"""
Bot Telegram: kirim link vidmonstr.com/e/... atau /d/... -> bot membalas file videonya.

Cara kerja:
1. Buka halaman embed pakai browser otomatis (Playwright/Chromium).
2. Tutup tab iklan yang muncul, lalu "klik play" seperti user biasa.
3. Rekam request jaringan untuk mencari link video (.m3u8 / .mp4).
4. Unduh pakai yt-dlp (otomatis gabung m3u8 jadi mp4 via ffmpeg).
5. Kirim ke Telegram.

Tidak ada bypass captcha / penyamaran fingerprint. Kalau situs menolak, bot melapor gagal.
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------- konfigurasi
BOT_TOKEN = os.environ["BOT_TOKEN"]
# Opsional: batasi siapa yang boleh pakai, contoh "12345,67890". Kosong = semua orang.
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
# Opsional: alamat Local Bot API Server, contoh "http://localhost:8081". Kalau diisi, batas upload 2000 MB.
BOT_API_URL = os.environ.get("BOT_API_URL", "").rstrip("/")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2000" if BOT_API_URL else "50"))
# Video yang lebih besar dari batas tapi <= batas x rasio ini dikompres dulu; selebihnya dipotong.
COMPRESS_RATIO = float(os.environ.get("COMPRESS_RATIO", "1.5"))
MAX_PARTS = int(os.environ.get("MAX_PARTS", "20"))  # batas jumlah potongan per video
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "1"))
SNIFF_TIMEOUT = int(os.environ.get("SNIFF_TIMEOUT", "40"))  # detik menunggu link video
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
LINK_RE = re.compile(r"https?://(?:www\.)?vidmonstr\.com/(?:e|d)/([A-Za-z0-9]+)")
# .m3u8/.mp4 biasa, plus stream.php milik vidmonstr (player-nya mengambil video dari sini lalu
# memutarnya lewat blob:, jadi link ini tidak berakhiran .mp4 dan tidak terlihat di <video>).
VIDEO_RE = re.compile(r"\.(m3u8|mp4)(\?|$)|/stream\.php\?", re.I)
# Selector tombol play yang umum di berbagai player (JW, Video.js, Plyr, dll.)
PLAY_SELECTORS = [
    ".jw-icon-playback", ".jw-display-icon-container", ".vjs-big-play-button",
    ".plyr__control--overlaid", ".play-button", "#play", "button[aria-label*=Play i]",
]

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("vidbot")
sem = asyncio.Semaphore(MAX_PARALLEL)


# ---------------------------------------------------------------- cari link video
class NotFound(RuntimeError):
    """Gagal menemukan video; membawa data diagnosis (screenshot + teks)."""

    def __init__(self, msg: str, screenshot: bytes | None = None, report: str = ""):
        super().__init__(msg)
        self.screenshot = screenshot
        self.report = report


VIDEO_TYPES = ("mpegurl", "video/", "dash+xml")
SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|woff2?|ttf|ico)(\?|$)", re.I)


async def find_video_url(video_id: str) -> dict:
    """Kembalikan {'url', 'referer', 'kind', 'cookies'} atau raise NotFound."""
    page_url = f"https://vidmonstr.com/e/{video_id}"
    cands: list[dict] = []  # semua link yang mungkin video
    seen: list[str] = []  # log request untuk diagnosis
    first_hit: list[float] = []

    def add(url: str, referer: str, how: str):
        if url.startswith(("blob:", "data:")) or any(c["url"] == url for c in cands):
            return
        cands.append({"url": url, "referer": referer or page_url, "how": how})
        if not first_hit:
            first_hit.append(time.monotonic())
        log.info("kandidat (%s): %s", how, url[:200])

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 720})

        def on_request(req):
            if not SKIP_EXT.search(req.url):
                seen.append(f"{req.resource_type[:5]:5} {req.url[:160]}")
            if VIDEO_RE.search(req.url):
                add(req.url, req.headers.get("referer"), "url")

        def on_response(resp):
            ctype = (resp.headers.get("content-type") or "").lower()
            if any(t in ctype for t in VIDEO_TYPES) or resp.request.resource_type == "media":
                add(resp.url, resp.request.headers.get("referer"), f"type {ctype[:30]}")

        ctx.on("request", on_request)
        ctx.on("response", on_response)

        page = await ctx.new_page()

        # Tab iklan yang dibuka otomatis langsung ditutup.
        async def close_popup(new_page):
            if new_page != page:
                try:
                    await new_page.close()
                except Exception:
                    pass

        ctx.on("page", lambda np: asyncio.ensure_future(close_popup(np)))

        try:
            resp = await page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
            status = resp.status if resp else "?"
        except Exception as e:
            await browser.close()
            raise NotFound(f"Gagal membuka halaman: {e}")

        # Buang overlay iklan transparan lalu tampilkan iframe player (sama seperti klik thumbnail).
        try:
            await page.evaluate(
                """() => {
                    document.querySelectorAll('a[style*="position: fixed"], div[style*="opacity: 0.01"]')
                        .forEach(el => el.remove());
                    const link = document.querySelector('.video-link');
                    if (link) link.click();
                    const f = document.getElementById('videq_iframe');
                    if (f) f.style.display = 'block';
                }"""
            )
        except Exception:
            pass

        deadline = time.monotonic() + SNIFF_TIMEOUT
        while time.monotonic() < deadline:
            # setelah kandidat pertama, tunggu 6 detik lagi untuk mengumpulkan kandidat lain
            if first_hit and time.monotonic() - first_hit[0] > 6:
                break
            for frame in page.frames:
                try:
                    for sel in PLAY_SELECTORS:
                        el = await frame.query_selector(sel)
                        if el:
                            await el.click(timeout=1500, force=True)
                            break
                    src = await frame.evaluate(
                        """() => {
                            const v = document.querySelector('video');
                            if (!v) return null;
                            v.muted = true; v.play().catch(()=>{});
                            const s = v.currentSrc || v.src || (v.querySelector('source')||{}).src;
                            return (s && !s.startsWith('blob:')) ? s : null;
                        }"""
                    )
                    if src:
                        add(src, frame.url, "video.src")
                except Exception:
                    pass
            await asyncio.sleep(2)

        # Kumpulkan data diagnosis sebelum browser ditutup (dipakai kalau gagal).
        if True:
            shot = None
            try:
                shot = await page.screenshot(type="jpeg", quality=60)
            except Exception:
                pass
            try:
                title = await page.title()
            except Exception:
                title = "?"
            frames = "\n".join(f"  {f.url[:160]}" for f in page.frames)
            vids = []
            for f in page.frames:
                try:
                    info = await f.evaluate(
                        """() => [...document.querySelectorAll('video')].map(v =>
                              (v.currentSrc || v.src || '(kosong)').slice(0,160))"""
                    )
                    vids += info
                except Exception:
                    pass
            report = (
                f"HTTP status: {status}\nJudul: {title}\n\nFrames:\n{frames}\n\n"
                f"<video> src: {vids or 'tidak ada'}\n\n"
                f"Request terakhir ({len(seen)} total):\n" + "\n".join(seen[-40:])
            )
        cookies = await ctx.cookies()
        await browser.close()

    # Periksa isi tiap kandidat, pilih yang benar-benar video.
    rank = {"hls": 0, "mp4": 1, "webm": 2, "ts": 3}
    probes = []
    for c in cands:
        kind, detail = await asyncio.to_thread(probe, c["url"], c["referer"], cookies)
        c["kind"] = kind
        probes.append(f"[{kind}] ({c['how']}) {c['url'][:200]}\n    {detail}")
    good = sorted((c for c in cands if c["kind"] in rank), key=lambda c: rank[c["kind"]])
    if not good:
        report = "Kandidat yang diperiksa:\n" + ("\n".join(probes) or "  (tidak ada)") + "\n\n" + report
        raise NotFound(
            "Link video tidak ditemukan (mungkin video dihapus, atau situs menolak akses otomatis).",
            shot, report,
        )
    best = good[0]
    log.info("dipilih [%s]: %s", best["kind"], best["url"][:200])
    return {**best, "cookies": cookies, "report": "\n".join(probes)}


def cookie_header(cookies: list, url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    return "; ".join(
        f"{c['name']}={c['value']}" for c in cookies
        if host == c["domain"].lstrip(".") or host.endswith("." + c["domain"].lstrip("."))
    )


def probe(url: str, referer: str, cookies: list) -> tuple[str, str]:
    """Ambil 4 KB pertama dan tebak jenisnya: hls / mp4 / webm / ts / bukan video."""
    headers = {"User-Agent": USER_AGENT, "Referer": referer, "Range": "bytes=0-4095"}
    ck = cookie_header(cookies, url)
    if ck:
        headers["Cookie"] = ck
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            head = r.read(4096)
            ctype = r.headers.get("content-type", "")
            code = r.status
    except Exception as e:
        return "error", str(e)[:200]
    detail = f"HTTP {code}, {ctype}, awal: {head[:80]!r}"
    if head.lstrip(b"\xef\xbb\xbf").startswith(b"#EXTM3U"):
        return "hls", detail
    if head[4:8] == b"ftyp":
        return "mp4", detail
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "webm", detail
    if head[:1] == b"G" and len(head) > 188 and head[188:189] == b"G":
        return "ts", detail
    return "bukan-video", detail


def write_cookie_file(cookies: list, path: Path) -> None:
    """Simpan cookie browser ke format Netscape agar bisa dipakai yt-dlp."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c["domain"]
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(int(c.get("expires", 0)) if c.get("expires", -1) > 0 else 0),
            c["name"],
            c["value"],
        ]))
    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------- unduh
async def download_hls(info: dict, workdir: Path) -> Path:
    """Unduh playlist HLS pakai ffmpeg (-f hls: tetap jalan walau link tanpa .m3u8)."""
    out = workdir / "video_ff.mp4"
    hdr = f"Referer: {info['referer']}\r\n"
    ck = cookie_header(info.get("cookies", []), info["url"])
    if ck:
        hdr += f"Cookie: {ck}\r\n"
    try:
        await run(
            "ffmpeg", "-y", "-v", "error", "-user_agent", USER_AGENT, "-headers", hdr,
            "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-f", "hls", "-i", info["url"],
            "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out),
        )
    except Exception as e:
        raise RuntimeError(f"Unduhan HLS gagal: {str(e)[-400:]}")
    return out


def http_download(info: dict, out: Path) -> None:
    """Unduh file video langsung (dipakai untuk link seperti stream.php yang tidak dikenali yt-dlp)."""
    headers = {"User-Agent": USER_AGENT, "Referer": info["referer"]}
    ck = cookie_header(info.get("cookies", []), info["url"])
    if ck:
        headers["Cookie"] = ck
    req = urllib.request.Request(info["url"], headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r, out.open("wb") as f:
        shutil.copyfileobj(r, f, 1024 * 1024)
    if out.stat().st_size == 0:
        raise RuntimeError("file kosong")


async def download(info: dict, workdir: Path) -> Path:
    kind = info.get("kind")
    if kind == "hls":
        return await download_hls(info, workdir)
    if kind in ("mp4", "webm", "ts"):
        direct = workdir / f"direct.{kind}"
        try:
            await asyncio.to_thread(http_download, info, direct)
            if kind == "mp4":
                return direct
            mp4 = workdir / "direct_remux.mp4"
            try:
                await run("ffmpeg", "-y", "-v", "error", "-i", str(direct), "-c", "copy",
                          "-movflags", "+faststart", str(mp4))
                return mp4
            except Exception as e:
                log.warning("remux ke mp4 gagal, kirim apa adanya: %s", e)
                return direct
        except Exception as e:
            log.warning("unduh langsung gagal, coba yt-dlp: %s", e)
            direct.unlink(missing_ok=True)
    cookie_file = workdir / "cookies.txt"
    write_cookie_file(info.get("cookies", []), cookie_file)
    out_tpl = str(workdir / "video.%(ext)s")
    cmd = [
        "yt-dlp", "--no-playlist", "--quiet", "--no-warnings",
        "--cookies", str(cookie_file),
        "--add-header", f"Referer:{info['referer']}",
        "--add-header", f"User-Agent:{USER_AGENT}",
        "-o", out_tpl, info["url"],
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    files = sorted(f for f in workdir.glob("video.*") if f.stat().st_size > 0)

    if proc.returncode != 0 or not files:
        raise RuntimeError(f"Unduhan gagal: {err.decode(errors='ignore')[-300:]}")

    path = files[0]
    if path.suffix.lower() != ".mp4":
        mp4 = workdir / "video_remux.mp4"
        try:
            await run("ffmpeg", "-y", "-v", "error", "-i", str(path), "-c", "copy",
                      "-movflags", "+faststart", str(mp4))
            return mp4
        except Exception as e:
            log.warning("remux ke mp4 gagal, kirim apa adanya: %s", e)
    return path


# ---------------------------------------------------------------- ukuran besar: kompres / potong
LIMIT_BYTES = MAX_UPLOAD_MB * 1024 * 1024


async def run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} gagal: {err.decode(errors='ignore')[-300:]}")
    return out.decode(errors="ignore")


async def duration_of(path: Path) -> float:
    out = await run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    )
    return float(out.strip())


async def compress(path: Path, workdir: Path) -> Path:
    """Encode ulang agar muat di batas upload (target 92% dari batas)."""
    dur = await duration_of(path)
    audio_kbps = 96
    total_kbps = (LIMIT_BYTES * 0.92 * 8 / 1000) / dur
    video_kbps = int(total_kbps - audio_kbps)
    if video_kbps < 250:  # terlalu kecil -> gambar hancur, lebih baik dipotong
        raise RuntimeError("bitrate terlalu rendah")
    out = workdir / "compressed.mp4"
    await run(
        "ffmpeg", "-y", "-v", "error", "-i", str(path),
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
        "-vf", "scale='min(1280,iw)':-2",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart", str(out),
    )
    return out


async def split(path: Path, workdir: Path) -> list[Path]:
    """Potong tanpa encode ulang. Kalau ada potongan yang masih kebesaran, ulangi dengan durasi lebih pendek."""
    dur = await duration_of(path)
    size = path.stat().st_size
    seg = max(10.0, dur * (LIMIT_BYTES * 0.85) / size)
    for _ in range(4):
        if dur / seg > MAX_PARTS:
            raise RuntimeError(f"Video terlalu besar (lebih dari {MAX_PARTS} bagian).")
        partdir = workdir / "parts"
        shutil.rmtree(partdir, ignore_errors=True)
        partdir.mkdir()
        await run(
            "ffmpeg", "-y", "-v", "error", "-i", str(path),
            "-c", "copy", "-map", "0", "-f", "segment",
            "-segment_time", f"{seg:.2f}", "-reset_timestamps", "1",
            "-segment_format_options", "movflags=+faststart",
            str(partdir / "part%03d.mp4"),
        )
        parts = sorted(partdir.glob("part*.mp4"))
        biggest = max(p.stat().st_size for p in parts)
        if biggest <= LIMIT_BYTES:
            return parts
        seg *= (LIMIT_BYTES * 0.85) / biggest  # perkecil durasi sesuai potongan terbesar
    raise RuntimeError("Gagal memotong video jadi ukuran yang muat.")


async def fit_for_upload(path: Path, workdir: Path, status) -> list[Path]:
    size = path.stat().st_size
    if size <= LIMIT_BYTES:
        return [path]
    mb = size / 1024 / 1024
    if size <= LIMIT_BYTES * COMPRESS_RATIO:
        await status.edit_text(f"🗜️ Video {mb:.0f} MB, sedang dikompres...")
        try:
            small = await compress(path, workdir)
            if small.stat().st_size <= LIMIT_BYTES:
                return [small]
        except Exception as e:
            log.warning("kompres gagal, beralih ke potong: %s", e)
    await status.edit_text(f"✂️ Video {mb:.0f} MB, sedang dipotong...")
    return await split(path, workdir)


# ---------------------------------------------------------------- handler telegram
def allowed(update: Update) -> bool:
    return not ALLOWED or (update.effective_user and update.effective_user.id in ALLOWED)


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Kirim link vidmonstr.com/e/... atau /d/..., nanti aku kirim videonya."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    m = LINK_RE.search(update.message.text or "")
    if not m:
        await update.message.reply_text("Itu bukan link vidmonstr.")
        return

    status = await update.message.reply_text("⏳ Mencari video...")
    workdir = Path(tempfile.mkdtemp(prefix="vid_"))
    info = None
    try:
        async with sem:
            info = await find_video_url(m.group(1))
            await status.edit_text("⬇️ Mengunduh...")
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)
            path = await download(info, workdir)

        parts = await fit_for_upload(path, workdir, status)

        total = len(parts)
        for i, part in enumerate(parts, 1):
            label = f"⬆️ Mengirim bagian {i}/{total}..." if total > 1 else "⬆️ Mengirim..."
            await status.edit_text(label)
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)
            with part.open("rb") as f:
                await update.message.reply_video(
                    f,
                    caption=f"Bagian {i}/{total}" if total > 1 else None,
                    supports_streaming=True,
                    read_timeout=600, write_timeout=600,
                )
        await status.delete()
    except NotFound as e:
        log.warning("tidak ketemu:\n%s", e.report)
        await status.edit_text(f"❌ {e}")
        if e.screenshot:
            await update.message.reply_photo(e.screenshot, caption="Screenshot halaman yang dilihat bot")
        if e.report:
            rpt = Path(workdir) / "diagnosis.txt"
            rpt.write_text(e.report)
            with rpt.open("rb") as f:
                await update.message.reply_document(f, filename="diagnosis.txt")
    except Exception as e:
        log.exception("gagal")
        await status.edit_text(f"❌ {str(e)[:900]}")
        rpt_text = (info or {}).get("report")
        if rpt_text:
            rpt = Path(workdir) / "kandidat.txt"
            rpt.write_text(rpt_text)
            with rpt.open("rb") as f:
                await update.message.reply_document(f, filename="kandidat.txt")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    builder = Application.builder().token(BOT_TOKEN)
    if BOT_API_URL:
        builder = (
            builder.base_url(f"{BOT_API_URL}/bot")
            .base_file_url(f"{BOT_API_URL}/file/bot")
            .local_mode(True)
        )
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("bot jalan (batas upload %d MB%s)", MAX_UPLOAD_MB, ", local API" if BOT_API_URL else "")
    app.run_polling()


if __name__ == "__main__":
    main()
