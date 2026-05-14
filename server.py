#!/usr/bin/env python3
"""AI 웰페이퍼 공모전 서버 (Python 3 표준 라이브러리만 사용)"""

import json
import os
import uuid
import socket
import hashlib
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
from datetime import datetime, timezone

PORT = int(os.environ.get("PORT", 3000))
DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "public", "uploads")
DOCS_DIR  = os.path.join(os.path.dirname(__file__), "public", "docs")
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "public")
BASE_DIR   = os.path.dirname(__file__)

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
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
}

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
ALLOWED_DOC_EXTS   = {".hwp", ".hwpx", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".zip"}

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DOCS_DIR,   exist_ok=True)

# ── 비밀번호 해싱 ──
PW_SALT = "wallpaper-contest-v1"

def hash_password(pw: str) -> str:
    return "sha256:" + hashlib.sha256((pw + PW_SALT).encode()).hexdigest()

def check_password(pw: str, stored: str) -> bool:
    if stored.startswith("sha256:"):
        return hash_password(pw) == stored
    return pw == stored  # 기존 평문 비밀번호 호환 (마이그레이션 전 1회)

# ── 데이터 파일 초기화 ──
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({"works": [], "votes": {}, "adminPassword": hash_password("admin1234")}, f)

# ── 동시 쓰기 방지 락 ──
data_lock = threading.Lock()

def load_data():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def modify_data(fn):
    """락을 잡고 데이터를 읽어 fn으로 수정한 뒤 저장. fn이 dict를 반환하면 그걸 결과로 사용."""
    with data_lock:
        data = load_data()
        result = fn(data)
        save_data(data)
        return result

# ── 데이터 마이그레이션 ──
def migrate_data():
    with data_lock:
        data = load_data()
        changed = False

        # 1) 평문 비밀번호 → 해시
        if data.get("adminPassword") and not data["adminPassword"].startswith("sha256:"):
            data["adminPassword"] = hash_password(data["adminPassword"])
            changed = True

        # 2) 구 투표 형식 {"이름": ["id"]} → 신 형식 {"token": {"voterName": ..., "votes": [...]}}
        migrated_votes = {}
        for key, val in list(data.get("votes", {}).items()):
            if isinstance(val, list):
                migrated_votes[f"migrated-{str(uuid.uuid4())}"] = {"voterName": key, "votes": val}
                changed = True
            else:
                migrated_votes[key] = val
        data["votes"] = migrated_votes

        # 3) 기존 작품에 uploaderToken 필드 추가
        for work in data.get("works", []):
            if "uploaderToken" not in work:
                work["uploaderToken"] = None
                changed = True

        if changed:
            save_data(data)

# ── Rate Limiting (IP 기반 단순 구현) ──
_rate_store: dict = {}  # { (ip, endpoint): [(timestamp, ...), ...] }
_rate_lock = threading.Lock()

def check_rate_limit(ip: str, endpoint: str, max_requests: int, window_sec: int) -> bool:
    key = (ip, endpoint)
    now = time.time()
    with _rate_lock:
        timestamps = [t for t in _rate_store.get(key, []) if now - t < window_sec]
        if len(timestamps) >= max_requests:
            _rate_store[key] = timestamps
            return False
        timestamps.append(now)
        _rate_store[key] = timestamps
    return True

# ── 유틸 ──
def sanitize(val, max_len=100):
    return str(val or "").strip()[:max_len]

def get_vote_count(data, work_id):
    count = 0
    for v in data["votes"].values():
        votes = v if isinstance(v, list) else v.get("votes", [])
        if work_id in votes:
            count += 1
    return count

