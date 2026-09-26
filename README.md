# Bot Telegram pengunduh video vidmonstr

Kirim link `vidmonstr.com/e/...` atau `/d/...` ke bot → bot membalas file videonya.

## Tes dulu di laptop (paling disarankan)

1. Install Python 3.10+ dan [ffmpeg](https://ffmpeg.org/download.html) (pastikan `ffmpeg` bisa dipanggil dari terminal).
2. Di folder ini:
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```
3. Jalankan (ganti token dari @BotFather):
   - Windows (PowerShell): `$env:BOT_TOKEN="123:ABC"; python bot.py`
   - Linux/Mac: `BOT_TOKEN=123:ABC python bot.py`
4. Kirim link vidmonstr ke bot-mu.

Kalau gagal "Link video tidak ditemukan", jalankan dengan `HEADLESS=0` supaya jendela browser kelihatan
dan kamu bisa lihat apa yang terjadi, lalu kirim pesan error/log-nya.

## Variabel lingkungan

| Nama | Wajib | Keterangan |
|---|---|---|
| `BOT_TOKEN` | ya | Token dari @BotFather |
| `ALLOWED_USER_IDS` | tidak | ID Telegram yang boleh pakai, pisah koma. Kosong = semua orang |
| `BOT_API_URL` | tidak | Alamat Local Bot API Server (mis. `http://localhost:8081`). Kalau diisi, batas upload jadi 2000 MB |
| `MAX_UPLOAD_MB` | tidak | Default 50 (atau 2000 kalau `BOT_API_URL` diisi) |
| `COMPRESS_RATIO` | tidak | Video sampai batas × rasio ini dikompres, di atasnya dipotong. Default 1.5 |
| `MAX_PARTS` | tidak | Maksimal jumlah potongan per video, default 20 |
| `MAX_PARALLEL` | tidak | Jumlah unduhan bersamaan, default 1 |
| `SNIFF_TIMEOUT` | tidak | Detik menunggu link video, default 40 |
| `HEADLESS` | tidak | `0` = tampilkan browser (untuk debug) |

## Deploy ke Heroku

Playwright butuh Chromium, jadi pakai Docker:
```
heroku stack:set container -a NAMA_APP
```
Buat `heroku.yml`:
```yaml
build:
  docker:
    worker: Dockerfile
```
Lalu set `BOT_TOKEN` di Config Vars, push, dan nyalakan dyno **worker** (bukan web).
Butuh RAM minimal ~512 MB; dyno Eco/Basic bisa pas-pasan saat Chromium jalan.

## Video besar

- **≤ 50 MB**: dikirim langsung.
- **50–75 MB**: dikompres (maks 720p) supaya jadi satu file di bawah 50 MB. Kalau gagal, dipotong.
- **> 75 MB**: dipotong jadi beberapa bagian < 50 MB tanpa encode ulang, dikirim berurutan "Bagian 1/3", dst.

### Opsi: satu file utuh sampai 2 GB (Local Bot API Server, cocok untuk VPS)

1. Ambil `api_id` dan `api_hash` di https://my.telegram.org.
2. Jalankan server resmi Telegram:
   ```
   docker run -d -p 8081:8081 -v /var/lib/telegram-bot-api:/var/lib/telegram-bot-api \
     -e TELEGRAM_API_ID=xxx -e TELEGRAM_API_HASH=yyy aiogram/telegram-bot-api:latest
   ```
3. Sekali saja, lepaskan bot dari server resmi: buka
   `https://api.telegram.org/bot<TOKEN>/logOut` di browser.
4. Jalankan bot dengan `BOT_API_URL=http://localhost:8081`.

## Batasan

- Tanpa Local Bot API Server, file dikirim maks 50 MB per bagian.
- Kalau vidmonstr mengubah player atau mewajibkan captcha, bot akan gagal — tidak ada bypass captcha.
