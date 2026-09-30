"""
Bot Telegram: kirim link vidmonstr.com / fiuosba.com (/e/... atau /d/...) -> bot membalas file videonya.

Cara kerja:
1. Buka halaman embed pakai browser otomatis (Playwright/Chromium).
2. Tutup tab iklan yang muncul, lalu "klik play" seperti user biasa.
3. Rekam request jaringan untuk mencari link video (.m3u8 / .mp4).
4. Unduh pakai yt-dlp (otomatis gabung m3u8 jadi mp4 via ffmpeg).
5. Kirim ke Telegram.

Tidak ada bypass captcha / penyamaran fingerprint. Kalau situs menolak, bot melapor gagal.
"""

import asyncio
import json
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
# Opsional: api_id & api_hash dari https://my.telegram.org. Kalau diisi, video dikirim lewat MTProto
# (Telethon) dengan token bot yang sama, jadi batas upload naik dari 50 MB ke 2000 MB tanpa server tambahan.
API_ID = int(os.environ.get("API_ID") or 0)
API_HASH = os.environ.get("API_HASH", "").strip()
MTPROTO = bool(API_ID and API_HASH) and not BOT_API_URL
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2000" if (BOT_API_URL or MTPROTO) else "50"))
# Video yang lebih besar dari batas tapi <= batas x rasio ini dikompres dulu; selebihnya dipotong.
# Mode MTProto: default 1.0 (tanpa kompres) karena meng-encode video > 2 GB di Heroku terlalu berat.
COMPRESS_RATIO = float(os.environ.get("COMPRESS_RATIO", "1.0" if MTPROTO else "1.5"))
MAX_PARTS = int(os.environ.get("MAX_PARTS", "20"))  # batas jumlah potongan per video
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "1"))
MAX_LINKS = int(os.environ.get("MAX_LINKS", "20"))  # batas jumlah link per pesan
SNIFF_TIMEOUT = int(os.environ.get("SNIFF_TIMEOUT", "40"))  # detik menunggu link video
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
# Tampilkan info teknis video (ukuran, rasio piksel, rotasi) di caption. Set VIDEO_INFO=0 untuk mematikan.
VIDEO_INFO = os.environ.get("VIDEO_INFO", "1") != "0"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
# Situs yang dikenali (satu player yang sama, beda domain). Tambah domain lain lewat Config Var
# SITES di Heroku, pisahkan dengan koma, mis. "vidmonstr.com,fiuosba.com,situslain.com".
SITES = [d.strip().lower().removeprefix("www.") for d in
         os.environ.get("SITES", "vidmonstr.com,fiuosba.com").split(",") if d.strip()]
LINK_RE = re.compile(
    r"https?://(?:www\.)?(" + "|".join(re.escape(d) for d in SITES) + r")/(?:e|d)/([A-Za-z0-9]+)",
    re.I,
)
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


# Header yang tidak boleh ikut disalin saat meniru request browser.
DROP_HEADERS = {"host", "content-length", "range", "accept-encoding", "connection"}
BODY_TIMEOUT = int(os.environ.get("BODY_TIMEOUT", "300"))  # detik menunggu browser selesai memuat video
# Isi respons browser dibaca utuh ke RAM. Di atas batas ini jangan diambil dari browser (dyno Heroku 512 MB
# bisa crash); video diunduh ulang langsung ke disk.
BODY_MAX_MB = int(os.environ.get("BODY_MAX_MB", "100"))


