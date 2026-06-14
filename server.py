#!/usr/bin/env python3
"""AI 웰페이퍼 공모전 서버 (Python 3 표준 라이브러리)"""

import json
import io
import os
import base64
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
import socket
import hashlib
import hmac
import threading
import time
import zipfile
import zlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote, quote, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

PORT = int(os.environ.get("PORT", 3000))
_BASE      = os.path.dirname(__file__)
DATA_FILE  = os.path.join(_BASE, "data.json")
UPLOAD_DIR = os.path.join(_BASE, "public", "uploads")
DOCS_DIR   = os.path.join(_BASE, "public", "docs")
PUBLIC_DIR = os.path.join(_BASE, "public")
LOCAL_TOOLS_DIR = os.path.join(tempfile.gettempdir(), "wallpaper-contest-tools")
PDFKIT_TOOL_JS = os.path.join(_BASE, "tools", "pdfkit_tool.js")

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css",
    ".js":   "application/javascript",
    ".json": "application/json",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".bmp":  "image/bmp",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
    ".pdf":  "application/pdf",
}
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
ALLOWED_DOC_EXTS   = {".hwp", ".hwpx", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".zip", ".md", ".html", ".htm"}
DOC_UPLOAD_LIMIT_MB = 150
DOC_UPLOAD_LIMIT_BYTES = DOC_UPLOAD_LIMIT_MB * 1024 * 1024
LOCAL_TOOL_UPLOAD_LIMIT_MB = 200
LOCAL_TOOL_UPLOAD_LIMIT_BYTES = LOCAL_TOOL_UPLOAD_LIMIT_MB * 1024 * 1024
LOCAL_JOB_TTL_SECONDS = 6 * 60 * 60

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DOCS_DIR,   exist_ok=True)
os.makedirs(LOCAL_TOOLS_DIR, exist_ok=True)

# ── 비밀번호 해싱 ──
PW_SALT = "wallpaper-contest-v1"

def hash_password(pw):
    return "sha256:" + hashlib.sha256((pw + PW_SALT).encode()).hexdigest()

def check_password(pw, stored):
    if stored.startswith("sha256:"):
        return hash_password(pw) == stored
    return pw == stored

# ── Cloudflare R2 클라이언트 ──
def env_first(*names, default=""):
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return default

def env_present(*names):
    return any(bool(os.environ.get(name, "").strip()) for name in names)

R2_ACCOUNT_ID = env_first("R2_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID")
R2_ACCESS_KEY = env_first("R2_ACCESS_KEY_ID", "R2_ACCESS_KEY")
R2_SECRET     = env_first("R2_SECRET_ACCESS_KEY", "R2_SECRET_KEY")
R2_BUCKET     = env_first("R2_BUCKET_NAME", "R2_BUCKET", default="wallpaper-contest")
_r2_last_error = ""

def _r2_config_error():
    checks = [
        ("R2_ACCOUNT_ID", R2_ACCOUNT_ID),
        ("R2_ACCESS_KEY_ID", R2_ACCESS_KEY),
        ("R2_SECRET_ACCESS_KEY", R2_SECRET),
        ("R2_BUCKET_NAME", R2_BUCKET),
    ]
    for name, value in checks:
        if value and any(ord(ch) > 127 for ch in value):
            return f"{name} 값에 한글/한자/특수문자가 섞여 있습니다. Cloudflare에서 복사한 영문·숫자 키만 넣어주세요."
    if R2_ACCOUNT_ID and not re.fullmatch(r"[0-9a-fA-F]{32}", R2_ACCOUNT_ID):
        return "R2_ACCOUNT_ID 형식이 올바르지 않습니다. Cloudflare Account ID는 보통 32자리 영문/숫자 조합입니다."
    if R2_BUCKET and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", R2_BUCKET):
        return "R2_BUCKET_NAME 형식이 올바르지 않습니다. 버킷 이름은 영문 소문자, 숫자, 점, 하이픈만 사용할 수 있습니다."
    return ""

def _get_r2():
    return all([R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET, R2_BUCKET]) and not _r2_config_error()

def _r2_signing_key(date_stamp):
    key = ("AWS4" + R2_SECRET).encode("utf-8")
    for msg in [date_stamp, "auto", "s3", "aws4_request"]:
        key = hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()
    return key

