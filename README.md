# DKPP_AUTO GitHub Actions Runner (Template)

Template ini meniru logika app Android kamu untuk endpoint eKinerja Barito Utara:

Urutan:
1) AUTH initial (token/akun = null) -> refresh cookie `ekinbarut=...`
2) LOGIN (user, pass, imei/device_id_16, dname/device_name)
3) AUTH (token, akun)
4) INFODAY (cek ket & jadwal)
5) SNDLOC (stimo=1 masuk, stimo=2 pulang) jika jadwal valid
6) INFODAY lagi

## Setup
1. Copy file ini ke repo baru (atau tambah ke repo yang ada).
2. Tambahkan Secrets di GitHub repo:
   - `EKIN_USER` : username
   - `EKIN_PASS` : password
   - `DEVICE_NAME` : contoh `POCO F7` (optional; default "Android")
   - `DEVICE_ID_16` : optional kalau mau fix; kalau kosong akan auto-generate & disimpan di cache
   - `NTFY_TOPIC_URL` : optional, contoh `https://ntfy.sh/topikmu`

3. Pastikan ada `koordinat.csv` di root repo **atau** project android lengkap sehingga file ada di `app/src/main/res/raw/koordinat.csv`.
   - Format mengikuti app: header `lat,lang`

## Jadwal acak dalam window
Workflow dipanggil tiap 5 menit, tetapi `runner.py` memilih waktu target acak 1x per hari per tipe (MASUK/PULANG)
dalam window WIB:
- MASUK  : 06:15:00 - 06:30:00
- PULANG : 16:00:00 - 17:00:00

Untuk menambah jumlah eksekusi dalam 1 window (misal 2x), ubah env:
- `MASUK_TARGET_COUNT`
- `PULANG_TARGET_COUNT`

## Anti tabrakan / tahan banting
- `concurrency` mencegah workflow yang sama overlap.
- `lock.json` mencegah proses ganda jika GitHub memanggil job berdekatan / retry.
- State per hari disimpan di `.state/`.

Catatan: cache GitHub Actions dipakai untuk menyimpan `.cache` dan `.state`.


## Multi akun (dkpp1, dkpp2, dst) tanpa bentrok
Cara paling gampang:
1) Duplikat file workflow: `.github/workflows/dkpp_auto.yml` menjadi `dkpp1.yml`, `dkpp2.yml`, dst.
2) Di masing-masing file, ubah env secrets menjadi berbeda, misalnya:
   - dkpp1.yml pakai `secrets.DKPP1_USER` dan `secrets.DKPP1_PASS`
   - dkpp2.yml pakai `secrets.DKPP2_USER` dan `secrets.DKPP2_PASS`
3) Semua workflow tetap **antri** karena memakai `concurrency.group: dkpp-global-queue` dan `cancel-in-progress: false`.

## Device ID & Device Name
Edit langsung di `runner.py`:
- `DEVICE_ID_16` (16 karakter) dan `DEVICE_NAME` (contoh: `POCO F7`).
Nilai ini harus stabil agar server tidak menganggap device baru.
