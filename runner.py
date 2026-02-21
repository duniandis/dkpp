# runner.py
import os, json, time, random, csv, re
from pathlib import Path
from datetime import datetime, date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================== Endpoints ==================
LOGIN_URL   = "https://ekinerja.baritoutarakab.go.id/api/presentapp/login"
AUTH_URL    = "https://ekinerja.baritoutarakab.go.id/api/presentapp/auth"
SNDLOC_URL  = "https://ekinerja.baritoutarakab.go.id/api/presentapp/sndloc"
INFODAY_URL = "https://ekinerja.baritoutarakab.go.id/api/presentapp/infoday"

ACCEPT_HEADER = "application/json, text/plain, */*"
USER_AGENT = "okhttp/4.12.0"

# ================== RUN KEY (pisah akun) ==================
RUN_KEY = os.getenv("RUN_KEY", "default").strip() or "default"

CACHE_DIR = Path(".cache") / RUN_KEY
STATE_DIR = Path(".state") / RUN_KEY
CACHE_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

COOKIE_FILE = CACHE_DIR / "cookie_ekin.json"
COORD_FILE  = CACHE_DIR / "coord.json"
LOCK_FILE   = STATE_DIR / "lock.json"

# ================== Waktu ==================
def tz_now_wib() -> datetime:
    # WIB (UTC+7)
    return datetime.utcnow() + timedelta(hours=7)

def parse_time_to_sec(t: str) -> int:
    h, m, s = [int(x) for x in t.split(":")]
    return h * 3600 + m * 60 + s

def in_window(now_local: datetime, start: str, end: str) -> bool:
    sec = now_local.hour * 3600 + now_local.minute * 60 + now_local.second
    return parse_time_to_sec(start) <= sec <= parse_time_to_sec(end)

# ================== NTFY ==================
def ntfy(title: str, message: str, priority: str = "default"):
    url = os.getenv("NTFY_TOPIC_URL", "").strip()
    if not url:
        return
    try:
        requests.post(
            url,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=15,
        )
    except Exception:
        pass

# ================== JSON IO ==================
def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default

def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

# ================== Lock ==================
def acquire_lock(max_age_seconds: int = 20 * 60) -> bool:
    now = int(time.time())
    cur = load_json(LOCK_FILE, {})
    if isinstance(cur, dict) and cur.get("ts"):
        age = now - int(cur["ts"])
        if age < max_age_seconds:
            return False
    save_json(LOCK_FILE, {"ts": now, "run_id": os.getenv("GITHUB_RUN_ID", "")})
    return True

def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass

# ================== Identity ==================
def get_device_identity_from_env():
    device_id = os.getenv("DEVICE_ID_16", "").strip()
    device_name = os.getenv("DEVICE_NAME", "").strip()
    if not device_id or len(device_id) != 16:
        raise RuntimeError("ENV DEVICE_ID_16 wajib 16 karakter.")
    if not device_name:
        raise RuntimeError("ENV DEVICE_NAME wajib diisi.")
    return device_id, device_name

# ================== Cookies (persist) ==================
def load_cookie_value() -> str | None:
    data = load_json(COOKIE_FILE, {})
    c = (data.get("cookie") or "").strip()
    return c or None

def save_cookie_value(cookie_value: str | None):
    save_json(COOKIE_FILE, {"cookie": cookie_value or ""})

def set_session_cookie(session: requests.Session, cookie_value: str | None):
    if not cookie_value or "=" not in cookie_value:
        return
    name, val = cookie_value.split("=", 1)
    session.cookies.set(name, val)

def extract_ekin_cookie_from_session(session: requests.Session) -> str | None:
    for c in session.cookies:
        if c.name == "ekinbarut":
            return f"{c.name}={c.value}"
    return None

# ================== Session + Retry ==================
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def base_headers():
    return {
        "Accept": ACCEPT_HEADER,
        "User-Agent": USER_AGENT,
        "Connection": "Keep-Alive",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    }

def post_form(session: requests.Session, url: str, data: dict):
    return session.post(url, data=data, headers=base_headers(), timeout=30)

# ================== State (per-hari) ==================
def state_path_for(day: date, key: str) -> Path:
    return STATE_DIR / f"{day.isoformat()}_{key}.json"

def load_state(day: date, key: str) -> dict:
    return load_json(state_path_for(day, key), {}) or {}

def save_state(day: date, key: str, st: dict):
    save_json(state_path_for(day, key), st)

def already_done(day: date, key: str, target_count: int) -> bool:
    st = load_state(day, key)
    done = int(st.get("done_count", 0) or 0)
    return done >= target_count

