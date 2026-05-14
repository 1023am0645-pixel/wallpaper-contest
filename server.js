const express = require('express');
const multer = require('multer');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { v4: uuidv4 } = require('uuid');
const rateLimit = require('express-rate-limit');

const app = express();
const PORT = process.env.PORT || 3000;
const DATA_FILE = './data.json';
const UPLOAD_DIR = './public/uploads';
const DOCS_DIR = './public/docs';

if (!fs.existsSync(UPLOAD_DIR)) fs.mkdirSync(UPLOAD_DIR, { recursive: true });
if (!fs.existsSync(DOCS_DIR)) fs.mkdirSync(DOCS_DIR, { recursive: true });
if (!fs.existsSync(DATA_FILE)) {
  fs.writeFileSync(DATA_FILE, JSON.stringify({
    works: [],
    votes: {},
    adminPassword: hashPassword('admin1234')
  }, null, 2));
}

// ── 비밀번호 해싱 (Node 내장 crypto, 추가 의존성 없음) ──
const PW_SALT = 'wallpaper-contest-v1';
function hashPassword(pw) {
  return 'sha256:' + crypto.createHash('sha256').update(pw + PW_SALT).digest('hex');
}
function checkPassword(pw, stored) {
  if (stored.startsWith('sha256:')) return hashPassword(pw) === stored;
  return pw === stored; // 기존 평문 비밀번호 호환 (마이그레이션 전 1회만 사용)
}

// ── 데이터 마이그레이션 (서버 시작 시 1회 실행) ──
function migrateData() {
  const data = getData();
  let changed = false;

  // 1) 평문 비밀번호 → 해시
  if (data.adminPassword && !data.adminPassword.startsWith('sha256:')) {
    data.adminPassword = hashPassword(data.adminPassword);
    changed = true;
  }

  // 2) 구 투표 형식 { "이름": ["id"] } → 신 형식 { "token": { voterName, votes: [] } }
  const migratedVotes = {};
  for (const [key, val] of Object.entries(data.votes || {})) {
    if (Array.isArray(val)) {
      migratedVotes[`migrated-${uuidv4()}`] = { voterName: key, votes: val };
      changed = true;
    } else {
      migratedVotes[key] = val;
    }
  }
  data.votes = migratedVotes;

  // 3) 기존 작품에 uploaderToken 필드 추가
  for (const work of data.works || []) {
    if (work.uploaderToken === undefined) {
      work.uploaderToken = null; // 기존 작품은 업로더 미상
      changed = true;
    }
  }

  if (changed) saveData(data);
}

// ── 데이터 I/O ──
function getData() {
  return JSON.parse(fs.readFileSync(DATA_FILE, 'utf-8'));
}
function saveData(data) {
  fs.writeFileSync(DATA_FILE, JSON.stringify(data, null, 2));
}

// ── 동시 쓰기 방지 (간단한 인메모리 락) ──
let dataLock = false;
async function modifyData(fn) {
  while (dataLock) await new Promise(r => setTimeout(r, 10));
  dataLock = true;
  try {
    const data = getData();
    const result = fn(data);
    saveData(data);
    return result;
  } finally {
    dataLock = false;
  }
}

// ── Rate Limiting ──
const generalLimiter = rateLimit({ windowMs: 15 * 60 * 1000, max: 300, standardHeaders: true, legacyHeaders: false });
const voteLimiter   = rateLimit({ windowMs: 60 * 1000, max: 20,  standardHeaders: true, legacyHeaders: false });
const adminLimiter  = rateLimit({ windowMs: 15 * 60 * 1000, max: 40,  standardHeaders: true, legacyHeaders: false });
const uploadLimiter = rateLimit({ windowMs: 60 * 1000, max: 10,  standardHeaders: true, legacyHeaders: false });

// ── 이미지 업로드 설정 ──
const ALLOWED_IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'];

const storage = multer.diskStorage({
  destination: UPLOAD_DIR,
  filename: (req, file, cb) => {
    const ext = path.extname(file.originalname).toLowerCase();
    cb(null, uuidv4() + ext);
  }
});
const upload = multer({
  storage,
  limits: { fileSize: 30 * 1024 * 1024 },
  fileFilter: (req, file, cb) => {
    const ext = path.extname(file.originalname).toLowerCase();
    if (file.mimetype.startsWith('image/') && ALLOWED_IMAGE_EXTS.includes(ext)) {
      cb(null, true);
    } else {
      cb(new Error('이미지 파일만 업로드 가능합니다. (jpg, png, gif, webp, bmp)'));
    }
  }
});