def _r2_request(method, key="", data=b"", content_type="application/octet-stream", query=None):
    if not _get_r2():
        return None, (_r2_config_error() or "R2 environment variables are missing").encode("utf-8")
    query = query or {}
    host = f"{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    payload = data or b""
    payload_hash = hashlib.sha256(payload).hexdigest()
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    object_path = f"/{R2_BUCKET}" + (f"/{key}" if key else "")
    canonical_uri = quote(object_path, safe="/-_.~")
    canonical_query = "&".join(
        f"{quote(str(k), safe='-_.~')}={quote(str(v), safe='-_.~')}"
        for k, v in sorted(query.items())
    )
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join([
        method,
        canonical_uri,
        canonical_query,
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    scope = f"{date_stamp}/auto/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        _r2_signing_key(date_stamp),
        string_to_sign.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    authorization = (
        "AWS4-HMAC-SHA256 "
        f"Credential={R2_ACCESS_KEY}/{scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    url = f"https://{host}{canonical_uri}"
    if canonical_query:
        url += "?" + canonical_query
    headers = {
        "Authorization": authorization,
        "X-Amz-Content-Sha256": payload_hash,
        "X-Amz-Date": amz_date,
    }
    if method == "PUT":
        headers["Content-Type"] = content_type
    try:
        req = Request(url, data=payload if method in ["PUT"] else None, headers=headers, method=method)
        with urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = str(e).encode("utf-8")
        return e.code, body
    except URLError as e:
        return None, str(e).encode("utf-8")
    except Exception as e:
        return None, str(e).encode("utf-8")

def r2_upload(key, data, content_type="application/octet-stream"):
    global _r2_last_error
    if not _get_r2():
        _r2_last_error = "R2 환경변수가 부족합니다."
        return False
    status, body = _r2_request("PUT", key=key, data=data, content_type=content_type)
    if status in [200, 201]:
        _r2_last_error = ""
        return True
    _r2_last_error = f"{status} {body[:500].decode('utf-8', errors='replace')}"
    print(f"[R2 업로드 오류] {key}: {_r2_last_error}")
    return False

def r2_download(key):
    if not _get_r2():
        return None
    status, body = _r2_request("GET", key=key)
    return body if status == 200 else None

def r2_delete(key):
    if not _get_r2():
        return False
    status, _ = _r2_request("DELETE", key=key)
    return status in [200, 204]

def r2_list(prefix):
    if not _get_r2():
        return []
    status, body = _r2_request("GET", query={"list-type": "2", "prefix": prefix})
    if status != 200:
        print(f"[R2 목록 오류] {status} {body[:300]!r}")
        return []
    root = ET.fromstring(body)
    return [el.text for el in root.iter() if el.tag.endswith("Key") and el.text]

def r2_configured():
    return bool(_get_r2())

def r2_env_status():
    return {
        "R2_ACCOUNT_ID": env_present("R2_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID"),
        "R2_ACCESS_KEY_ID": env_present("R2_ACCESS_KEY_ID", "R2_ACCESS_KEY"),
        "R2_SECRET_ACCESS_KEY": env_present("R2_SECRET_ACCESS_KEY", "R2_SECRET_KEY"),
        "R2_BUCKET_NAME": env_present("R2_BUCKET_NAME", "R2_BUCKET"),
    }

def r2_effective_status():
    return {
        "accountId": bool(R2_ACCOUNT_ID),
        "accessKey": bool(R2_ACCESS_KEY),
        "secretKey": bool(R2_SECRET),
        "bucketName": bool(R2_BUCKET),
    }

def r2_health():
    config_error = _r2_config_error()
    if config_error:
        return False, config_error
    if not _get_r2():
        missing = []
        if not R2_ACCOUNT_ID:
            missing.append("R2_ACCOUNT_ID 없음")
        if not R2_ACCESS_KEY:
            missing.append("R2_ACCESS_KEY_ID 없음")
        if not R2_SECRET:
            missing.append("R2_SECRET_ACCESS_KEY 없음")
        if not R2_BUCKET:
            missing.append("R2_BUCKET_NAME 없음")
        return False, ", ".join(missing) or "R2 설정 미확인"
    status, body = _r2_request("GET", query={"list-type": "2", "max-keys": "1"})
    if status == 200:
        return True, ""
    return False, f"R2 연결 실패: {status} {body[:160].decode('utf-8', errors='replace')}"

def init_data_file():
    if os.path.exists(DATA_FILE):
        return
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "works": [],
            "sessions": {},
            "adminPassword": hash_password("admin1234")
        }, f, ensure_ascii=False, indent=2)

def sync_from_r2():
    """서버 시작 시 R2 → 로컬 동기화 (Railway 재시작 후 데이터 복원)"""
    ok, err = r2_health()
    if not ok:
        if _get_r2():
            print(f"[R2] 연결 테스트 실패: {err}")
        return
    print("[R2] 데이터 동기화 시작...")

    # data.json 복원
    raw = r2_download("data.json")
    if raw:
        with open(DATA_FILE, "wb") as f:
            f.write(raw)
        print("[R2] data.json 복원 완료")

    # uploads 복원
    count = 0
    for key in r2_list("uploads/"):
        fname = os.path.basename(key)
        if not fname:
            continue
        fpath = os.path.join(UPLOAD_DIR, fname)
        if not os.path.exists(fpath):
            raw = r2_download(key)
            if raw:
                with open(fpath, "wb") as f:
                    f.write(raw)
                count += 1
    if count:
        print(f"[R2] uploads {count}개 복원 완료")

    # docs 복원
    count = 0
    for key in r2_list("docs/"):
        fname = os.path.basename(key)
        if not fname:
            continue
        fpath = os.path.join(DOCS_DIR, fname)
        if not os.path.exists(fpath):
            raw = r2_download(key)
            if raw:
                with open(fpath, "wb") as f:
                    f.write(raw)
                count += 1
    if count:
        print(f"[R2] docs {count}개 복원 완료")
    print("[R2] 동기화 완료")

# ── 동시 쓰기 방지 락 ──
data_lock = threading.Lock()

def load_data():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # R2 백업. 데이터 파일은 작아서 즉시 저장 성공 여부를 확인한다.
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode()
    r2_upload("data.json", raw, "application/json")

def save_data_checked(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if r2_configured():
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode()
        return r2_upload("data.json", raw, "application/json")
    return True

def modify_data(fn):
    with data_lock:
        data = load_data()
        result = fn(data)
        save_data(data)
        return result

def modify_data_checked(fn):
    with data_lock:
        data = load_data()
        result = fn(data)
        ok = save_data_checked(data)
        return result, ok

# ── 세션 헬퍼 ──
def get_nickname_by_token(data, session_token):
    for nickname, session in data.get("sessions", {}).items():
        if session.get("sessionToken") == session_token:
            return nickname
    return None

# ── 마이그레이션 ──
def migrate_data():
    with data_lock:
        data = load_data()
        changed = False
        if data.get("adminPassword") and not data["adminPassword"].startswith("sha256:"):
            data["adminPassword"] = hash_password(data["adminPassword"])
            changed = True
        if "sessions" not in data:
            data["sessions"] = {}
            changed = True
        old_votes = data.get("votes", {})
        if old_votes:
            for key, val in old_votes.items():
                if isinstance(val, list):
                    nick, votes = key, val
                elif isinstance(val, dict) and "voterName" in val:
                    nick, votes = val.get("voterName", "").strip(), val.get("votes", [])
                else:
                    continue
                if nick and nick not in data["sessions"]:
                    data["sessions"][nick] = {"sessionToken": str(uuid.uuid4()), "votes": votes}
            data["votes"] = {}
            changed = True
        for work in data.get("works", []):
            if "uploaderNickname" not in work:
                work["uploaderNickname"] = None
                changed = True
            for old_key in ["uploaderToken"]:
                if old_key in work:
                    del work[old_key]
                    changed = True
        if changed:
            save_data(data)

# ── Rate Limiting ──
_rate_store: dict = {}
_rate_lock = threading.Lock()

def check_rate_limit(ip, endpoint, max_req, window_sec):
    key = (ip, endpoint)
    now = time.time()
    with _rate_lock:
        ts = [t for t in _rate_store.get(key, []) if now - t < window_sec]
        if len(ts) >= max_req:
            _rate_store[key] = ts
            return False
        ts.append(now)
        _rate_store[key] = ts
    return True

# ── 유틸 ──
def sanitize(val, max_len=100):
    return str(val or "").strip()[:max_len]

def safe_download_name(work):
    ext = os.path.splitext(work.get("filename", ""))[1].lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        ext = ".jpg"
    raw = f"{work.get('author', '')}_{work.get('title', '')}".strip(" _")
    cleaned = "".join("_" if ch in '\\/:*?"<>|\r\n\t' else ch for ch in raw).strip(" ._")
    return (cleaned[:80] or "wallpaper") + ext

def get_vote_count(data, work_id):
    return sum(1 for s in data.get("sessions", {}).values() if work_id in s.get("votes", []))

def is_admin_request(handler, data):
    pw = handler.headers.get("X-Admin-Password", "")
    return bool(pw) and check_password(pw, data.get("adminPassword", ""))

def validate_backup_data(data):
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("works", []), list):
        return False
    if not isinstance(data.get("sessions", {}), dict):
        return False
    return True

def parse_multipart(content_type, body_bytes):
    boundary = None
    for part in content_type.split(";"):
        p = part.strip()
        if p.startswith("boundary="):
            boundary = p[len("boundary="):].strip('"')
            break
    if not boundary:
        return {}, None, None, None
    fields = {}
    file_data = file_name = file_ct = None
    for part in body_bytes.split(("--" + boundary).encode())[1:]:
        if part.startswith(b"--") or part.strip() == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        hdr, _, content = part.partition(b"\r\n\r\n")
        if content.endswith(b"\r\n"):
            content = content[:-2]
        disposition = part_type = ""
        for line in hdr.decode("utf-8", errors="replace").split("\r\n"):
            ll = line.lower()
            if ll.startswith("content-disposition:"):
                disposition = line
            elif ll.startswith("content-type:"):
                part_type = line.split(":", 1)[1].strip()
        name = fname = ""
        for seg in disposition.split(";"):
            seg = seg.strip()
            if seg.startswith("name="):
                name = seg[5:].strip('"')
            elif seg.startswith("filename="):
                fname = seg[9:].strip('"')
        if fname:
            file_data, file_name, file_ct = content, fname, part_type
        else:
            fields[name] = content.decode("utf-8", errors="replace")
    return fields, file_data, file_name, file_ct

def cleanup_local_tool_jobs():
    now = time.time()
    if not os.path.isdir(LOCAL_TOOLS_DIR):
        return
    for name in os.listdir(LOCAL_TOOLS_DIR):
        path = os.path.join(LOCAL_TOOLS_DIR, name)
        try:
            if now - os.path.getmtime(path) <= LOCAL_JOB_TTL_SECONDS:
                continue
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except Exception:
            pass

def remove_local_tool_job(job_id_or_path):
    if not job_id_or_path:
        return False
    base = os.path.realpath(LOCAL_TOOLS_DIR)
    if os.path.sep in str(job_id_or_path):
        path = os.path.realpath(str(job_id_or_path))
    else:
        path = os.path.realpath(os.path.join(LOCAL_TOOLS_DIR, str(job_id_or_path)))
    if not path.startswith(base + os.path.sep):
        return False
    if not os.path.exists(path):
        return True
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return True
    except Exception:
        return False

def safe_file_stem(filename, fallback="file", max_len=70):
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    cleaned = "".join(c if c.isalnum() or c in "._- " else "_" for c in stem).strip(" ._")
    return (cleaned[:max_len] or fallback)

def find_soffice():
    candidates = [
        shutil.which("soffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/Applications/OpenOffice.app/Contents/MacOS/soffice",
        "/Applications/Collabora Office.app/Contents/MacOS/soffice",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None

def allow_text_pdf_fallback():
    return os.environ.get("PDF_TOOL_ALLOW_TEXT_FALLBACK", "").strip() == "1"

def local_tool_status():
    soffice = find_soffice()
    return {
        "pdfPreviewAndSplit": bool(shutil.which("osascript") and os.path.isfile(PDFKIT_TOOL_JS)),
        "hwpAutoConverter": bool(soffice),
        "sofficePath": soffice or "",
        "hwpMac2014": os.path.isdir("/Applications/HwpMac2014.app"),
        "hancomViewer": os.path.isdir("/Applications/한컴오피스 한글 Viewer.app"),
        "hwpTextFallback": allow_text_pdf_fallback(),
        "hwpxTextFallback": allow_text_pdf_fallback() and bool(shutil.which("cupsfilter") or os.path.isfile("/usr/sbin/cupsfilter")),
        "uploadLimitMb": LOCAL_TOOL_UPLOAD_LIMIT_MB,
    }

def parse_pdfkit_output(proc):
    payload = None
    for line in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
            break
        except Exception:
            continue
    if payload is None:
        detail = (proc.stderr or proc.stdout or "PDFKit 도구 응답을 읽을 수 없습니다.").strip()
        raise RuntimeError(detail[:500])
    if proc.returncode != 0 or not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "PDFKit 처리에 실패했습니다.")
    return payload

def run_pdfkit_tool(*args, timeout=120):
    osascript = shutil.which("osascript") or "/usr/bin/osascript"
    if not os.path.isfile(PDFKIT_TOOL_JS):
        raise RuntimeError("PDFKit 도구 파일을 찾을 수 없습니다.")
    proc = subprocess.run(
        [osascript, "-l", "JavaScript", PDFKIT_TOOL_JS, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return parse_pdfkit_output(proc)

def parse_page_selection(value, max_page):
    pages = []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = re.split(r"[,\s]+", str(value or "").strip())
    for item in raw_items:
        if isinstance(item, int):
            pages.append(item)
            continue
        text = str(item).strip()
        if not text:
            continue
        if "-" in text:
            start, _, end = text.partition("-")
            if start.strip().isdigit() and end.strip().isdigit():
                a, b = int(start), int(end)
                if a <= b:
                    pages.extend(range(a, b + 1))
                else:
                    pages.extend(range(a, b - 1, -1))
            continue
        if text.isdigit():
            pages.append(int(text))
    normalized = []
    seen = set()
    for page in pages:
        if page < 1 or page > max_page:
            raise ValueError("선택한 페이지 번호가 PDF 범위를 벗어났습니다.")
        if page not in seen:
            seen.add(page)
            normalized.append(page)
    normalized.sort()
    if not normalized:
        raise ValueError("분리할 페이지를 선택해주세요.")
    return normalized

def extract_hwpx_text(path):
    paragraphs = []

    def local_name(tag):
        return tag.rsplit("}", 1)[-1].lower()

    def read_xml(xml_bytes):
        root = ET.fromstring(xml_bytes)
        for elem in root.iter():
            if local_name(elem.tag) not in {"p", "paragraph"}:
                continue
            parts = []
            for child in elem.iter():
                name = local_name(child.tag)
                if name == "t" and child.text:
                    parts.append(child.text)
                elif name in {"linebreak", "br"}:
                    parts.append("\n")
            text = "".join(parts).strip()
            if text:
                paragraphs.append(text)

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = [
                name for name in zf.namelist()
                if name.lower().endswith(".xml") and (
                    "contents/section" in name.lower() or "section" in os.path.basename(name).lower()
                )
            ]
            if not names:
                names = [name for name in zf.namelist() if name.lower().endswith(".xml")]
            names.sort(key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
            for name in names:
                try:
                    read_xml(zf.read(name))
                except Exception:
                    continue
    else:
        with open(path, "rb") as f:
            read_xml(f.read())

    return "\n\n".join(paragraphs).strip()

class OleReader:
    FREESECT = 0xFFFFFFFF
    ENDOFCHAIN = 0xFFFFFFFE
    FATSECT = 0xFFFFFFFD
    DIFSECT = 0xFFFFFFFC

    def __init__(self, data):
        self.data = data
        if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise ValueError("HWP 5.x 파일 형식이 아닙니다.")
        self.sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
        self.mini_sector_size = 1 << struct.unpack_from("<H", data, 32)[0]
        self.first_dir_sector = struct.unpack_from("<I", data, 48)[0]
        self.mini_cutoff = struct.unpack_from("<I", data, 56)[0]
        self.first_minifat_sector = struct.unpack_from("<I", data, 60)[0]
        self.num_minifat_sectors = struct.unpack_from("<I", data, 64)[0]
        self.first_difat_sector = struct.unpack_from("<I", data, 68)[0]
        self.num_difat_sectors = struct.unpack_from("<I", data, 72)[0]
        self.fat = self._read_fat()
        self.entries = self._read_directory()
        self.root_entry = next((e for e in self.entries if e["type"] == 5), None)
        self.minifat = self._read_minifat()
        self.mini_stream = self._read_regular_stream(
            self.root_entry["start"], self.root_entry["size"]
        ) if self.root_entry else b""
        self.streams = {}
        if self.entries:
            self._walk_storage(0, "")

    def _sector_offset(self, sector):
        return (sector + 1) * self.sector_size

    def _sector_bytes(self, sector):
        start = self._sector_offset(sector)
        return self.data[start:start + self.sector_size]

    def _chain(self, start, fat=None):
        if start in {self.FREESECT, self.ENDOFCHAIN}:
            return []
        fat = fat or self.fat
        out = []
        seen = set()
        sector = start
        while sector not in {self.FREESECT, self.ENDOFCHAIN}:
            if sector in seen or sector >= len(fat):
                break
            seen.add(sector)
            out.append(sector)
            sector = fat[sector]
        return out

    def _read_fat(self):
        fat_sector_ids = [
            v for v in struct.unpack_from("<109I", self.data, 76)
            if v not in {self.FREESECT, self.ENDOFCHAIN}
        ]
        sector = self.first_difat_sector
        for _ in range(self.num_difat_sectors):
            if sector in {self.FREESECT, self.ENDOFCHAIN}:
                break
            block = self._sector_bytes(sector)
            values = struct.unpack_from(f"<{self.sector_size // 4}I", block)
            fat_sector_ids.extend(v for v in values[:-1] if v not in {self.FREESECT, self.ENDOFCHAIN})
            sector = values[-1]
        fat = []
        for sid in fat_sector_ids:
            block = self._sector_bytes(sid)
            fat.extend(struct.unpack_from(f"<{self.sector_size // 4}I", block))
        return fat

    def _read_regular_stream(self, start, size):
        chunks = [self._sector_bytes(sid) for sid in self._chain(start)]
        return b"".join(chunks)[:size]

    def _read_minifat(self):
        if self.first_minifat_sector in {self.FREESECT, self.ENDOFCHAIN}:
            return []
        chunks = [self._sector_bytes(sid) for sid in self._chain(self.first_minifat_sector)]
        raw = b"".join(chunks)[:self.num_minifat_sectors * self.sector_size]
        if not raw:
            return []
        return list(struct.unpack_from(f"<{len(raw) // 4}I", raw))

    def _read_mini_stream(self, start, size):
        chunks = []
        for sid in self._chain(start, self.minifat):
            pos = sid * self.mini_sector_size
            chunks.append(self.mini_stream[pos:pos + self.mini_sector_size])
        return b"".join(chunks)[:size]

    def _read_directory(self):
        raw = self._read_regular_stream(self.first_dir_sector, len(self.data))
        entries = []
        for pos in range(0, len(raw), 128):
            item = raw[pos:pos + 128]
            if len(item) < 128:
                continue
            name_len = struct.unpack_from("<H", item, 64)[0]
            name = ""
            if 2 <= name_len <= 64:
                name = item[:name_len - 2].decode("utf-16le", errors="ignore")
            obj_type = item[66]
            if not name and obj_type == 0:
                continue
            entries.append({
                "name": name,
                "type": obj_type,
                "left": struct.unpack_from("<I", item, 68)[0],
                "right": struct.unpack_from("<I", item, 72)[0],
                "child": struct.unpack_from("<I", item, 76)[0],
                "start": struct.unpack_from("<I", item, 116)[0],
                "size": struct.unpack_from("<Q", item, 120)[0],
            })
        return entries

    def _walk_storage(self, entry_id, prefix):
        if entry_id in {self.FREESECT, self.ENDOFCHAIN} or entry_id >= len(self.entries):
            return
        entry = self.entries[entry_id]
        for side in ["left", "right"]:
            if entry[side] not in {self.FREESECT, self.ENDOFCHAIN}:
                self._walk_storage(entry[side], prefix)
        if entry["type"] == 5:
            if entry["child"] not in {self.FREESECT, self.ENDOFCHAIN}:
                self._walk_storage(entry["child"], prefix)
        elif entry["type"] == 1:
            name = entry["name"]
            next_prefix = f"{prefix}/{name}" if prefix else name
            if entry["child"] not in {self.FREESECT, self.ENDOFCHAIN}:
                self._walk_storage(entry["child"], next_prefix)
        elif entry["type"] == 2:
            path = f"{prefix}/{entry['name']}" if prefix else entry["name"]
            self.streams[path] = entry

    def open_stream(self, path):
        entry = self.streams.get(path)
        if not entry:
            raise KeyError(path)
        if entry["size"] < self.mini_cutoff and self.minifat:
            return self._read_mini_stream(entry["start"], entry["size"])
        return self._read_regular_stream(entry["start"], entry["size"])

    def list_streams(self):
        return sorted(self.streams.keys())

def clean_hwp_text(text):
    out = []
    for ch in text:
        code = ord(ch)
        if ch in "\r\n":
            out.append("\n")
        elif ch == "\t":
            out.append(" ")
        elif code < 32:
            continue
        elif 0xE000 <= code <= 0xF8FF:
            continue
        else:
            out.append(ch)
    cleaned = "".join(out)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()

def extract_hwp_text(path):
    with open(path, "rb") as f:
        reader = OleReader(f.read())
    header = reader.open_stream("FileHeader")
    if b"HWP Document File" not in header[:80]:
        raise RuntimeError("지원하지 않는 HWP 파일입니다.")
    flags = struct.unpack_from("<I", header, 36)[0] if len(header) >= 40 else 0
    if flags & 0x02:
        raise RuntimeError("암호가 걸린 HWP 파일은 변환할 수 없습니다.")
    compressed = bool(flags & 0x01)
    section_streams = [
        name for name in reader.list_streams()
        if re.match(r"^BodyText/Section\d+$", name, re.IGNORECASE)
    ]
    section_streams.sort(key=lambda n: int(re.search(r"(\d+)$", n).group(1)))
    paragraphs = []
    for stream_name in section_streams:
        raw = reader.open_stream(stream_name)
        if compressed:
            try:
                raw = zlib.decompress(raw, -15)
            except zlib.error:
                raw = zlib.decompress(raw)
        pos = 0
        while pos + 4 <= len(raw):
            header_val = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
            tag_id = header_val & 0x3ff
            size = (header_val >> 20) & 0xfff
            if size == 0xfff:
                if pos + 4 > len(raw):
                    break
                size = struct.unpack_from("<I", raw, pos)[0]
                pos += 4
            payload = raw[pos:pos + size]
            pos += size
            if tag_id == 67 and payload:
                text = clean_hwp_text(payload.decode("utf-16le", errors="ignore"))
                if text:
                    paragraphs.append(text)
    result = "\n\n".join(paragraphs).strip()
    if not result:
        raise RuntimeError("HWP 문서에서 텍스트를 추출하지 못했습니다.")
    return result

def text_to_pdf_with_cups(text_path, pdf_path):
    cupsfilter = shutil.which("cupsfilter") or "/usr/sbin/cupsfilter"
    if not os.path.isfile(cupsfilter):
        raise RuntimeError("macOS cupsfilter를 찾을 수 없습니다.")
    env = os.environ.copy()
    env.setdefault("LANG", "ko_KR.UTF-8")
    with open(pdf_path, "wb") as out:
        proc = subprocess.run(
            [cupsfilter, text_path],
            stdout=out,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            env=env,
        )
    if proc.returncode != 0 or not os.path.isfile(pdf_path) or os.path.getsize(pdf_path) < 100:
        raise RuntimeError((proc.stderr or "텍스트 PDF 변환에 실패했습니다.").strip()[:500])

def convert_with_soffice(input_path, output_dir):
    soffice = find_soffice()
    if not soffice:
        return None
    proc = subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", output_dir, input_path],
        capture_output=True,
        text=True,
        timeout=180,
    )
    expected = os.path.join(output_dir, os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
    if proc.returncode == 0 and os.path.isfile(expected):
        return expected
    pdfs = [os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.lower().endswith(".pdf")]
    if proc.returncode == 0 and pdfs:
        return max(pdfs, key=os.path.getmtime)
    raise RuntimeError((proc.stderr or proc.stdout or "LibreOffice 변환에 실패했습니다.").strip()[:500])

def convert_local_document_to_pdf(input_path, original_name, output_dir):
    ext = os.path.splitext(original_name or input_path)[1].lower()
    soffice_pdf = convert_with_soffice(input_path, output_dir)
    if soffice_pdf:
        return soffice_pdf, "office"
    if allow_text_pdf_fallback() and ext == ".hwp":
        text = extract_hwp_text(input_path)
        text_path = os.path.join(output_dir, safe_file_stem(original_name, "hwp") + ".txt")
        pdf_path = os.path.join(output_dir, safe_file_stem(original_name, "hwp") + ".pdf")
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(text)
        text_to_pdf_with_cups(text_path, pdf_path)
        return pdf_path, "hwp-text"
    if allow_text_pdf_fallback() and ext == ".hwpx":
        text = extract_hwpx_text(input_path)
        if not text:
            raise RuntimeError("HWPX에서 텍스트를 추출하지 못했습니다.")
        text_path = os.path.join(output_dir, safe_file_stem(original_name, "hwpx") + ".txt")
        pdf_path = os.path.join(output_dir, safe_file_stem(original_name, "hwpx") + ".pdf")
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(text)
        text_to_pdf_with_cups(text_path, pdf_path)
        return pdf_path, "hwpx-text"
    raise RuntimeError(
        "이 노트북에는 HWP/HWPX 원본 서식을 PDF로 변환하는 로컬 엔진이 연결되어 있지 않습니다. "
        "이전 텍스트 추출 방식은 표와 문단이 깨질 수 있어 중단했습니다. "
        "LibreOffice, OpenOffice, Collabora Office 중 하나의 soffice가 설치되면 이 화면에서 자동 변환됩니다."
    )

def send_binary(handler, body, filename, content_type):
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
    handler.send_header("Content-Length", len(body))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def get_client_ip(self):
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, msg, status=400):
        self.send_json({"error": msg}, status)

    # ──────── GET ────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        ip = self.get_client_ip()

        # 업로드 이미지 서빙
        if path.startswith("/uploads/"):
            fname = os.path.basename(unquote(path[len("/uploads/"):]))
            fpath = os.path.join(UPLOAD_DIR, fname)
            if not os.path.isfile(fpath):
                self.send_response(404); self.end_headers(); return
            ext  = os.path.splitext(fname)[1].lower()
            mime = MIME_TYPES.get(ext, "application/octet-stream")
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", len(body))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/api/works/download/"):
            work_id = unquote(path[len("/api/works/download/"):]).strip()
            data = load_data()
            work = next((w for w in data.get("works", []) if w.get("id") == work_id), None)
            if not work:
                self.send_error_json("작품을 찾을 수 없습니다.", 404); return
            fname = os.path.basename(work.get("filename", ""))
            fpath = os.path.join(UPLOAD_DIR, fname)
            body = None
            if os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    body = f.read()
            elif r2_configured():
                body = r2_download(f"uploads/{fname}")
            if body is None:
                self.send_error_json("이미지 파일을 찾을 수 없습니다.", 404); return
            ext = os.path.splitext(fname)[1].lower()
            mime = MIME_TYPES.get(ext, "application/octet-stream")
            display = safe_download_name(work)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(display)}")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/works/download-selected":
            ids = [
                unquote(v).strip()
                for raw in parse_qs(parsed.query).get("ids", [])
                for v in raw.split(",")
                if unquote(v).strip()
            ]
            if not ids:
                self.send_error_json("선택된 작품이 없습니다.", 400); return
            data = load_data()
            works_by_id = {w.get("id"): w for w in data.get("works", [])}
            buf = io.BytesIO()
            added = 0
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for idx, work_id in enumerate(ids, 1):
                    work = works_by_id.get(work_id)
                    if not work:
                        continue
                    fname = os.path.basename(work.get("filename", ""))
                    fpath = os.path.join(UPLOAD_DIR, fname)
                    body = None
                    if os.path.isfile(fpath):
                        with open(fpath, "rb") as f:
                            body = f.read()
                    elif r2_configured():
                        body = r2_download(f"uploads/{fname}")
                    if body is None:
                        continue
                    zf.writestr(f"{idx:02d}_{safe_download_name(work)}", body)
                    added += 1
            if not added:
                self.send_error_json("다운로드할 이미지 파일을 찾을 수 없습니다.", 404); return
            zip_body = buf.getvalue()
            today = datetime.now().strftime("%Y%m%d")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename=wallpapers-{today}.zip")
            self.send_header("Content-Length", len(zip_body))
            self.end_headers()
            self.wfile.write(zip_body)
            return

        if path == "/api/works":
            data = load_data()
            result = [{**w, "voteCount": get_vote_count(data, w["id"])} for w in data["works"]]
            result.sort(key=lambda x: x.get("uploadedAt", ""))
            self.send_json(result); return

        if path == "/api/votes/me":
            token = self.headers.get("X-Session-Token", "")
            if not token:
                self.send_json({"votes": []}); return
            data = load_data()
            nick = get_nickname_by_token(data, token)
            votes = data["sessions"][nick].get("votes", []) if nick else []
            self.send_json({"votes": votes}); return

        if path == "/api/status":
            data = load_data()
            self.send_json({"votingEnded": data.get("votingEnded", False)}); return

        if path == "/api/local-tools/status":
            cleanup_local_tool_jobs()
            self.send_json(local_tool_status()); return

        if path == "/api/results":
            data = load_data()
            if not data.get("votingEnded", False) and not is_admin_request(self, data):
                self.send_error_json("투표 종료 전에는 결과를 볼 수 없습니다.", 403); return
            results = [{"id": w["id"], "author": w["author"], "title": w["title"],
                        "filename": w["filename"], "voteCount": get_vote_count(data, w["id"])}
                       for w in data["works"]]
            results.sort(key=lambda x: -x["voteCount"])
            self.send_json({"results": results, "totalVoters": len(data.get("sessions", {}))}); return

        if path == "/api/docs":
            docs = []
            if os.path.isdir(DOCS_DIR):
                for fname in sorted(os.listdir(DOCS_DIR), reverse=True):
                    fp = os.path.join(DOCS_DIR, fname)
                    if os.path.isfile(fp):
                        stat = os.stat(fp)
                        display = fname[9:] if len(fname) > 9 and fname[8] == "_" else fname
                        docs.append({"filename": fname, "displayName": display,
                                     "size": stat.st_size, "uploadedAt": stat.st_mtime * 1000})
            self.send_json(docs); return

        if path.startswith("/api/docs/download/"):
            fname = os.path.basename(unquote(path[len("/api/docs/download/"):]))
            fpath = os.path.join(DOCS_DIR, fname)
            if not os.path.isfile(fpath):
                self.send_error_json("파일을 찾을 수 없습니다.", 404); return
            display = fname[9:] if len(fname) > 9 and fname[8] == "_" else fname
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(display)}")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body); return

        if path.startswith("/api/docs/view/"):
            fname = os.path.basename(unquote(path[len("/api/docs/view/"):]))
            fpath = os.path.join(DOCS_DIR, fname)
            if not os.path.isfile(fpath):
                self.send_error_json("파일을 찾을 수 없습니다.", 404); return
            ext = os.path.splitext(fname)[1].lower()
            if ext not in {".html", ".htm", ".pdf", ".txt"}:
                self.send_error_json("미리보기를 지원하지 않는 파일입니다.", 400); return
            mime = {
                ".html": "text/html; charset=utf-8",
                ".htm": "text/html; charset=utf-8",
                ".pdf": "application/pdf",
                ".txt": "text/plain; charset=utf-8",
            }.get(ext, "application/octet-stream")
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Disposition", "inline")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body); return

        if path == "/api/admin/status":
            if not check_rate_limit(ip, "admin", 20, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            data = load_data()
            if not is_admin_request(self, data): self.send_error_json("비밀번호가 틀렸습니다.", 403); return
            r2 = _get_r2()
            r2_ok, r2_error = r2_health()
            data_mtime = os.path.getmtime(DATA_FILE) if os.path.exists(DATA_FILE) else None
            self.send_json({
                "server": "python",
                "storageMode": "r2" if r2_ok else "local",
                "r2Configured": bool(r2),
                "r2Connected": bool(r2_ok),
                "r2Error": r2_error,
                "works": len(data.get("works", [])),
                "voters": len(data.get("sessions", {})),
                "docs": len([f for f in os.listdir(DOCS_DIR) if os.path.isfile(os.path.join(DOCS_DIR, f))]) if os.path.isdir(DOCS_DIR) else 0,
                "uploads": len([f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f))]) if os.path.isdir(UPLOAD_DIR) else 0,
                "dataUpdatedAt": datetime.fromtimestamp(data_mtime, timezone.utc).isoformat() if data_mtime else None,
                "requiredEnv": ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"],
                "envPresent": r2_env_status(),
                "effectiveConfig": r2_effective_status(),
            }); return

        if path == "/api/admin/r2-test":
            if not check_rate_limit(ip, "admin", 20, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            data = load_data()
            if not is_admin_request(self, data): self.send_error_json("비밀번호가 틀렸습니다.", 403); return
            test_key = "_healthcheck/render-r2-test.txt"
            ok = r2_upload(test_key, b"ok", "text/plain")
            if not ok:
                self.send_json({"success": False, "error": _r2_last_error}, 500); return
            r2_delete(test_key)
            self.send_json({"success": True}); return

        if path == "/api/admin/sessions":
            if not check_rate_limit(ip, "admin", 20, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            pw = self.headers.get("X-Admin-Password", "")
            data = load_data()
            if not check_password(pw, data.get("adminPassword", "")): self.send_error_json("비밀번호가 틀렸습니다.", 403); return
            sessions = data.get("sessions", {})
            result = [
                {"nickname": nick, "votes": len(info.get("votes", []))}
                for nick, info in sessions.items()
            ]
            result.sort(key=lambda x: x["nickname"])
            self.send_json({"sessions": result}); return

        if path == "/api/admin/export":
            if not check_rate_limit(ip, "admin", 20, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            pw = self.headers.get("X-Admin-Password", "")
            data = load_data()
            if not check_password(pw, data.get("adminPassword", "")): self.send_error_json("비밀번호가 틀렸습니다.", 403); return
            today = datetime.now().strftime("%Y-%m-%d")
            body = json.dumps(data, ensure_ascii=False, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="backup-{today}.json"')
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body); return

        char_map = {
            "/cursor.png": "public/강이.png", "/%EA%B0%95%EC%9D%B4.png": "public/강이.png", "/강이.png": "public/강이.png",
            "/%EA%B1%B4%EA%B0%95%EA%B7%A0%EB%8D%A9.png": "건강균덩.png",
            "/character.png": "건강균덩.png", "/건강균덩.png": "건강균덩.png",
        }
        if path in char_map:
            fpath = os.path.join(_BASE, char_map[path])
            if os.path.isfile(fpath):
                with open(fpath, "rb") as f: body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", len(body))
                self.end_headers(); self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()
            return

        if path == "/": path = "/index.html"
        fpath = os.path.realpath(os.path.join(PUBLIC_DIR, path.lstrip("/")))
        if not fpath.startswith(os.path.realpath(PUBLIC_DIR)):
            self.send_response(403); self.end_headers(); return
        if os.path.isfile(fpath):
            ext = os.path.splitext(fpath)[1].lower()
            mime = MIME_TYPES.get(ext, "application/octet-stream")
            with open(fpath, "rb") as f: body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", len(body))
            self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    # ──────── POST ────────
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        ip = self.get_client_ip()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if path == "/api/tools/pdf/cleanup":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                payload = {}
            job_id = sanitize(payload.get("jobId", ""), 80)
            self.send_json({"success": remove_local_tool_job(job_id)}); return

        if path == "/api/tools/pdf/preview":
            if not check_rate_limit(ip, "pdf-preview", 20, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            cleanup_local_tool_jobs()
            ct = self.headers.get("Content-Type", "")
            _, file_data, file_name, _ = parse_multipart(ct, body)
            if file_data is None or not file_name:
                self.send_error_json("PDF 파일을 선택해주세요."); return
            if len(file_data) > LOCAL_TOOL_UPLOAD_LIMIT_BYTES:
                self.send_error_json(f"파일 크기는 {LOCAL_TOOL_UPLOAD_LIMIT_MB}MB 이하여야 합니다."); return
            if os.path.splitext(file_name)[1].lower() != ".pdf":
                self.send_error_json("PDF 파일만 미리보기할 수 있습니다."); return

            job_id = str(uuid.uuid4())
            job_dir = os.path.join(LOCAL_TOOLS_DIR, job_id)
            preview_dir = os.path.join(job_dir, "previews")
            os.makedirs(preview_dir, exist_ok=True)
            input_path = os.path.join(job_dir, "source.pdf")
            with open(input_path, "wb") as f:
                f.write(file_data)
            display = os.path.basename(file_name)
            try:
                result = run_pdfkit_tool("preview", input_path, preview_dir, "170", timeout=180)
                previews = []
                for item in result.get("previews", []):
                    p = item.get("path", "")
                    with open(p, "rb") as img:
                        encoded = base64.b64encode(img.read()).decode("ascii")
                    previews.append({"page": item.get("page"), "image": "data:image/png;base64," + encoded})
                meta = {"filename": display, "pageCount": result.get("pageCount", 0), "createdAt": time.time()}
                with open(os.path.join(job_dir, "meta.json"), "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False)
                self.send_json({
                    "success": True,
                    "jobId": job_id,
                    "filename": display,
                    "pageCount": result.get("pageCount", 0),
                    "previews": previews,
                }); return
            except Exception as e:
                try: shutil.rmtree(job_dir)
                except Exception: pass
                self.send_error_json(str(e), 500); return

        if path == "/api/tools/pdf/split":
            if not check_rate_limit(ip, "pdf-split", 30, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self.send_error_json("잘못된 요청입니다."); return
            job_id = sanitize(payload.get("jobId", ""), 80)
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", job_id):
                self.send_error_json("PDF 작업 정보를 찾을 수 없습니다.", 404); return
            job_dir = os.path.realpath(os.path.join(LOCAL_TOOLS_DIR, job_id))
            if not job_dir.startswith(os.path.realpath(LOCAL_TOOLS_DIR)):
                self.send_error_json("PDF 작업 정보를 찾을 수 없습니다.", 404); return
            input_path = os.path.join(job_dir, "source.pdf")
            meta_path = os.path.join(job_dir, "meta.json")
            if not os.path.isfile(input_path) or not os.path.isfile(meta_path):
                self.send_error_json("PDF 미리보기를 먼저 생성해주세요.", 404); return
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            try:
                pages = parse_page_selection(payload.get("pages", payload.get("pageText", "")), int(meta.get("pageCount", 0)))
            except ValueError as e:
                self.send_error_json(str(e)); return
            output_path = os.path.join(job_dir, "selected-pages.pdf")
            try:
                run_pdfkit_tool("split", input_path, output_path, ",".join(map(str, pages)), timeout=180)
                with open(output_path, "rb") as f:
                    out_body = f.read()
                filename = safe_file_stem(meta.get("filename", "selected-pages"), "selected-pages") + "_선택페이지.pdf"
                remove_local_tool_job(job_dir)
                send_binary(self, out_body, filename, "application/pdf"); return
            except Exception as e:
                self.send_error_json(str(e), 500); return

        if path == "/api/tools/hwp-to-pdf":
            if not check_rate_limit(ip, "hwp-convert", 10, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            cleanup_local_tool_jobs()
            ct = self.headers.get("Content-Type", "")
            _, file_data, file_name, _ = parse_multipart(ct, body)
            if file_data is None or not file_name:
                self.send_error_json("한글 파일을 선택해주세요."); return
            if len(file_data) > LOCAL_TOOL_UPLOAD_LIMIT_BYTES:
                self.send_error_json(f"파일 크기는 {LOCAL_TOOL_UPLOAD_LIMIT_MB}MB 이하여야 합니다."); return
            ext = os.path.splitext(file_name)[1].lower()
            if ext not in {".hwp", ".hwpx", ".hml", ".doc", ".docx", ".odt", ".rtf"}:
                self.send_error_json("HWP, HWPX, HML, DOC, DOCX, ODT, RTF 파일만 변환할 수 있습니다."); return

            job_id = str(uuid.uuid4())
            job_dir = os.path.join(LOCAL_TOOLS_DIR, job_id)
            os.makedirs(job_dir, exist_ok=True)
            input_path = os.path.join(job_dir, safe_file_stem(file_name, "document") + ext)
            with open(input_path, "wb") as f:
                f.write(file_data)
            try:
                pdf_path, mode = convert_local_document_to_pdf(input_path, file_name, job_dir)
                with open(pdf_path, "rb") as f:
                    out_body = f.read()
                filename = safe_file_stem(file_name, "converted") + ".pdf"
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
                self.send_header("Content-Length", len(out_body))
                self.send_header("X-Conversion-Mode", mode)
                self.end_headers()
                self.wfile.write(out_body); return
            except Exception as e:
                self.send_error_json(str(e), 500); return
            finally:
                remove_local_tool_job(job_dir)

        if path == "/api/login":
            if not check_rate_limit(ip, "login", 20, 30): self.send_error_json("요청이 너무 많습니다.", 429); return
            try: payload = json.loads(body.decode("utf-8"))
            except Exception: self.send_error_json("잘못된 요청입니다."); return
            nickname = sanitize(payload.get("nickname", ""), 50)
            if not nickname: self.send_error_json("닉네임을 입력해주세요."); return
            result = {}
            def _login(data):
                if "sessions" not in data: data["sessions"] = {}
                if nickname in data["sessions"]:
                    s = data["sessions"][nickname]
                    result.update({"sessionToken": s["sessionToken"], "myVotes": s.get("votes", [])})
                else:
                    token = str(uuid.uuid4())
                    data["sessions"][nickname] = {"sessionToken": token, "votes": []}
                    result.update({"sessionToken": token, "myVotes": []})
            modify_data(_login)
            self.send_json({"success": True, **result}); return

        if path == "/api/upload":
            if not check_rate_limit(ip, "upload", 10, 30): self.send_error_json("업로드 요청이 너무 많습니다.", 429); return
            ct = self.headers.get("Content-Type", "")
            fields, file_data, file_name, _ = parse_multipart(ct, body)
            author = sanitize(fields.get("author", ""), 50)
            title  = sanitize(fields.get("title", ""), 100)
            session_token = self.headers.get("X-Session-Token", "").strip()
            snap = load_data()
            uploader_nick = get_nickname_by_token(snap, session_token) if session_token else None
            if not author or file_data is None: self.send_error_json("이름과 이미지를 모두 입력해주세요."); return
            ext = ".jpg"
            if file_name:
                _, e = os.path.splitext(file_name)
                if e.lower() in ALLOWED_IMAGE_EXTS: ext = e.lower()
                else: self.send_error_json("이미지 파일만 업로드 가능합니다. (jpg, png, gif, webp, bmp)"); return
            if len(file_data) > 30 * 1024 * 1024: self.send_error_json("파일 크기는 30MB 이하여야 합니다."); return
            fname = str(uuid.uuid4()) + ext
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as f: f.write(file_data)
            if r2_configured():
                ok = r2_upload(f"uploads/{fname}", file_data, f"image/{ext[1:]}")
                if not ok:
                    try: os.remove(os.path.join(UPLOAD_DIR, fname))
                    except Exception: pass
                    self.send_error_json(f"R2 백업 저장에 실패했습니다: {_r2_last_error}", 500); return
            work = {"id": str(uuid.uuid4()), "author": author,
                    "title": title if title else f"{author}의 웰페이퍼",
                    "filename": fname, "uploaderNickname": uploader_nick,
                    "uploadedAt": datetime.now(timezone.utc).isoformat()}
            _, data_ok = modify_data_checked(lambda d: d["works"].append(work))
            if not data_ok:
                try: os.remove(os.path.join(UPLOAD_DIR, fname))
                except Exception: pass
                r2_delete(f"uploads/{fname}")
                self.send_error_json(f"작품 정보 저장에 실패했습니다: {_r2_last_error}", 500); return
            self.send_json({"success": True, "work": work}); return

        if path == "/api/vote":
            if not check_rate_limit(ip, "vote", 20, 30): self.send_error_json("투표 요청이 너무 많습니다.", 429); return
            try: payload = json.loads(body.decode("utf-8"))
            except Exception: self.send_error_json("잘못된 요청입니다."); return
            work_id = sanitize(payload.get("workId", ""), 200)
            session_token = self.headers.get("X-Session-Token", "").strip()
            if not work_id or not session_token: self.send_error_json("필수 정보가 누락됐습니다."); return
            result = {}
            def _vote(data):
                nick = get_nickname_by_token(data, session_token)
                if not nick: result.update({"error": "로그인이 필요합니다.", "status": 401}); return
                if data.get("votingEnded"): result.update({"error": "투표가 종료됐습니다.", "status": 400}); return
                work = next((w for w in data["works"] if w["id"] == work_id), None)
                if not work: result.update({"error": "존재하지 않는 작품입니다.", "status": 404}); return
                if work.get("uploaderNickname") and work["uploaderNickname"] == nick:
                    result.update({"error": "본인의 작품에는 투표할 수 없습니다.", "status": 400}); return
                session = data["sessions"][nick]
                votes = session["votes"]
                if work_id in votes:
                    session["votes"] = [x for x in votes if x != work_id]
                    result.update({"success": True, "action": "removed", "myVotes": session["votes"]}); return
                if len(votes) >= 2: result.update({"error": "최대 2개 작품에만 투표할 수 있습니다.", "status": 400}); return
                session["votes"].append(work_id)
                result.update({"success": True, "action": "added", "myVotes": session["votes"]})
            modify_data(_vote)
            if "error" in result: self.send_error_json(result["error"], result.get("status", 400))
            else: self.send_json(result)
            return

        if path == "/api/admin/verify":
            if not check_rate_limit(ip, "admin", 20, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            try: payload = json.loads(body.decode("utf-8"))
            except Exception: self.send_error_json("잘못된 요청입니다."); return
            data = load_data()
            self.send_json({"valid": check_password((payload.get("password") or "").strip(), data.get("adminPassword", ""))}); return

        if path == "/api/admin/change-password":
            if not check_rate_limit(ip, "admin", 20, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            try: payload = json.loads(body.decode("utf-8"))
            except Exception: self.send_error_json("잘못된 요청입니다."); return
            data = load_data()
            cur = (payload.get("current") or "").strip()
            new = (payload.get("newPassword") or "").strip()
            if not check_password(cur, data.get("adminPassword", "")): self.send_error_json("현재 비밀번호가 틀렸습니다.", 403); return
            if len(new) < 4: self.send_error_json("새 비밀번호는 4자 이상이어야 합니다."); return
            modify_data(lambda d: d.update({"adminPassword": hash_password(new)}))
            self.send_json({"success": True}); return

        if path == "/api/admin/end-voting":
            if not check_rate_limit(ip, "admin", 20, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            try: payload = json.loads(body.decode("utf-8"))
            except Exception: self.send_error_json("잘못된 요청입니다."); return
            data = load_data()
            if not check_password((payload.get("password") or "").strip(), data.get("adminPassword", "")): self.send_error_json("비밀번호가 틀렸습니다.", 403); return
            new_state = not data.get("votingEnded", False)
            modify_data(lambda d: d.update({"votingEnded": new_state}))
            self.send_json({"success": True, "votingEnded": new_state}); return

        if path == "/api/admin/docs/upload":
            if not check_rate_limit(ip, "admin", 20, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            admin_pw = self.headers.get("X-Admin-Password", "")
            data = load_data()
            if not check_password(admin_pw, data.get("adminPassword", "")): self.send_error_json("비밀번호가 틀렸습니다.", 403); return
            ct = self.headers.get("Content-Type", "")
            fields, file_data, file_name, _ = parse_multipart(ct, body)
            if file_data is None or not file_name: self.send_error_json("파일이 없습니다."); return
            if len(file_data) > DOC_UPLOAD_LIMIT_BYTES:
                self.send_error_json(f"파일 크기는 {DOC_UPLOAD_LIMIT_MB}MB 이하여야 합니다."); return
            _, ext = os.path.splitext(file_name)
            if ext.lower() not in ALLOWED_DOC_EXTS: self.send_error_json("허용되지 않는 파일 형식입니다."); return
            safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in os.path.splitext(file_name)[0])[:60]
            saved = str(uuid.uuid4())[:8] + "_" + safe + ext.lower()
            with open(os.path.join(DOCS_DIR, saved), "wb") as f: f.write(file_data)
            if not r2_configured():
                try: os.remove(os.path.join(DOCS_DIR, saved))
                except Exception: pass
                self.send_error_json("R2 백업이 연결되지 않아 자료를 저장하지 않았습니다. Render 환경변수 4개를 확인해주세요.", 500); return
            ok = r2_upload(f"docs/{saved}", file_data)
            if not ok:
                try: os.remove(os.path.join(DOCS_DIR, saved))
                except Exception: pass
                self.send_error_json(f"R2 백업 저장에 실패했습니다. 자료가 사라지지 않도록 업로드를 취소했습니다: {_r2_last_error}", 500); return
            display = saved[9:] if len(saved) > 9 and saved[8] == "_" else saved
            self.send_json({"success": True, "filename": saved, "displayName": display}); return

        if path == "/api/admin/import":
            if not check_rate_limit(ip, "admin", 20, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
            admin_pw = self.headers.get("X-Admin-Password", "")
            current = load_data()
            if not check_password(admin_pw, current.get("adminPassword", "")): self.send_error_json("비밀번호가 틀렸습니다.", 403); return
            ct = self.headers.get("Content-Type", "")
            _, file_data, file_name, _ = parse_multipart(ct, body)
            if file_data is None:
                self.send_error_json("백업 JSON 파일을 선택해주세요."); return
            if file_name and not file_name.lower().endswith(".json"):
                self.send_error_json("JSON 백업 파일만 복원할 수 있습니다."); return
            try:
                imported = json.loads(file_data.decode("utf-8"))
            except Exception:
                self.send_error_json("백업 파일을 읽을 수 없습니다."); return
            if not validate_backup_data(imported):
                self.send_error_json("백업 파일 형식이 올바르지 않습니다."); return
            if "adminPassword" not in imported:
                imported["adminPassword"] = current.get("adminPassword", hash_password("admin1234"))
            with data_lock:
                save_data(imported)
            migrate_data()
            restored = load_data()
            self.send_json({
                "success": True,
                "works": len(restored.get("works", [])),
                "voters": len(restored.get("sessions", {}))
            }); return

        self.send_response(404); self.end_headers()

    # ──────── DELETE ────────
    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        ip = self.get_client_ip()
        if not check_rate_limit(ip, "admin", 20, 60): self.send_error_json("요청이 너무 많습니다.", 429); return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try: payload = json.loads(body.decode("utf-8"))
        except Exception: payload = {}
        pw = (payload.get("password") or "").strip()
        data = load_data()
        if not check_password(pw, data.get("adminPassword", "")): self.send_error_json("비밀번호가 틀렸습니다.", 403); return

        if path == "/api/admin/reset":
            for fname in os.listdir(UPLOAD_DIR):
                try:
                    os.remove(os.path.join(UPLOAD_DIR, fname))
                    threading.Thread(target=r2_delete, args=(f"uploads/{fname}",), daemon=True).start()
                except Exception: pass
            modify_data(lambda d: d.update({"works": [], "sessions": {}}))
            self.send_json({"success": True}); return

        if path.startswith("/api/admin/docs/"):
            fname = os.path.basename(unquote(path[len("/api/admin/docs/"):]))
            fpath = os.path.join(DOCS_DIR, fname)
            if not os.path.isfile(fpath): self.send_error_json("파일을 찾을 수 없습니다.", 404); return
            try: os.remove(fpath)
            except Exception: pass
            if r2_configured() and not r2_delete(f"docs/{fname}"):
                self.send_error_json("로컬 자료는 삭제했지만 R2 백업 삭제에 실패했습니다. 새로고침 후 다시 확인해주세요.", 500); return
            self.send_json({"success": True}); return

        if path.startswith("/api/admin/work/"):
            work_id = path[len("/api/admin/work/"):]
            work = next((w for w in data["works"] if w["id"] == work_id), None)
            if not work: self.send_error_json("작품을 찾을 수 없습니다.", 404); return
            try: os.remove(os.path.join(UPLOAD_DIR, work["filename"]))
            except Exception: pass
            threading.Thread(target=r2_delete, args=(f"uploads/{work['filename']}",), daemon=True).start()
            def _del(d):
                d["works"] = [w for w in d["works"] if w["id"] != work_id]
                for s in d.get("sessions", {}).values():
                    s["votes"] = [x for x in s.get("votes", []) if x != work_id]
            modify_data(_del)
            self.send_json({"success": True}); return

        self.send_response(404); self.end_headers()


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return "localhost"


if __name__ == "__main__":
    sync_from_r2()
    init_data_file()
    migrate_data()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    local_ip = get_local_ip()
    r2_ok, r2_error = r2_health()
    r2_status = "연결됨" if r2_ok else f"미연결 ({r2_error})"

    print()
    print("==========================================")
    print("  AI 웰페이퍼 공모전 사이트 시작!")
    print(f"  R2 스토리지: {r2_status}")
    print("==========================================")
    print(f"  로컬:    http://localhost:{PORT}")
    print(f"  네트워크: http://{local_ip}:{PORT}")
    print("==========================================")
    print("  종료: Ctrl+C")
    print("==========================================")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
        server.server_close()