def mark_done(day: date, key: str, ok: bool, msg: str, infoday_obj: dict | None):
    st = load_state(day, key)
    st["done_count"] = int(st.get("done_count", 0) or 0) + 1
    st["last_run_at"] = tz_now_wib().isoformat(sep=" ", timespec="seconds")
    st["last_result_ok"] = bool(ok)
    st["last_message"] = msg
    st["last_infoday_raw"] = infoday_obj
    save_state(day, key, st)

def was_notified(day: date, key: str) -> bool:
    st = load_state(day, key)
    return bool(st.get("notified", False))

def mark_notified(day: date, key: str):
    st = load_state(day, key)
    st["notified"] = True
    st["notified_at"] = tz_now_wib().isoformat(sep=" ", timespec="seconds")
    save_state(day, key, st)

# ================== Koordinat ==================
def load_coords_from_csv(path: str = "koordinat.csv"):
    p = Path(path)
    if not p.exists():
        alt = Path("app/src/main/res/raw/koordinat.csv")
        if alt.exists():
            p = alt
        else:
            raise FileNotFoundError("koordinat.csv tidak ditemukan.")

    coords = []
    with p.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lat = row.get("lat") or row.get("Lat") or row.get("LAT")
            lng = row.get("lang") or row.get("lng") or row.get("lon") or row.get("Lang") or row.get("LNG")
            if lat is None or lng is None:
                continue
            try:
                coords.append((float(lat), float(lng)))
            except Exception:
                continue

    if not coords:
        raise RuntimeError("Isi koordinat.csv kosong / format salah (butuh kolom lat,lang).")
    return coords

def get_or_pick_coord():
    cur = load_json(COORD_FILE, {})
    lat = cur.get("lat")
    lng = cur.get("lng")
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return lat, lng

    coords = load_coords_from_csv()
    lat, lng = random.choice(coords)
    save_json(COORD_FILE, {"lat": lat, "lng": lng})
    return lat, lng

# ================== API wrappers ==================
def auth_initial(session: requests.Session):
    return post_form(session, AUTH_URL, {"token": "null", "akun": "null"})

def login(session: requests.Session, user: str, pw: str, imei: str, dname: str):
    r = post_form(session, LOGIN_URL, {"user": user, "pass": pw, "imei": imei, "dname": dname})
    try:
        obj = r.json()
    except Exception:
        return False, "Login respon tidak valid", None, None

    status = int(obj.get("status", 0) or 0)
    msg = str(obj.get("msg", "") or "")

    if status == 1:
        return True, "Login berhasil", str(obj.get("token", "") or ""), str(obj.get("akun", "") or "")
    return False, f"Login gagal: {msg}", None, None

def auth(session: requests.Session, token: str, akun: str):
    r = post_form(session, AUTH_URL, {"token": token, "akun": akun})
    try:
        obj = r.json()
        status = int(obj.get("status", 0) or 0)
        return (status == 1), ("AUTH ok" if status == 1 else "AUTH gagal")
    except Exception:
        return False, "AUTH respon tidak valid"

def infoday_raw(session: requests.Session, token: str, akun: str):
    r = post_form(session, INFODAY_URL, {"token": token, "akun": akun})
    if not r.ok:
        return False, {"error": f"HTTP {r.status_code}"}
    try:
        return True, r.json()
    except Exception:
        return False, {"error": "JSON parse error"}

def sndloc(session: requests.Session, token: str, akun: str, lat: float, lng: float, type_name: str):
    stimo = 1 if type_name == "MASUK" else 2
    r = post_form(session, SNDLOC_URL, {
        "token": token,
        "akun": akun,
        "lat": str(lat),
        "lang": str(lng),
        "stimo": str(stimo),
        "status": "1",
    })
    ok_send = False
    msg_send = "SNDLOC respon tidak valid"
    try:
        obj = r.json()
        ret = int(obj.get("ret", 0) or 0)
        nama = str(obj.get("nama", "") or "")
        ok_send = (ret == 1)
        msg_send = (f"Berhasil: {nama}" if ok_send else f"Gagal: {nama}")
    except Exception:
        pass
    return ok_send, msg_send

# ================== INFODAY parsing ==================
TIME_RE = re.compile(r"(\d{2}:\d{2}:\d{2})")