// ── 자료 파일 업로드 설정 ──
const ALLOWED_DOC_EXTS = ['.hwp', '.hwpx', '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.txt', '.zip'];
const docStorage = multer.diskStorage({
  destination: DOCS_DIR,
  filename: (req, file, cb) => {
    const ext = path.extname(file.originalname).toLowerCase();
    const base = path.basename(file.originalname, ext)
      .replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 60);
    cb(null, uuidv4().slice(0, 8) + '_' + base + ext);
  }
});
const docUpload = multer({
  storage: docStorage,
  limits: { fileSize: 50 * 1024 * 1024 },
  fileFilter: (req, file, cb) => {
    const ext = path.extname(file.originalname).toLowerCase();
    if (ALLOWED_DOC_EXTS.includes(ext)) cb(null, true);
    else cb(new Error('허용되지 않는 파일 형식입니다.'));
  }
});

// ── 입력값 정리 헬퍼 ──
function sanitize(str, maxLen = 100) {
  return String(str || '').trim().slice(0, maxLen);
}

app.use(express.json());
app.use(express.static('public'));
app.use(generalLimiter);

// ── 작품 목록 조회 ──
app.get('/api/works', (req, res) => {
  const data = getData();
  const worksWithVotes = data.works.map(w => ({
    ...w,
    voteCount: Object.values(data.votes).filter(v => {
      const votes = Array.isArray(v) ? v : (v.votes || []);
      return votes.includes(w.id);
    }).length
  }));
  worksWithVotes.sort((a, b) => b.voteCount - a.voteCount || new Date(a.uploadedAt) - new Date(b.uploadedAt));
  res.json(worksWithVotes);
});

// ── 내 투표 현황 조회 (토큰 기반) ──
app.get('/api/votes/me', (req, res) => {
  const token = req.headers['x-vote-token'];
  if (!token) return res.json({ votes: [] });
  const data = getData();
  const record = data.votes[token];
  const votes = record ? (Array.isArray(record) ? record : record.votes || []) : [];
  res.json({ votes });
});