async def grab_from_browser(c: dict, workdir: Path) -> None:
    """Salin header asli request browser, dan kalau respons browser berisi video, simpan isinya.

    Beberapa server (mis. stream.php vidmonstr) hanya memberi video ke request yang persis seperti
    dari player, atau tokennya sekali pakai. Isi yang sudah diterima browser adalah jalan paling aman.
    """
    req = c.get("req")
    if not req:
        return
    try:
        hdrs = await req.all_headers()
        c["headers"] = {k: v for k, v in hdrs.items()
                        if not k.startswith(":") and k.lower() not in DROP_HEADERS}
        c["referer"] = hdrs.get("referer") or c["referer"]
    except Exception as e:
        log.info("gagal membaca header request: %s", e)
    try:
        resp = await req.response()
        if not resp:
            c["browser"] = "tanpa respons"
            return
        ctype = resp.headers.get("content-type", "")
        c["browser"] = f"HTTP {resp.status}, {ctype}"
        try:
            clen = int(resp.headers.get("content-length") or 0)
        except ValueError:
            clen = 0
        if clen > BODY_MAX_MB * 1024 * 1024:
            c["browser"] += f", {clen / 1024 / 1024:.0f} MB, terlalu besar untuk RAM, diunduh ulang"
            return
        body = await asyncio.wait_for(resp.body(), BODY_TIMEOUT)
        kind = sniff(body[:4096])
        c["browser"] += f", {len(body) / 1024 / 1024:.1f} MB, isi: {kind}"
        if kind in ("mp4", "webm", "ts"):
            f = workdir / f"browser.{kind}"
            f.write_bytes(body)
            c["body_file"] = str(f)
            c["kind"] = kind
        elif kind == "hls":
            c["kind"] = "hls"
    except Exception as e:
        c["browser"] = c.get("browser", "") + f", isi tidak bisa diambil: {str(e)[:150]}"


GET_COVER_JS = """() => {
    const v = document.querySelector('video[poster]');
    if (v && v.poster) return v.poster;
    const i = document.querySelector('.video-link img, img.thumbnail, .plyr__poster, .thumbnail');
    if (i) {
        if (i.currentSrc || i.src) return i.currentSrc || i.src;
        const m = (getComputedStyle(i).backgroundImage || '').match(/url\\(["']?(.*?)["']?\\)/);
        if (m) return m[1];
    }
    const og = document.querySelector('meta[property="og:image"]');
    return og ? og.content : null;
}"""