def extract_infodata(obj: dict) -> dict:
    data = obj.get("data")
    if not isinstance(data, dict):
        data = {}
    return {
        "today": str(obj.get("today", "-") or "-"),
        "nama": str(data.get("nama", "-") or "-"),
        "nip": str(data.get("nip", "-") or "-"),
        "jmasuk": str(data.get("jmasuk", "") or ""),
        "jpulang": str(data.get("jpulang", "") or ""),
        "masuk": str(data.get("masuk", "-") or "-"),
        "pulang": str(data.get("pulang", "-") or "-"),
        "ket": str(data.get("ket", "") or ""),
        "isma": str(data.get("isma", "") or ""),
        "raw": obj,
    }

def is_marked(value: str) -> bool:
    v = (value or "").strip()
    return bool(v) and v != "-"

def looks_like_libur(info: dict) -> bool:
    jmasuk = (info.get("jmasuk") or "").strip()
    jpulang = (info.get("jpulang") or "").strip()

    # kalau tidak mengandung jam sama sekali => libur / tidak terjadwal
    if not TIME_RE.search(jmasuk) or not TIME_RE.search(jpulang):
        blob = " ".join([
            (info.get("today") or ""),
            jmasuk,
            jpulang,
            (info.get("ket") or ""),
        ]).lower()
        keywords = [
            "hari libur", "libur",
            "tidak terjadwal", "belum terjadual",
            "belum dijadwalkan", "tidak dijadwalkan",
            "cuti",
        ]
        if any(k in blob for k in keywords):
            return True
        # tetap anggap libur/invalid kalau tidak ada jam
        return True

    # kalau ada jam, bukan libur
    return False

def parse_window_from_infoday(info: dict) -> tuple[str, str, str, str] | None:
    jm = TIME_RE.findall(info.get("jmasuk", "") or "")
    jp = TIME_RE.findall(info.get("jpulang", "") or "")
    if len(jm) >= 2 and len(jp) >= 2:
        return jm[0], jm[1], jp[0], jp[1]
    return None

# ================== KET rule ==================
def ket_is_dinas_skip(info: dict) -> tuple[bool, str]:
    ket = (info.get("ket") or "").strip()
    # rule lama: kalau ket >2 char dianggap dinas/laporan => skip absen
    if len(ket) > 2:
        return True, ket
    # kadang isi "-" atau "----"
    if ket and all(ch == "-" for ch in ket):
        return False, ""
    return False, ""

# ================== Filter sukses + format NTFY ==================
def is_real_success(ok_send: bool, msg: str) -> bool:
    """
    NTFY hari kerja untuk hasil sndloc:
    - ok_send True
    - msg diawali "Berhasil:"
    - msg mengandung "DINAS"
    - msg TIDAK mengandung "terkendala"/"hubungi administrator"/"kendala"
    """
    t = (msg or "").strip().lower()
    if not ok_send:
        return False
    if not t.startswith("berhasil:"):
        return False
    if "dinas" not in t:
        return False
    bad = ["terkendala", "hubungi administrator", "administrator", "kendala"]
    if any(b in t for b in bad):
        return False
    return True

def fmt_infoday_pretty(info: dict) -> str:
    today  = info.get("today", "-")
    nama   = info.get("nama", "-")
    nip    = info.get("nip", "-")
    jmasuk = (info.get("jmasuk") or "-").strip() or "-"
    jpulang= (info.get("jpulang") or "-").strip() or "-"
    masuk  = info.get("masuk", "-")
    pulang = info.get("pulang", "-")
    ket    = (info.get("ket") or "").strip()

    lines = [
        f"Today  : {today}",
        f"Nama   : {nama}",
        f"NIP    : {nip}",
        f"Jadwal : Masuk {jmasuk} | Pulang {jpulang}",
        f"Status : Masuk {masuk} | Pulang {pulang}",
    ]
    if ket:
        lines.append(f"Ket    : {ket}")
    return "\n".join(lines)

def notify_dinas_once(day: date, now: datetime, device_name: str, device_id: str, ket: str, info: dict):
    # key unik agar 1x/hari per akun
    key = "DINAS_KET"
    if was_notified(day, key):
        return
    title = f"DKPP_AUTO {RUN_KEY} DINAS"
    body = [
        f"Akun  : {RUN_KEY}",
        f"Device: {device_name} ({device_id})",
        f"Waktu : {now.isoformat(sep=' ', timespec='seconds')}",
        f"Ket   : {ket}",
        "",
        fmt_infoday_pretty(info),
    ]
    ntfy(title, "\n".join(body), priority="default")
    mark_notified(day, key)