// ── 작품 업로드 ──
app.post('/api/upload', uploadLimiter, upload.single('image'), async (req, res) => {
  try {
    const author = sanitize(req.body.author, 50);
    const title  = sanitize(req.body.title,  100);
    const uploaderToken = sanitize(req.body.uploaderToken, 200);

    if (!req.file || !author) {
      return res.status(400).json({ error: '이름과 이미지를 모두 입력해주세요.' });
    }

    const work = {
      id: uuidv4(),
      author,
      title: title || `${author}의 웰페이퍼`,
      filename: req.file.filename,
      uploaderToken: uploaderToken || null,
      uploadedAt: new Date().toISOString()
    };

    await modifyData(data => { data.works.push(work); });
    res.json({ success: true, work });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── 투표 ──
app.post('/api/vote', voteLimiter, async (req, res) => {
  const { voterName, workId } = req.body;
  const token = req.headers['x-vote-token'];

  if (!workId || !token) {
    return res.status(400).json({ error: '필수 정보가 누락됐습니다.' });
  }

  let result;
  try {
    result = await modifyData(data => {
      // 투표 종료 체크
      if (data.votingEnded) {
        return { error: '투표가 종료됐습니다.', status: 400 };
      }

      // 작품 존재 확인
      const work = data.works.find(w => w.id === workId);
      if (!work) return { error: '존재하지 않는 작품입니다.', status: 404 };

      // 본인 작품 투표 방지
      if (work.uploaderToken && work.uploaderToken === token) {
        return { error: '본인의 작품에는 투표할 수 없습니다.', status: 400 };
      }

      // 토큰 기반 투표 레코드 초기화
      if (!data.votes[token]) {
        data.votes[token] = { voterName: sanitize(voterName || '익명', 50), votes: [] };
      }
      const tokenRecord = data.votes[token];
      const tokenVotes = tokenRecord.votes;

      // 토글 (투표 취소)
      if (tokenVotes.includes(workId)) {
        tokenRecord.votes = tokenVotes.filter(id => id !== workId);
        return { success: true, action: 'removed', myVotes: tokenRecord.votes };
      }

      // 최대 2표 제한
      if (tokenVotes.length >= 2) {
        return { error: '최대 2개 작품에만 투표할 수 있습니다.', status: 400 };
      }

      tokenRecord.votes.push(workId);
      return { success: true, action: 'added', myVotes: tokenRecord.votes };
    });
  } catch (err) {
    return res.status(500).json({ error: '서버 오류가 발생했습니다.' });
  }

  if (result.error) return res.status(result.status || 400).json({ error: result.error });
  res.json(result);
});

// ── 결과 조회 ──
app.get('/api/results', (req, res) => {
  const data = getData();
  const results = data.works.map(w => ({
    id: w.id,
    author: w.author,
    title: w.title,
    filename: w.filename,
    voteCount: Object.values(data.votes).filter(v => {
      const votes = Array.isArray(v) ? v : (v.votes || []);
      return votes.includes(w.id);
    }).length
  }));
  results.sort((a, b) => b.voteCount - a.voteCount);
  const totalVoters = Object.keys(data.votes).length;
  res.json({ results, totalVoters });
});

// ── 투표 상태 조회 ──
app.get('/api/status', (req, res) => {
  const data = getData();
  res.json({ votingEnded: data.votingEnded || false });
});

// ── 어드민: 비밀번호 확인 ──
app.post('/api/admin/verify', adminLimiter, (req, res) => {
  const { password } = req.body;
  const data = getData();
  res.json({ valid: checkPassword(password, data.adminPassword) });
});

// ── 어드민: 비밀번호 변경 ──
app.post('/api/admin/change-password', adminLimiter, async (req, res) => {
  const { current, newPassword } = req.body;
  const data = getData();
  if (!checkPassword(current, data.adminPassword)) {
    return res.status(403).json({ error: '현재 비밀번호가 틀렸습니다.' });
  }
  if (!newPassword || newPassword.length < 4) {
    return res.status(400).json({ error: '새 비밀번호는 4자 이상이어야 합니다.' });
  }
  await modifyData(d => { d.adminPassword = hashPassword(newPassword); });
  res.json({ success: true });
});

// ── 어드민: 투표 종료/재개 토글 ──
app.post('/api/admin/end-voting', adminLimiter, async (req, res) => {
  const { password } = req.body;
  const data = getData();
  if (!checkPassword(password, data.adminPassword)) {
    return res.status(403).json({ error: '비밀번호가 틀렸습니다.' });
  }
  const newState = !data.votingEnded;
  await modifyData(d => { d.votingEnded = newState; });
  res.json({ success: true, votingEnded: newState });
});

// ── 어드민: 전체 초기화 ──
app.delete('/api/admin/reset', adminLimiter, async (req, res) => {
  const { password } = req.body;
  const data = getData();
  if (!checkPassword(password, data.adminPassword)) {
    return res.status(403).json({ error: '비밀번호가 틀렸습니다.' });
  }
  if (fs.existsSync(UPLOAD_DIR)) {
    fs.readdirSync(UPLOAD_DIR).forEach(f => {
      try { fs.unlinkSync(path.join(UPLOAD_DIR, f)); } catch {}
    });
  }
  await modifyData(d => { d.works = []; d.votes = {}; });
  res.json({ success: true });
});

// ── 어드민: 작품 삭제 ──
app.delete('/api/admin/work/:id', adminLimiter, async (req, res) => {
  const { password } = req.body;
  const data = getData();
  if (!checkPassword(password, data.adminPassword)) {
    return res.status(403).json({ error: '비밀번호가 틀렸습니다.' });
  }
  const work = data.works.find(w => w.id === req.params.id);
  if (!work) return res.status(404).json({ error: '작품을 찾을 수 없습니다.' });

  try { fs.unlinkSync(path.join(UPLOAD_DIR, work.filename)); } catch {}

  await modifyData(d => {
    d.works = d.works.filter(w => w.id !== req.params.id);
    for (const token of Object.keys(d.votes)) {
      const rec = d.votes[token];
      if (Array.isArray(rec)) {
        d.votes[token] = rec.filter(id => id !== req.params.id);
      } else if (rec.votes) {
        rec.votes = rec.votes.filter(id => id !== req.params.id);
      }
    }
  });
  res.json({ success: true });
});

// ── 어드민: 데이터 백업 다운로드 ──
app.get('/api/admin/export', adminLimiter, (req, res) => {
  const pw = req.headers['x-admin-password'];
  const data = getData();
  if (!checkPassword(pw, data.adminPassword)) {
    return res.status(403).json({ error: '비밀번호가 틀렸습니다.' });
  }
  const filename = `backup-${new Date().toISOString().slice(0, 10)}.json`;
  res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
  res.setHeader('Content-Type', 'application/json');
  res.json(data);
});

// ── 자료 목록 조회 ──
app.get('/api/docs', (req, res) => {
  const files = fs.existsSync(DOCS_DIR)
    ? fs.readdirSync(DOCS_DIR).map(f => {
        const stat = fs.statSync(path.join(DOCS_DIR, f));
        const displayName = f.replace(/^[0-9a-f]{8}_/, '');
        return { filename: f, displayName, size: stat.size, uploadedAt: stat.mtime };
      }).sort((a, b) => new Date(b.uploadedAt) - new Date(a.uploadedAt))
    : [];
  res.json(files);
});

// ── 자료 다운로드 ──
app.get('/api/docs/download/:filename', (req, res) => {
  const filename = path.basename(req.params.filename);
  const filepath = path.join(DOCS_DIR, filename);
  if (!fs.existsSync(filepath)) return res.status(404).json({ error: '파일을 찾을 수 없습니다.' });
  const displayName = filename.replace(/^[0-9a-f]{8}_/, '');
  res.download(filepath, displayName);
});

// ── 어드민: 자료 업로드 ──
app.post('/api/admin/docs/upload', adminLimiter, (req, res, next) => {
  const pw = req.headers['x-admin-password'];
  const data = getData();
  if (!checkPassword(pw, data.adminPassword)) return res.status(403).json({ error: '비밀번호가 틀렸습니다.' });
  next();
}, docUpload.single('file'), (req, res) => {
  if (!req.file) return res.status(400).json({ error: '파일이 없습니다.' });
  const displayName = req.file.filename.replace(/^[0-9a-f]{8}_/, '');
  res.json({ success: true, filename: req.file.filename, displayName });
});

// ── 어드민: 자료 삭제 ──
app.delete('/api/admin/docs/:filename', adminLimiter, (req, res) => {
  const { password } = req.body;
  const data = getData();
  if (!checkPassword(password, data.adminPassword)) return res.status(403).json({ error: '비밀번호가 틀렸습니다.' });
  const filename = path.basename(req.params.filename);
  const filepath = path.join(DOCS_DIR, filename);
  if (!fs.existsSync(filepath)) return res.status(404).json({ error: '파일을 찾을 수 없습니다.' });
  try { fs.unlinkSync(filepath); } catch {}
  res.json({ success: true });
});

// ── 캐릭터 이미지 제공 ──
app.get('/강이.png',    (req, res) => res.sendFile(path.resolve('./강이.png')));
app.get('/cursor.png',  (req, res) => res.sendFile(path.resolve('./강이.png')));
app.get('/건강균덩.png', (req, res) => res.sendFile(path.resolve('./건강균덩.png')));
app.get('/character.png', (_req, res) => res.sendFile(path.resolve('./건강균덩.png')));

// ── 서버 시작 ──
migrateData();

app.listen(PORT, '0.0.0.0', () => {
  console.log('');
  console.log('==========================================');
  console.log('  AI 웰페이퍼 공모전 사이트 시작!');
  console.log('==========================================');
  console.log(`  로컬:  http://localhost:${PORT}`);

  const { networkInterfaces } = require('os');
  const nets = networkInterfaces();
  for (const name of Object.keys(nets)) {
    for (const net of nets[name]) {
      if (net.family === 'IPv4' && !net.internal) {
        console.log(`  네트워크: http://${net.address}:${PORT}`);
      }
    }
  }
  console.log('==========================================\n');
});
