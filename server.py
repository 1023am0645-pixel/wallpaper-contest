#!/usr/bin/env python3
"""AI 웰페이퍼 공모전 서버 (Python 3 표준 라이브러리만 사용)"""

import json
import os
import sys
import uuid
import shutil
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
import email.parser
import email.policy
import io

PORT = 3000
DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "public", "uploads")
DOCS_DIR = os.path.join(os.path.dirname(__file__), "public", "docs")
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "public")
BASE_DIR = os.path.dirname(__file__)

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DOCS_DIR, exist_ok=True)
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({"works": [], "votes": {}, "adminPassword": "admin1234"}, f)


def load_data():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_multipart(content_type, body_bytes):
    """multipart/form-data 파싱"""
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):]
            if boundary.startswith('"') and boundary.endswith('"'):
                boundary = boundary[1:-1]
            break
    if not boundary:
        return {}, None, None, None

    fields = {}
    file_data = None
    file_name = None
    file_content_type = None

    delimiter = ("--" + boundary).encode()
    end_delimiter = ("--" + boundary + "--").encode()

    parts = body_bytes.split(delimiter)
    for part in parts[1:]:
        if part.startswith(b"--") or part.strip() == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_bytes, _, content = part.partition(b"\r\n\r\n")
        # 마지막 \r\n 제거
        if content.endswith(b"\r\n"):
            content = content[:-2]

        headers_str = header_bytes.decode("utf-8", errors="replace")
        disposition = ""
        part_ct = ""
        for line in headers_str.split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                disposition = line
            elif line.lower().startswith("content-type:"):
                part_ct = line.split(":", 1)[1].strip()

        # name 추출
        name = ""
        fname = ""
        for segment in disposition.split(";"):
            segment = segment.strip()
            if segment.startswith("name="):
                name = segment[5:].strip('"')
            elif segment.startswith("filename="):
                fname = segment[9:].strip('"')

        if fname:
            file_data = content
            file_name = fname
            file_content_type = part_ct
        else:
            fields[name] = content.decode("utf-8", errors="replace")

    return fields, file_data, file_name, file_content_type


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 로그 최소화

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, msg, status=400):
        self.send_json({"error": msg}, status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # API 라우트
        if path == "/api/works":
            data = load_data()
            result = []
            for w in data["works"]:
                vote_count = sum(1 for v in data["votes"].values() if w["id"] in v)
                result.append({**w, "voteCount": vote_count})
            result.sort(key=lambda x: (-x["voteCount"], x.get("uploadedAt", "")))
            self.send_json(result)
            return

        if path.startswith("/api/votes/"):
            voter = unquote(path[len("/api/votes/"):]).strip()
            data = load_data()
            votes = data["votes"].get(voter, [])
            self.send_json({"votes": votes})
            return

        if path == "/api/status":
            data = load_data()
            self.send_json({"votingEnded": data.get("votingEnded", False)})
            return

        if path == "/api/docs":
            docs = []
            if os.path.isdir(DOCS_DIR):
                for fname in sorted(os.listdir(DOCS_DIR), reverse=True):
                    fp = os.path.join(DOCS_DIR, fname)
                    if os.path.isfile(fp):
                        stat = os.stat(fp)
                        display = fname[9:] if len(fname) > 9 and fname[8] == '_' else fname
                        docs.append({
                            "filename": fname,
                            "displayName": display,
                            "size": stat.st_size,
                            "uploadedAt": stat.st_mtime * 1000
                        })
            self.send_json(docs)
            return

        if path.startswith("/api/docs/download/"):
            fname = unquote(path[len("/api/docs/download/"):])
            fname = os.path.basename(fname)
            fpath = os.path.join(DOCS_DIR, fname)
            if not os.path.isfile(fpath):
                self.send_error_json("파일을 찾을 수 없습니다.", 404)
                return
            display = fname[9:] if len(fname) > 9 and fname[8] == '_' else fname
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{display.encode('utf-8').hex()}")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        # 캐릭터 이미지 (ASCII 경로)
        if path == "/cursor.png":
            fpath = os.path.join(BASE_DIR, "강이.png")
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

        if path == "/api/results":
            data = load_data()
            results = []
            for w in data["works"]:
                vote_count = sum(1 for v in data["votes"].values() if w["id"] in v)
                results.append({
                    "id": w["id"], "author": w["author"], "title": w["title"],
                    "filename": w["filename"], "voteCount": vote_count
                })
            results.sort(key=lambda x: -x["voteCount"])
            total_voters = len(data["votes"])
            self.send_json({"results": results, "totalVoters": total_voters})
            return

        # 정적 파일
        if path == "/":
            path = "/index.html"

        file_path = os.path.join(PUBLIC_DIR, path.lstrip("/"))
        # 경로 탈출 방지
        file_path = os.path.realpath(file_path)
        if not file_path.startswith(os.path.realpath(PUBLIC_DIR)):
            self.send_response(403)
            self.end_headers()
            return

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
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if path == "/api/upload":
            ct = self.headers.get("Content-Type", "")
            fields, file_data, file_name, file_ct = parse_multipart(ct, body)

            author = (fields.get("author") or "").strip()
            title = (fields.get("title") or "").strip()

            if not author or file_data is None:
                self.send_error_json("이름과 이미지를 모두 입력해주세요.")
                return

            # 확장자 결정
            ext = ".jpg"
            if file_name:
                _, e = os.path.splitext(file_name)
                if e.lower() in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                    ext = e.lower()
            elif file_ct:
                m = {"image/jpeg": ".jpg", "image/png": ".png",
                     "image/gif": ".gif", "image/webp": ".webp"}
                ext = m.get(file_ct.split(";")[0].strip(), ".jpg")

            # 크기 제한 30MB
            if len(file_data) > 30 * 1024 * 1024:
                self.send_error_json("파일 크기는 30MB 이하여야 합니다.")
                return

            fname = str(uuid.uuid4()) + ext
            fpath = os.path.join(UPLOAD_DIR, fname)
            with open(fpath, "wb") as f:
                f.write(file_data)

            data = load_data()
            from datetime import datetime, timezone
            work = {
                "id": str(uuid.uuid4()),
                "author": author,
                "title": title if title else f"{author}의 웰페이퍼",
                "filename": fname,
                "uploadedAt": datetime.now(timezone.utc).isoformat()
            }
            data["works"].append(work)
            save_data(data)
            self.send_json({"success": True, "work": work})
            return

        if path == "/api/vote":
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self.send_error_json("잘못된 요청입니다.")
                return

            voter_name = (payload.get("voterName") or "").strip()
            work_id = (payload.get("workId") or "").strip()

            if not voter_name or not work_id:
                self.send_error_json("이름과 작품 ID가 필요합니다.")
                return

            data = load_data()
            work_exists = any(w["id"] == work_id for w in data["works"])
            if not work_exists:
                self.send_error_json("존재하지 않는 작품입니다.", 404)
                return

            if voter_name not in data["votes"]:
                data["votes"][voter_name] = []

            if work_id in data["votes"][voter_name]:
                data["votes"][voter_name] = [x for x in data["votes"][voter_name] if x != work_id]
                save_data(data)
                self.send_json({"success": True, "action": "removed", "myVotes": data["votes"][voter_name]})
                return

            if len(data["votes"][voter_name]) >= 2:
                self.send_error_json("최대 2개 작품에만 투표할 수 있습니다.")
                return

            data["votes"][voter_name].append(work_id)
            save_data(data)
            self.send_json({"success": True, "action": "added", "myVotes": data["votes"][voter_name]})
            return

        if path == "/api/admin/verify":
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self.send_error_json("잘못된 요청입니다.")
                return
            data = load_data()
            pw = (payload.get("password") or "").strip()
            self.send_json({"valid": pw == data.get("adminPassword", "admin1234")})
            return

        if path == "/api/admin/end-voting":
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self.send_error_json("잘못된 요청입니다.")
                return
            data = load_data()
            pw = (payload.get("password") or "").strip()
            if pw != data.get("adminPassword", "admin1234"):
                self.send_error_json("비밀번호가 틀렸습니다.", 403)
                return
            data["votingEnded"] = not data.get("votingEnded", False)
            save_data(data)
            self.send_json({"success": True, "votingEnded": data["votingEnded"]})
            return

        if path == "/api/admin/docs/upload":
            admin_pw = self.headers.get("x-admin-password", "")
            data = load_data()
            if admin_pw != data.get("adminPassword", "admin1234"):
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
            allowed_exts = {".hwp", ".hwpx", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".zip"}
            _, ext = os.path.splitext(file_name)
            if ext.lower() not in allowed_exts:
                self.send_error_json("허용되지 않는 파일 형식입니다.")
                return
            safe_base = "".join(c if c.isalnum() or c in "._- " else "_" for c in os.path.splitext(file_name)[0])[:60]
            saved_name = str(uuid.uuid4())[:8] + "_" + safe_base + ext.lower()
            with open(os.path.join(DOCS_DIR, saved_name), "wb") as f:
                f.write(file_data)
            display = saved_name[9:] if len(saved_name) > 9 and saved_name[8] == '_' else saved_name
            self.send_json({"success": True, "filename": saved_name, "displayName": display})
            return

        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {}

        pw = (payload.get("password") or "").strip()
        data = load_data()

        if pw != data.get("adminPassword", "admin1234"):
            self.send_error_json("비밀번호가 틀렸습니다.", 403)
            return

        if path == "/api/admin/reset":
            # 파일 삭제
            for fname in os.listdir(UPLOAD_DIR):
                try:
                    os.remove(os.path.join(UPLOAD_DIR, fname))
                except Exception:
                    pass
            save_data({"works": [], "votes": {}, "adminPassword": data.get("adminPassword", "admin1234")})
            self.send_json({"success": True})
            return

        if path.startswith("/api/admin/docs/"):
            fname = os.path.basename(path[len("/api/admin/docs/"):])
            fpath = os.path.join(DOCS_DIR, fname)
            if not os.path.isfile(fpath):
                self.send_error_json("파일을 찾을 수 없습니다.", 404)
                return
            try:
                os.remove(fpath)
            except Exception:
                pass
            self.send_json({"success": True})
            return

        if path.startswith("/api/admin/work/"):
            work_id = path[len("/api/admin/work/"):]
            work = next((w for w in data["works"] if w["id"] == work_id), None)
            if not work:
                self.send_error_json("작품을 찾을 수 없습니다.", 404)
                return

            try:
                os.remove(os.path.join(UPLOAD_DIR, work["filename"]))
            except Exception:
                pass

            for voter in data["votes"]:
                data["votes"][voter] = [x for x in data["votes"][voter] if x != work_id]
            data["works"] = [w for w in data["works"] if w["id"] != work_id]
            save_data(data)
            self.send_json({"success": True})
            return

        self.send_response(404)
        self.end_headers()


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
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    local_ip = get_local_ip()

    print()
    print("==========================================")
    print("  AI 웰페이퍼 공모전 사이트 시작!")
    print("==========================================")
    print(f"  로컬:    http://localhost:{PORT}")
    print(f"  네트워크: http://{local_ip}:{PORT}")
    print("==========================================")
    print("  어드민 비밀번호: admin1234")
    print("  종료: Ctrl+C")
    print("==========================================")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
        server.server_close()