def notify_libur_once(day: date, now: datetime, device_name: str, device_id: str, info: dict, label: str = "LIBUR"):
    key = "LIBUR"
    if was_notified(day, key):
        return
    title = f"DKPP_AUTO {RUN_KEY} {label}"
    body = [
        f"Akun  : {RUN_KEY}",
        f"Device: {device_name} ({device_id})",
        f"Waktu : {now.isoformat(sep=' ', timespec='seconds')}",
        "",
        fmt_infoday_pretty(info),
    ]
    ntfy(title, "\n".join(body), priority="default")
    mark_notified(day, key)

# ================== run_once (jalan sndloc bila perlu) ==================
def run_once(type_name: str):
    user = os.getenv("EKIN_USER", "").strip()
    pw   = os.getenv("EKIN_PASS", "").strip()
    if not user or not pw:
        raise RuntimeError("Secrets EKIN_USER / EKIN_PASS belum diset.")

    device_id, device_name = get_device_identity_from_env()
    lat, lng = get_or_pick_coord()

    session = make_session()
    set_session_cookie(session, load_cookie_value())

    # AUTH initial
    auth_initial(session)
    save_cookie_value(extract_ekin_cookie_from_session(session))

    # LOGIN
    ok, msg, token, akun = login(session, user, pw, device_id, device_name)
    save_cookie_value(extract_ekin_cookie_from_session(session))
    if not ok or not token or not akun:
        return False, msg, None, device_id, device_name

    # AUTH
    ok2, msg2 = auth(session, token, akun)
    save_cookie_value(extract_ekin_cookie_from_session(session))
    if not ok2:
        return False, msg2, None, device_id, device_name

    # INFODAY sebelum
    ok3, info_before_obj = infoday_raw(session, token, akun)
    save_cookie_value(extract_ekin_cookie_from_session(session))
    if not ok3:
        return False, f"INFODAY error: {info_before_obj}", info_before_obj, device_id, device_name

    info_before = extract_infodata(info_before_obj)

    # KET dinas: skip sndloc + notif 1x/hari
    skip, ket = ket_is_dinas_skip(info_before)
    if skip:
        now = tz_now_wib()
        notify_dinas_once(now.date(), now, device_name, device_id, ket, info_before)
        return True, f"SKIP DINAS: {ket}", info_before_obj, device_id, device_name

    # Kalau sudah marked, stop tanpa sndloc
    if type_name == "MASUK" and is_marked(info_before.get("masuk", "-")):
        return True, f"SKIP: sudah MASUK ({info_before.get('masuk')})", info_before_obj, device_id, device_name
    if type_name == "PULANG" and is_marked(info_before.get("pulang", "-")):
        return True, f"SKIP: sudah PULANG ({info_before.get('pulang')})", info_before_obj, device_id, device_name

    # SNDLOC
    ok_send, msg_send = sndloc(session, token, akun, lat, lng, type_name)
    save_cookie_value(extract_ekin_cookie_from_session(session))

    # INFODAY setelah sndloc
    ok4, info_after_obj = infoday_raw(session, token, akun)
    save_cookie_value(extract_ekin_cookie_from_session(session))
    if not ok4:
        return ok_send, f"{msg_send} | INFODAY_AFTER error: {info_after_obj}", info_before_obj, device_id, device_name

    return ok_send, msg_send, info_after_obj, device_id, device_name

# ================== Probe (login-auth-infoday TANPA sndloc) ==================
def probe_infoday():
    device_id, device_name = get_device_identity_from_env()
    user = os.getenv("EKIN_USER", "").strip()
    pw   = os.getenv("EKIN_PASS", "").strip()
    if not user or not pw:
        raise RuntimeError("Secrets EKIN_USER / EKIN_PASS belum diset.")

    session = make_session()
    set_session_cookie(session, load_cookie_value())

    auth_initial(session)
    save_cookie_value(extract_ekin_cookie_from_session(session))

    ok, msg, token, akun = login(session, user, pw, device_id, device_name)
    save_cookie_value(extract_ekin_cookie_from_session(session))
    if not ok or not token or not akun:
        return False, f"Login gagal: {msg}", None, device_id, device_name

    ok2, msg2 = auth(session, token, akun)
    save_cookie_value(extract_ekin_cookie_from_session(session))
    if not ok2:
        return False, f"AUTH gagal: {msg2}", None, device_id, device_name

    ok3, info_obj = infoday_raw(session, token, akun)
    save_cookie_value(extract_ekin_cookie_from_session(session))
    if not ok3:
        return False, f"INFODAY gagal: {info_obj}", None, device_id, device_name

    return True, "OK", info_obj, device_id, device_name