def parse_multipart(content_type, body_bytes):
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"')
            break
    if not boundary:
        return {}, None, None, None

    fields = {}
    file_data = file_name = file_content_type = None

    delimiter = ("--" + boundary).encode()
    for part in body_bytes.split(delimiter)[1:]:
        if part.startswith(b"--") or part.strip() == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_bytes, _, content = part.partition(b"\r\n\r\n")
        if content.endswith(b"\r\n"):
            content = content[:-2]

        disposition = part_ct = ""
        for line in header_bytes.decode("utf-8", errors="replace").split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                disposition = line
            elif line.lower().startswith("content-type:"):
                part_ct = line.split(":", 1)[1].strip()

        name = fname = ""
        for seg in disposition.split(";"):
            seg = seg.strip()
            if seg.startswith("name="):
                name = seg[5:].strip('"')
            elif seg.startswith("filename="):
                fname = seg[9:].strip('"')

        if fname:
            file_data, file_name, file_content_type = content, fname, part_ct
        else:
            fields[name] = content.decode("utf-8", errors="replace")

    return fields, file_data, file_name, file_content_type


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

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

    # ──────────────── GET ────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        ip = self.get_client_ip()

        # ── 작품 목록 ──
        if path == "/api/works":
            data = load_data()
            result = []
            for w in data["works"]:
                result.append({**w, "voteCount": get_vote_count(data, w["id"])})
            result.sort(key=lambda x: (-x["voteCount"], x.get("uploadedAt", "")))
            self.send_json(result)
            return

        # ── 내 투표 현황 (토큰 기반) ──
        if path == "/api/votes/me":
            token = self.headers.get("X-Vote-Token", "")
            if not token:
                self.send_json({"votes": []})
                return
            data = load_data()
            record = data["votes"].get(token)
            if record is None:
                votes = []
            elif isinstance(record, list):
                votes = record
            else:
                votes = record.get("votes", [])
            self.send_json({"votes": votes})
            return

        # ── 투표 상태 ──
        if path == "/api/status":
            data = load_data()
            self.send_json({"votingEnded": data.get("votingEnded", False)})
            return

        # ── 결과 ──
        if path == "/api/results":
            data = load_data()
            results = [{
                "id": w["id"], "author": w["author"], "title": w["title"],
                "filename": w["filename"], "voteCount": get_vote_count(data, w["id"])
            } for w in data["works"]]
            results.sort(key=lambda x: -x["voteCount"])
            self.send_json({"results": results, "totalVoters": len(data["votes"])})
            return

        # ── 자료 목록 ──
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
            self.send_json(docs)
            return

        # ── 자료 다운로드 ──
        if path.startswith("/api/docs/download/"):
            fname = os.path.basename(unquote(path[len("/api/docs/download/"):]))
            fpath = os.path.join(DOCS_DIR, fname)
            if not os.path.isfile(fpath):
                self.send_error_json("파일을 찾을 수 없습니다.", 404)
                return
            display = fname[9:] if len(fname) > 9 and fname[8] == "_" else fname
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            encoded = display.encode("utf-8").hex()
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── 어드민: 데이터 백업 다운로드 ──
        if path == "/api/admin/export":
            if not check_rate_limit(ip, "admin", 40, 900):
                self.send_error_json("요청이 너무 많습니다. 잠시 후 다시 시도하세요.", 429)
                return
            pw = self.headers.get("X-Admin-Password", "")
            data = load_data()
            if not check_password(pw, data.get("adminPassword", "")):
                self.send_error_json("비밀번호가 틀렸습니다.", 403)
                return
            today = datetime.now().strftime("%Y-%m-%d")
            filename = f"backup-{today}.json"
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── 캐릭터 이미지 ──
        char_map = {
            "/cursor.png": "강이.png",
            "/%EA%B0%95%EC%9D%B4.png": "강이.png",
            "/강이.png": "강이.png",
            "/%EA%B1%B4%EA%B0%95%EA%B7%A0%EB%8D%A9.png": "건강균덩.png",
            "/character.png": "건강균덩.png",
            "/건강균덩.png": "건강균덩.png",
        }
        if path in char_map:
            fpath = os.path.join(BASE_DIR, char_map[path])
            if os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()
            return

        # ── 정적 파일 ──
        if path == "/":
            path = "/index.html"
        file_path = os.path.realpath(os.path.join(PUBLIC_DIR, path.lstrip("/")))
        if not file_path.startswith(os.path.realpath(PUBLIC_DIR)):
            self.send_response(403); self.end_headers(); return
        if os.path.isfile(file_path):
            ext = os.path.splitext(file_path)[1].lower()
            mime = MIME_TYPES.get(ext, "application/octet-stream")
            with open(file_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    # ──────────────── POST ────────────────
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        ip = self.get_client_ip()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        # ── 작품 업로드 ──
        if path == "/api/upload":
            if not check_rate_limit(ip, "upload", 10, 60):
                self.send_error_json("업로드 요청이 너무 많습니다. 잠시 후 다시 시도하세요.", 429)
                return
            ct = self.headers.get("Content-Type", "")
            fields, file_data, file_name, file_ct = parse_multipart(ct, body)

            author = sanitize(fields.get("author", ""), 50)
            title  = sanitize(fields.get("title", ""), 100)
            uploader_token = sanitize(fields.get("uploaderToken", ""), 200)

            if not author or file_data is None:
                self.send_error_json("이름과 이미지를 모두 입력해주세요.")
                return

            # 확장자 + 크기 검증
            ext = ".jpg"
            if file_name:
                _, e = os.path.splitext(file_name)
                if e.lower() in ALLOWED_IMAGE_EXTS:
                    ext = e.lower()
                else:
                    self.send_error_json("이미지 파일만 업로드 가능합니다. (jpg, png, gif, webp, bmp)")
                    return
            if len(file_data) > 30 * 1024 * 1024:
                self.send_error_json("파일 크기는 30MB 이하여야 합니다.")
                return

            fname = str(uuid.uuid4()) + ext
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as f:
                f.write(file_data)

            work = {
                "id": str(uuid.uuid4()),
                "author": author,
                "title": title if title else f"{author}의 웰페이퍼",
                "filename": fname,
                "uploaderToken": uploader_token or None,
                "uploadedAt": datetime.now(timezone.utc).isoformat()
            }
            modify_data(lambda d: d["works"].append(work))
            self.send_json({"success": True, "work": work})
            return

        # ── 투표 ──
        if path == "/api/vote":
            if not check_rate_limit(ip, "vote", 20, 60):
                self.send_error_json("투표 요청이 너무 많습니다. 잠시 후 다시 시도하세요.", 429)
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self.send_error_json("잘못된 요청입니다.")
                return

            voter_name = sanitize(payload.get("voterName", "익명"), 50)
            work_id    = sanitize(payload.get("workId", ""), 200)
            token      = self.headers.get("X-Vote-Token", "").strip()

            if not work_id or not token:
                self.send_error_json("필수 정보가 누락됐습니다.")
                return

            result = {}
            def _vote(data):
                nonlocal result
                if data.get("votingEnded"):
                    result = {"error": "투표가 종료됐습니다.", "status": 400}
                    return
                work = next((w for w in data["works"] if w["id"] == work_id), None)
                if not work:
                    result = {"error": "존재하지 않는 작품입니다.", "status": 404}
                    return
                # 본인 작품 투표 방지
                if work.get("uploaderToken") and work["uploaderToken"] == token:
                    result = {"error": "본인의 작품에는 투표할 수 없습니다.", "status": 400}
                    return
                # 토큰 기반 레코드 초기화
                if token not in data["votes"]:
                    data["votes"][token] = {"voterName": voter_name, "votes": []}
                record = data["votes"][token]
                token_votes = record["votes"]
                # 토글 (취소)
                if work_id in token_votes:
                    record["votes"] = [x for x in token_votes if x != work_id]
                    result = {"success": True, "action": "removed", "myVotes": record["votes"]}
                    return
                # 최대 2표 제한
                if len(token_votes) >= 2:
                    result = {"error": "최대 2개 작품에만 투표할 수 있습니다.", "status": 400}
                    return
                record["votes"].append(work_id)
                result = {"success": True, "action": "added", "myVotes": record["votes"]}

            modify_data(_vote)
            if "error" in result:
                self.send_error_json(result["error"], result.get("status", 400))
            else:
                self.send_json(result)
            return

        # ── 어드민: 비밀번호 확인 ──
        if path == "/api/admin/verify":
            if not check_rate_limit(ip, "admin", 40, 900):
                self.send_error_json("요청이 너무 많습니다.", 429)
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self.send_error_json("잘못된 요청입니다.")
                return
            data = load_data()
            pw = (payload.get("password") or "").strip()
            self.send_json({"valid": check_password(pw, data.get("adminPassword", ""))})
            return

        # ── 어드민: 비밀번호 변경 ──
        if path == "/api/admin/change-password":
            if not check_rate_limit(ip, "admin", 40, 900):
                self.send_error_json("요청이 너무 많습니다.", 429)
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self.send_error_json("잘못된 요청입니다.")
                return
            data = load_data()
            current = (payload.get("current") or "").strip()
            new_pw  = (payload.get("newPassword") or "").strip()
            if not check_password(current, data.get("adminPassword", "")):
                self.send_error_json("현재 비밀번호가 틀렸습니다.", 403)
                return
            if len(new_pw) < 4:
                self.send_error_json("새 비밀번호는 4자 이상이어야 합니다.")
                return
            modify_data(lambda d: d.update({"adminPassword": hash_password(new_pw)}))
            self.send_json({"success": True})
            return

        # ── 어드민: 투표 종료/재개 토글 ──
        if path == "/api/admin/end-voting":
            if not check_rate_limit(ip, "admin", 40, 900):
                self.send_error_json("요청이 너무 많습니다.", 429)
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self.send_error_json("잘못된 요청입니다.")
                return
            data = load_data()
            pw = (payload.get("password") or "").strip()
            if not check_password(pw, data.get("adminPassword", "")):
                self.send_error_json("비밀번호가 틀렸습니다.", 403)
                return
            new_state = not data.get("votingEnded", False)
            modify_data(lambda d: d.update({"votingEnded": new_state}))
            self.send_json({"success": True, "votingEnded": new_state})
            return

        # ── 어드민: 자료 업로드 ──
        if path == "/api/admin/docs/upload":
            if not check_rate_limit(ip, "admin", 40, 900):
                self.send_error_json("요청이 너무 많습니다.", 429)
                return
            admin_pw = self.headers.get("X-Admin-Password", "")
            data = load_data()
            if not check_password(admin_pw, data.get("adminPassword", "")):
                self.send_error_json("비밀번호가 틀렸습니다.", 403)
                return
            ct = self.headers.get("Content-Type", "")
            fields, file_data, file_name, _ = parse_multipart(ct, body)
            if file_data is None or not file_name:
                self.send_error_json("파일이 없습니다.")
                return
            if len(file_data) > 50 * 1024 * 1024:
                self.send_error_json("파일 크기는 50MB 이하여야 합니다.")
                return
            _, ext = os.path.splitext(file_name)
            if ext.lower() not in ALLOWED_DOC_EXTS:
                self.send_error_json("허용되지 않는 파일 형식입니다.")
                return
            safe_base = "".join(c if c.isalnum() or c in "._- " else "_"
                                for c in os.path.splitext(file_name)[0])[:60]
            saved_name = str(uuid.uuid4())[:8] + "_" + safe_base + ext.lower()
            with open(os.path.join(DOCS_DIR, saved_name), "wb") as f:
                f.write(file_data)
            display = saved_name[9:] if len(saved_name) > 9 and saved_name[8] == "_" else saved_name
            self.send_json({"success": True, "filename": saved_name, "displayName": display})
            return

        self.send_response(404); self.end_headers()

    # ──────────────── DELETE ────────────────
    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        ip = self.get_client_ip()
        if not check_rate_limit(ip, "admin", 40, 900):
            self.send_error_json("요청이 너무 많습니다.", 429)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {}

        pw = (payload.get("password") or "").strip()
        data = load_data()
        if not check_password(pw, data.get("adminPassword", "")):
            self.send_error_json("비밀번호가 틀렸습니다.", 403)
            return

        # ── 전체 초기화 ──
        if path == "/api/admin/reset":
            for fname in os.listdir(UPLOAD_DIR):
                try: os.remove(os.path.join(UPLOAD_DIR, fname))
                except Exception: pass
            modify_data(lambda d: d.update({"works": [], "votes": {}}))
            self.send_json({"success": True})
            return

        # ── 자료 삭제 ──
        if path.startswith("/api/admin/docs/"):
            fname = os.path.basename(path[len("/api/admin/docs/"):])
            fpath = os.path.join(DOCS_DIR, fname)
            if not os.path.isfile(fpath):
                self.send_error_json("파일을 찾을 수 없습니다.", 404)
                return
            try: os.remove(fpath)
            except Exception: pass
            self.send_json({"success": True})
            return

        # ── 작품 삭제 ──
        if path.startswith("/api/admin/work/"):
            work_id = path[len("/api/admin/work/"):]
            work = next((w for w in data["works"] if w["id"] == work_id), None)
            if not work:
                self.send_error_json("작품을 찾을 수 없습니다.", 404)
                return
            try: os.remove(os.path.join(UPLOAD_DIR, work["filename"]))
            except Exception: pass

            def _delete_work(d):
                d["works"] = [w for w in d["works"] if w["id"] != work_id]
                for token, rec in d["votes"].items():
                    votes = rec if isinstance(rec, list) else rec.get("votes", [])
                    filtered = [x for x in votes if x != work_id]
                    if isinstance(rec, list):
                        d["votes"][token] = filtered
                    else:
                        rec["votes"] = filtered

            modify_data(_delete_work)
            self.send_json({"success": True})
            return

        self.send_response(404); self.end_headers()


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


if __name__ == "__main__":
    migrate_data()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    local_ip = get_local_ip()

    print()
    print("==========================================")
    print("  AI 웰페이퍼 공모전 사이트 시작!")
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