async def grab_cover(ctx, frames, workdir: Path) -> Path | None:
    """Unduh gambar cover/thumbnail yang tampil di halaman video (pakai cookie browser)."""
    for f in frames:
        try:
            url = await f.evaluate(GET_COVER_JS)
        except Exception:
            continue
        if not url or not url.startswith("http"):
            continue
        try:
            r = await ctx.request.get(url, headers={"referer": f.url}, timeout=15_000)
            body = await r.body() if r.ok else b""
        except Exception as e:
            log.info("gagal mengunduh cover %s: %s", url[:120], e)
            continue
        if len(body) > 500:
            out = workdir / "cover_src"
            out.write_bytes(body)
            log.info("cover: %s (%d KB)", url[:120], len(body) // 1024)
            return out
    return None


# ---------------------------------------------------------------- situs dengan API /api/stream
# Sebagian situs (mis. fiuosba.com, player "Vidara") tidak menaruh link video di halaman. Player-nya
# memanggil POST /api/stream {"filecode": ID} dan mendapat JSON berisi "streaming_url". Bot langsung
# memanggil API itu (lebih cepat dan tidak ketahuan sebagai browser otomatis). Kalau tidak berhasil,
# bot tetap lanjut ke cara lama lewat browser.
def api_stream(host: str, video_id: str) -> tuple[dict | None, str]:
    """Kembalikan (data JSON atau None, catatan untuk diagnosis)."""
    page_url = f"https://{host}/e/{video_id}"
    body = json.dumps({"filecode": video_id, "device": "web"}).encode()
    req = urllib.request.Request(
        f"https://{host}/api/stream", data=body, method="POST",
        headers={
            "user-agent": USER_AGENT, "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "origin": f"https://{host}", "referer": page_url,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read(200000)
            code = r.status
    except Exception as e:
        return None, f"POST /api/stream gagal: {str(e)[:200]}"
    note = f"POST /api/stream -> HTTP {code}, isi: {raw[:600].decode('utf-8', 'replace')}"
    try:
        data = json.loads(raw)
    except Exception:
        return None, note + "\n(bukan JSON, mungkin terenkripsi)"
    return (data if isinstance(data, dict) else None), note


def fetch_file(url: str, referer: str, out: Path) -> Path | None:
    try:
        req = urllib.request.Request(url, headers={"user-agent": USER_AGENT, "referer": referer})
        with urllib.request.urlopen(req, timeout=20) as r, out.open("wb") as f:
            shutil.copyfileobj(r, f)
        return out if out.stat().st_size > 0 else None
    except Exception:
        return None


async def find_via_api(host: str, video_id: str, workdir: Path) -> tuple[dict | None, str]:
    data, note = await asyncio.to_thread(api_stream, host, video_id)
    url = (data or {}).get("streaming_url") or ""
    if not url.startswith("http"):
        return None, note
    page_url = f"https://{host}/e/{video_id}"
    c = {"url": url, "referer": page_url, "how": "api /api/stream"}
    kind, detail = await asyncio.to_thread(probe, c, [])
    if kind not in ("hls", "mp4", "webm", "ts"):
        # isi tidak bisa ditebak dari 4 KB pertama: tebak dari alamatnya
        kind = "hls" if ".m3u8" in url else "mp4" if ".mp4" in url else kind
    if kind not in ("hls", "mp4", "webm", "ts"):
        return None, f"{note}\nstreaming_url: {url[:200]}\n  diperiksa: [{kind}] {detail}"
    cover = None
    thumb = data.get("thumbnail") or ""
    if thumb.startswith("http"):
        cover = await asyncio.to_thread(fetch_file, thumb, page_url, workdir / "cover_api.jpg")
    log.info("video lewat API [%s]: %s", kind, url[:200])
    return {**c, "kind": kind, "cookies": [], "report": f"[{kind}] (api) {url[:200]}",
            "cover_file": str(cover) if cover else None}, note


async def find_video_url(video_id: str, workdir: Path, host: str = "vidmonstr.com") -> dict:
    """Kembalikan {'url', 'referer', 'kind', 'cookies'} atau raise NotFound."""
    page_url = f"https://{host}/e/{video_id}"
    api_info, api_note = await find_via_api(host, video_id, workdir)
    if api_info:
        return api_info
    cands: list[dict] = []  # semua link yang mungkin video
    seen: list[str] = []  # log request untuk diagnosis
    first_hit: list[float] = []

    def add(url: str, referer: str, how: str, req=None):
        if url.startswith(("blob:", "data:")):
            return
        # Hanya bukti kuat (respons bertipe video, atau src <video>) yang membuat bot berhenti menunggu.
        # Link yang cuma cocok pola URL (mis. stream.php) bisa saja halaman player, bukan videonya.
        strong = how != "url"
        if strong and not first_hit:
            first_hit.append(time.monotonic())
        for c in cands:
            if c["url"] == url:
                if req and not c.get("req"):
                    c["req"] = req
                return
        cands.append({"url": url, "referer": referer or page_url, "how": how, "req": req})
        log.info("kandidat (%s): %s", how, url[:200])

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 720})

        def on_request(req):
            if not SKIP_EXT.search(req.url):
                seen.append(f"{req.resource_type[:5]:5} {req.url[:160]}")
            if VIDEO_RE.search(req.url):
                add(req.url, req.headers.get("referer"), "url", req)

        api_seen: list[str] = []

        async def read_api(resp):
            # respons /api/stream yang diterima player di browser (cadangan kalau panggilan langsung gagal)
            try:
                data = await resp.json()
                api_seen.append(json.dumps(data)[:600])
                url = (data or {}).get("streaming_url") or ""
                if url.startswith("http"):
                    add(url, page_url, "api (browser)")
            except Exception as e:
                api_seen.append(f"(gagal dibaca: {e})")

        def on_response(resp):
            if resp.url.split("?", 1)[0].endswith("/api/stream"):
                asyncio.ensure_future(read_api(resp))
            ctype = (resp.headers.get("content-type") or "").lower()
            if any(t in ctype for t in VIDEO_TYPES) or resp.request.resource_type == "media":
                add(resp.url, resp.request.headers.get("referer"), f"type {ctype[:30]}", resp.request)

        ctx.on("request", on_request)
        ctx.on("response", on_response)

        page = await ctx.new_page()
        keep: list = []  # halaman player yang sengaja dibuka bot (jangan ditutup)

        # Tab iklan yang dibuka otomatis langsung ditutup.
        async def close_popup(new_page):
            await asyncio.sleep(0.5)
            if new_page != page and new_page not in keep:
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

        # Klik tombol play (thumbnail .video-link) di SEMUA frame lewat JavaScript, jadi tidak
        # kena lapisan iklan transparan. Di vidmonstr tombol ini ada di dalam frame player.
        CLICK_THUMB = """() => {
            document.querySelectorAll('a[style*="position: fixed"], div[style*="opacity: 0.01"]')
                .forEach(el => el.remove());
            const link = document.querySelector('.video-link');
            if (link) link.click();
            const f = document.getElementById('videq_iframe');
            if (f) f.style.display = 'block';
            return !!link;
        }"""

        def all_frames():
            for pg in [page, *keep]:
                if not pg.is_closed():
                    yield from pg.frames

        def player_url() -> str | None:
            if any("/stream.php?" in f.url for f in all_frames()):
                return None  # player sudah terbuka sebagai frame
            for c in cands:
                if "/stream.php?" in c["url"]:
                    return c["url"]
            return None

        player_referer = page_url
        opened_player = False
        deadline = time.monotonic() + SNIFF_TIMEOUT
        rounds = 0
        while time.monotonic() < deadline:
            # setelah bukti kuat pertama, tunggu 6 detik lagi untuk mengumpulkan kandidat lain
            if first_hit and time.monotonic() - first_hit[0] > 6:
                break
            rounds += 1
            if rounds <= 3:
                for frame in list(all_frames()):
                    if host not in frame.url:
                        continue
                    try:
                        if await frame.evaluate(CLICK_THUMB) and frame != page.main_frame:
                            player_referer = frame.url
                    except Exception:
                        pass
            # Kalau player belum muncul juga, buka halaman player stream.php langsung
            # (itu yang dilakukan tombol play), dengan referer frame player.
            if rounds == 4 and not first_hit and not opened_player:
                url = player_url()
                if url:
                    opened_player = True
                    try:
                        pg = await ctx.new_page()
                        keep.append(pg)
                        await pg.goto(url, referer=player_referer, wait_until="domcontentloaded",
                                      timeout=30_000)
                        log.info("membuka player langsung: %s", url[:120])
                    except Exception as e:
                        log.warning("gagal membuka player: %s", e)
            for frame in list(all_frames()):
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
                shot_page = keep[-1] if keep and not keep[-1].is_closed() else page
                shot = await shot_page.screenshot(type="jpeg", quality=60)
            except Exception:
                pass
            try:
                title = await page.title()
            except Exception:
                title = "?"
            frames = "\n".join(f"  {f.url[:160]}" for f in all_frames())
            vids = []
            for f in all_frames():
                try:
                    info = await f.evaluate(
                        """() => [...document.querySelectorAll('video')].map(v =>
                              (v.currentSrc || v.src || '(kosong)').slice(0,160))"""
                    )
                    vids += info
                except Exception:
                    pass
            # Isi HTML tiap frame milik vidmonstr (untuk melihat player apa yang dipakai).
            doms = []
            for f in all_frames():
                if host not in f.url and not f.url.startswith("blob:"):
                    continue
                try:
                    body = await f.evaluate("() => document.body ? document.body.innerHTML : ''")
                    doms.append(f"--- {f.url[:120]}\n{body[:4000]}")
                except Exception as e:
                    doms.append(f"--- {f.url[:120]}\n(gagal dibaca: {e})")
            own = [s for s in seen if host in s or s.split(" ", 1)[0] in ("media", "other")]
            report = (
                f"HTTP status: {status}\nJudul: {title}\n\nFrames:\n{frames}\n\n"
                f"<video> src: {vids or 'tidak ada'}\n\n"
                f"Request ke {host} / media ({len(own)}):\n" + "\n".join(own[-40:]) + "\n\n"
                f"Request terakhir ({len(seen)} total):\n" + "\n".join(seen[-40:]) + "\n\n"
                "Isi frame:\n" + ("\n\n".join(doms) or "(tidak ada)")
            )
        # Ambil header asli + isi respons dari browser untuk tiap kandidat (sebelum browser ditutup).
        for c in cands:
            await grab_from_browser(c, workdir)
        cover_file = await grab_cover(ctx, list(all_frames()), workdir)
        cookies = await ctx.cookies()
        await browser.close()

    # Periksa isi tiap kandidat, pilih yang benar-benar video.
    rank = {"hls": 0, "mp4": 1, "webm": 2, "ts": 3}
    probes = []
    for c in cands:
        c.pop("req", None)
        browser_note = f"\n    browser: {c['browser']}" if c.get("browser") else ""
        if c.get("body_file"):
            probes.append(f"[{c['kind']}] ({c['how']}) {c['url'][:200]}{browser_note}\n    diambil dari browser")
            continue
        kind, detail = await asyncio.to_thread(probe, c, cookies)
        c["kind"] = kind
        probes.append(f"[{kind}] ({c['how']}) {c['url'][:200]}{browser_note}\n    unduh ulang: {detail}")
    # utamakan yang isinya sudah ada di tangan
    good = sorted((c for c in cands if c["kind"] in rank),
                  key=lambda c: (0 if c.get("body_file") else 1, rank[c["kind"]]))
    if not good:
        report = (
            "Kandidat yang diperiksa:\n" + ("\n".join(probes) or "  (tidak ada)") + "\n\n"
            + f"API langsung: {api_note}\n"
            + ("API di browser: " + " | ".join(api_seen) + "\n" if api_seen else "")
            + "\n" + report
        )
        raise NotFound(
            "Link video tidak ditemukan (mungkin video dihapus, atau situs menolak akses otomatis).",
            shot, report,
        )
    best = good[0]
    log.info("dipilih [%s]: %s", best["kind"], best["url"][:200])
    return {**best, "cookies": cookies, "report": "\n".join(probes),
            "cover_file": str(cover_file) if cover_file else None}


def cookie_header(cookies: list, url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    return "; ".join(
        f"{c['name']}={c['value']}" for c in cookies
        if host == c["domain"].lstrip(".") or host.endswith("." + c["domain"].lstrip("."))
    )


def request_headers(info: dict, cookies: list) -> dict:
    """Header untuk mengunduh ulang: pakai header asli browser kalau ada, plus cookie."""
    headers = dict(info.get("headers") or {})
    headers.setdefault("user-agent", USER_AGENT)
    headers.setdefault("referer", info["referer"])
    ck = cookie_header(cookies, info["url"])
    if ck:
        headers["cookie"] = ck
    return headers


def sniff(head: bytes) -> str:
    if head.lstrip(b"\xef\xbb\xbf").startswith(b"#EXTM3U"):
        return "hls"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "webm"
    if head[:1] == b"G" and len(head) > 188 and head[188:189] == b"G":
        return "ts"
    return "bukan-video"


def probe(c: dict, cookies: list) -> tuple[str, str]:
    """Ambil 4 KB pertama dan tebak jenisnya: hls / mp4 / webm / ts / bukan video."""
    headers = request_headers(c, cookies)
    headers["range"] = "bytes=0-4095"
    try:
        with urllib.request.urlopen(urllib.request.Request(c["url"], headers=headers), timeout=20) as r:
            head = r.read(20000)
            ctype = r.headers.get("content-type", "")
            code = r.status
    except Exception as e:
        return "error", str(e)[:200]
    kind = sniff(head[:4096])
    detail = f"HTTP {code}, {ctype}, awal: {head[:80]!r}"
    if "html" in ctype.lower() and any(d in c["url"] for d in SITES):
        # halaman dari vidmonstr sendiri: simpan isinya, mungkin berisi link video aslinya
        detail += "\n    ---- isi halaman ----\n" + head.decode("utf-8", "replace")[:6000] + "\n    ----"
    return kind, detail


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
    headers = request_headers(info, info.get("cookies", []))
    req = urllib.request.Request(info["url"], headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r, out.open("wb") as f:
        shutil.copyfileobj(r, f, 1024 * 1024)
    if out.stat().st_size == 0:
        raise RuntimeError("file kosong")
    with out.open("rb") as f:
        if sniff(f.read(4096)) == "bukan-video":
            raise RuntimeError("server mengirim halaman, bukan video")


async def to_mp4(src: Path, workdir: Path) -> Path:
    if src.suffix.lower() == ".mp4":
        return src
    mp4 = workdir / f"{src.stem}_remux.mp4"
    try:
        await run("ffmpeg", "-y", "-v", "error", "-i", str(src), "-c", "copy",
                  "-movflags", "+faststart", str(mp4))
        return mp4
    except Exception as e:
        log.warning("remux ke mp4 gagal, kirim apa adanya: %s", e)
        return src


async def download(info: dict, workdir: Path) -> Path:
    kind = info.get("kind")
    if info.get("body_file"):  # isi video sudah diterima browser
        return await to_mp4(Path(info["body_file"]), workdir)
    if kind == "hls":
        return await download_hls(info, workdir)
    if kind in ("mp4", "webm", "ts"):
        direct = workdir / f"direct.{kind}"
        try:
            await asyncio.to_thread(http_download, info, direct)
            return await to_mp4(direct, workdir)
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


async def probe_video(path: Path) -> dict:
    """Info stream video pertama + format, via ffprobe -show_streams (jalan di ffmpeg 4.x maupun baru)."""
    out = await run(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_streams", "-show_format", "-of", "json", str(path),
    )
    data = json.loads(out)
    st = (data.get("streams") or [{}])[0]
    rot = st.get("tags", {}).get("rotate")
    for sd in st.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = sd["rotation"]
    st["_rotation"] = int(float(rot)) if rot not in (None, "") else 0
    st["_duration"] = float(data.get("format", {}).get("duration") or st.get("duration") or 0)
    return st


async def video_meta(path: Path) -> dict:
    """Lebar & tinggi TAMPILAN (memperhitungkan rotasi dan rasio piksel) serta durasi, untuk Telegram.
    Tanpa ini, aplikasi Telegram di HP menebak ukuran sendiri dan videonya bisa tampil gepeng."""
    try:
        st = await probe_video(path)
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        sar = st.get("sample_aspect_ratio") or "1:1"
        try:
            n, d = (int(x) for x in sar.split(":"))
            if n > 0 and d > 0 and n != d:
                w = int(round(w * n / d))
        except ValueError:
            pass
        if abs(st["_rotation"]) % 180 == 90:
            w, h = h, w
        dur = int(round(st["_duration"]))
        return {"width": w or None, "height": h or None, "duration": dur or None}
    except Exception as e:
        log.warning("gagal membaca ukuran video: %s", e)
        return {}


async def tech_info(path: Path) -> str:
    """Ringkasan teknis satu baris, untuk melacak masalah tampilan (gepeng, terbalik, dll.)."""
    try:
        st = await probe_video(path)
        mb = path.stat().st_size / 1024 / 1024
        return (f"📐 {st.get('width')}×{st.get('height')} · SAR {st.get('sample_aspect_ratio', '?')} · "
                f"DAR {st.get('display_aspect_ratio', '?')} · rotasi {st['_rotation']} · "
                f"{st.get('codec_name', '?')} · {mb:.1f} MB")
    except Exception as e:
        return f"📐 info gagal: {str(e)[:200]}"


async def make_cover(src: Path, workdir: Path) -> tuple[Path | None, Path | None]:
    """Dari gambar cover situs: cover JPEG (sisi terpanjang maks 1280) + thumbnail kecil (maks 320)."""
    cover, thumb = workdir / "cover.jpg", workdir / "cover_thumb.jpg"
    try:
        await run("ffmpeg", "-y", "-v", "error", "-i", str(src), "-frames:v", "1",
                  "-vf", "scale='if(gte(iw,ih),min(1280,iw),-2)':'if(gte(iw,ih),-2,min(1280,ih))'",
                  "-q:v", "3", str(cover))
        await run("ffmpeg", "-y", "-v", "error", "-i", str(src), "-frames:v", "1",
                  "-vf", "scale='if(gte(iw,ih),320,-2)':'if(gte(iw,ih),-2,320)'", "-q:v", "5", str(thumb))
    except Exception as e:
        log.warning("gagal membuat cover: %s", e)
        return None, None
    return (cover if cover.exists() else None), (thumb if thumb.exists() else None)


async def make_thumb(path: Path, out: Path) -> Path | None:
    """Thumbnail kecil (maks 320 px) agar pratinjau di Telegram tidak gepeng."""
    try:
        await run(
            "ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(path), "-frames:v", "1",
            "-vf", "scale='if(gte(iw,ih),320,-2)':'if(gte(iw,ih),-2,320)'", "-q:v", "5", str(out),
        )
        return out if out.exists() and out.stat().st_size > 0 else None
    except Exception:
        return None


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
        "-vf", "scale='if(gte(iw,ih),min(1280,iw),-2)':'if(gte(iw,ih),-2,min(1280,ih))'",
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


async def fix_aspect(path: Path, workdir: Path, status) -> Path:
    """Video dengan piksel tidak persegi (SAR != 1:1) tampil benar di browser, tapi aplikasi
    Telegram mengabaikan SAR sehingga gambarnya gepeng. Ubah jadi piksel persegi."""
    try:
        out = await run(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=sample_aspect_ratio", "-of", "default=nw=1:nk=1", str(path),
        )
        sar = out.strip().splitlines()[0] if out.strip() else "1:1"
        num, den = (int(x) for x in sar.split(":"))
    except Exception:
        return path
    if num <= 0 or den <= 0 or num == den:
        return path
    log.info("SAR %s, diperbaiki ke 1:1", sar)
    await status.edit_text("🛠️ Memperbaiki rasio gambar...")
    fixed = workdir / "aspect.mp4"
    try:
        await run(
            "ffmpeg", "-y", "-v", "error", "-i", str(path),
            "-vf", "scale='trunc(iw*sar/2)*2':'trunc(ih/2)*2',setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy", "-movflags", "+faststart", str(fixed),
        )
        return fixed
    except Exception as e:
        log.warning("gagal memperbaiki rasio: %s", e)
        return path


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


# ---------------------------------------------------------------- kirim lewat MTProto (sampai 2 GB)
tg = None  # klien Telethon, diisi di post_init kalau API_ID & API_HASH ada


async def start_mtproto(_app) -> None:
    global tg
    if not MTPROTO:
        return
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # Sesi di memori: login ulang tiap dyno restart, tidak perlu file sesi.
    # Update tetap diterima agar Telethon mengenal user/grup yang mengirim link.
    tg = TelegramClient(StringSession(), API_ID, API_HASH)
    await tg.start(bot_token=BOT_TOKEN)
    me = await tg.get_me()
    log.info("MTProto aktif sebagai @%s (upload sampai %d MB)", me.username, MAX_UPLOAD_MB)


async def stop_mtproto(_app) -> None:
    if tg:
        await tg.disconnect()


async def send_mtproto(update: Update, part: Path, caption: str | None, thumb: Path | None,
                       meta: dict, status, label: str) -> None:
    from telethon.tl.types import DocumentAttributeVideo

    chat_id = update.effective_chat.id
    entity = None
    for _ in range(5):  # beri waktu Telethon mencatat user dari update terbaru
        try:
            entity = await tg.get_input_entity(chat_id)
            break
        except ValueError:
            await asyncio.sleep(1)
    if entity is None:
        raise RuntimeError("MTProto tidak mengenali chat ini. Kirim link sekali lagi.")

    last = [0.0]

    async def progress(done: int, total: int):
        now = time.monotonic()
        if now - last[0] < 5 or not total:
            return
        last[0] = now
        try:
            await status.edit_text(f"{label} {done * 100 // total}%")
        except Exception:
            pass

    await tg.send_file(
        entity, str(part),
        caption=caption,
        thumb=str(thumb) if thumb else None,
        supports_streaming=True,
        attributes=[DocumentAttributeVideo(
            duration=meta.get("duration") or 0,
            w=meta.get("width") or 0, h=meta.get("height") or 0,
            supports_streaming=True,
        )],
        reply_to=update.message.message_id,
        progress_callback=progress,
    )


# ---------------------------------------------------------------- handler telegram
def allowed(update: Update) -> bool:
    return not ALLOWED or (update.effective_user and update.effective_user.id in ALLOWED)


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Kirim link {' / '.join(SITES)} (format /e/... atau /d/...), nanti aku kirim videonya.\n"
        "Bisa juga banyak link sekaligus dalam satu pesan (pisahkan dengan spasi atau baris baru)."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    # Ambil SEMUA link di pesan (boleh dipisah spasi, baris baru, dll.), buang yang dobel.
    ids = list(dict.fromkeys((m.group(1).lower(), m.group(2)) for m in LINK_RE.finditer(update.message.text or "")))
    if not ids:
        await update.message.reply_text("Link tidak dikenali. Situs yang didukung: " + ", ".join(SITES))
        return
    if len(ids) > MAX_LINKS:
        await update.message.reply_text(
            f"⚠️ Ada {len(ids)} link, yang diproses hanya {MAX_LINKS} pertama "
            f"(batas MAX_LINKS). Kirim sisanya di pesan berikutnya."
        )
        ids = ids[:MAX_LINKS]

    total = len(ids)
    gagal = []
    for n, (host, video_id) in enumerate(ids, 1):
        prefix = f"[{n}/{total}] " if total > 1 else ""
        try:
            ok = await process_one(update, context, video_id, prefix, host)
        except Exception:
            # jangan sampai satu link yang error menghentikan link-link berikutnya
            log.exception("link %s gagal total", video_id)
            ok = False
        if not ok:
            gagal.append(n)

    if total > 1:
        if gagal:
            await update.message.reply_text(
                f"Selesai: {total - len(gagal)}/{total} berhasil. "
                f"Gagal: link ke-{', '.join(map(str, gagal))}."
            )
        else:
            await update.message.reply_text(f"✅ Selesai, {total} video terkirim.")


async def process_one(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      video_id: str, prefix: str = "", host: str = "vidmonstr.com") -> bool:
    """Proses satu link. Mengembalikan True kalau berhasil."""
    status = await update.message.reply_text(f"{prefix}⏳ Mencari video...")
    workdir = Path(tempfile.mkdtemp(prefix="vid_"))
    info = None
    try:
        async with sem:
            info = await find_video_url(video_id, workdir, host)
            await status.edit_text(f"{prefix}⬇️ Mengunduh...")
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)
            path = await download(info, workdir)

        info_asli = await tech_info(path) if VIDEO_INFO else ""
        path = await fix_aspect(path, workdir, status)
        parts = await fit_for_upload(path, workdir, status)

        cover_img = cover_thumb = None
        if (info or {}).get("cover_file"):
            cover_img, cover_thumb = await make_cover(Path(info["cover_file"]), workdir)

        total = len(parts)
        for i, part in enumerate(parts, 1):
            label = prefix + (f"⬆️ Mengirim bagian {i}/{total}..." if total > 1 else "⬆️ Mengirim...")
            await status.edit_text(label)
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)
            meta = await video_meta(part)
            thumb = cover_thumb if (i == 1 and cover_thumb) else await make_thumb(part, workdir / f"thumb{i}.jpg")
            cap = ["ini milik @aiviral2"]
            if tg:
                await send_mtproto(update, part, "\n".join(cap) or None, thumb, meta, status, label)
                continue
            kwargs = dict(
                caption="\n".join(cap) or None,
                supports_streaming=True,
                width=meta.get("width"), height=meta.get("height"),
                duration=meta.get("duration"),
                thumbnail=thumb,
                read_timeout=600, write_timeout=600,
            )
            if cover_img and i == 1:
                kwargs["cover"] = cover_img  # tampilan awal video (Bot API 8.3+)
            try:
                await update.message.reply_video(part, **kwargs)
            except TypeError:
                # versi python-telegram-bot lama belum kenal 'cover'
                kwargs.pop("cover", None)
                await update.message.reply_video(part, **kwargs)
        await status.delete()
        return True
    except NotFound as e:
        log.warning("tidak ketemu:\n%s", e.report)
        await status.edit_text(f"{prefix}❌ {e}")
        if e.screenshot:
            await update.message.reply_photo(e.screenshot, caption="Screenshot halaman yang dilihat bot")
        if e.report:
            rpt = Path(workdir) / "diagnosis.txt"
            rpt.write_text(e.report)
            with rpt.open("rb") as f:
                await update.message.reply_document(f, filename="diagnosis.txt")
    except Exception as e:
        log.exception("gagal")
        await status.edit_text(f"{prefix}❌ {str(e)[:900]}")
        rpt_text = (info or {}).get("report")
        if rpt_text:
            rpt = Path(workdir) / "kandidat.txt"
            rpt.write_text(rpt_text)
            with rpt.open("rb") as f:
                await update.message.reply_document(f, filename="kandidat.txt")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return False


def main():
    builder = Application.builder().token(BOT_TOKEN).post_init(start_mtproto).post_shutdown(stop_mtproto)
    if BOT_API_URL:
        builder = (
            builder.base_url(f"{BOT_API_URL}/bot")
            .base_file_url(f"{BOT_API_URL}/file/bot")
            .local_mode(True)
        )
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("bot jalan (batas upload %d MB%s)", MAX_UPLOAD_MB,
             ", local API" if BOT_API_URL else ", MTProto" if MTPROTO else "")
    app.run_polling()


if __name__ == "__main__":
    main()