# ================== MAIN ==================
def main():
    now = tz_now_wib()
    today = now.date()
    
    # Kalau hari ini sudah pernah notif LIBUR / DINAS_KET, stop total (jangan login lagi)
    if was_notified(today, "LIBUR"):
        print(f"[{now}] LIBUR sudah terdeteksi hari ini ({RUN_KEY}). Skip login/probe.")
        return
    
    if was_notified(today, "DINAS_KET"):
        print(f"[{now}] DINAS_KET sudah terdeteksi hari ini ({RUN_KEY}). Skip login/probe.")
        return

    masuk_target  = int(os.getenv("MASUK_TARGET_COUNT", "1"))
    pulang_target = int(os.getenv("PULANG_TARGET_COUNT", "1"))

    if not acquire_lock():
        print(f"[{now}] Lock aktif. Exit.")
        return

    try:
        # jitter kecil biar tidak “nabrak” persis
        time.sleep(random.randint(2, 10))

        # ===== Probe infoday untuk window dinamis / libur / ket =====
        okp, msgp, info_obj, device_id, device_name = probe_infoday()
        if not okp or not isinstance(info_obj, dict):
            print(f"[{now}] PROBE gagal: {msgp}")
            return

        info = extract_infodata(info_obj)

        # 1) KET dinas => notif 1x/hari, exit (tidak sndloc)
        skip, ket = ket_is_dinas_skip(info)
        if skip:
            notify_dinas_once(today, now, device_name, device_id, ket, info)
            print(f"[{now}] DINAS via KET. Exit.")
            return

        # 2) Libur / tidak terjadwal => notif 1x/hari, exit
        if looks_like_libur(info):
            notify_libur_once(today, now, device_name, device_id, info, label="LIBUR")
            print(f"[{now}] Hari libur / tidak terjadwal. Exit.")
            return

        # 3) Window dinamis dari infoday
        win = parse_window_from_infoday(info)
        if not win:
            notify_libur_once(today, now, device_name, device_id, info, label="LIBUR/INVALID")
            print(f"[{now}] Window tidak valid (jadwal tidak terbaca). Exit.")
            return

        masuk_start, masuk_end, pulang_start, pulang_end = win

        # ===== Tentukan task berdasar INFODAY window dinamis =====
        tasks = []
        if in_window(now, masuk_start, masuk_end) and not already_done(today, "MASUK", masuk_target):
            tasks.append("MASUK")
        if in_window(now, pulang_start, pulang_end) and not already_done(today, "PULANG", pulang_target):
            tasks.append("PULANG")

        if not tasks:
            print(f"[{now}] Di luar window dinamis / sudah DONE. Exit.")
            return

        # ===== Eksekusi task =====
        for tname in tasks:
            ok_send, msg_send, infoday_obj, device_id2, device_name2 = run_once(tname)

            # simpan last info state (biar kelihatan)
            st = load_state(today, tname)
            st["last_run_at"] = tz_now_wib().isoformat(sep=" ", timespec="seconds")
            st["last_message"] = msg_send
            st["last_ok_send"] = bool(ok_send)
            st["last_infoday_raw"] = infoday_obj
            save_state(today, tname, st)

            # Kalau run_once stop karena KET dinas => sudah notif dinas dan return
            # Kita lanjut ke task lain saja.
            if (msg_send or "").startswith("SKIP DINAS:"):
                print(f"{tname}: {msg_send}")
                continue

            # NTFY hari kerja: hanya jika real success (Berhasil + DINAS + bukan terkendala)
            if is_real_success(ok_send, msg_send):
                # notif hanya 1x/hari per MASUK/PULANG per akun
                if not was_notified(today, tname):
                    info_after = extract_infodata(infoday_obj if isinstance(infoday_obj, dict) else {})
                    title = f"DKPP_AUTO {RUN_KEY} {tname} OK"
                    body = [
                        f"Akun  : {RUN_KEY}",
                        f"Device: {device_name2} ({device_id2})",
                        f"Action: {tname}",
                        f"Hasil : {msg_send}",
                        f"Waktu : {tz_now_wib().isoformat(sep=' ', timespec='seconds')}",
                        "",
                        fmt_infoday_pretty(info_after),
                    ]
                    ntfy(title, "\n".join(body), priority="default")
                    mark_notified(today, tname)

                # DONE hanya jika real success
                mark_done(today, tname, True, msg_send, infoday_obj)
                print(f"{tname}: DONE - {msg_send}")
            else:
                # selain real success:
                # - jika "terkendala" => tidak notif, tidak done (biar retry)
                # - jika "SKIP: sudah MASUK/PULANG" => tidak notif, tidak done (aman)
                print(f"{tname}: NO-DONE - {msg_send}")

    finally:
        release_lock()

if __name__ == "__main__":
    main()
