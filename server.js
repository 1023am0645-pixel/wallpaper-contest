const express = require('express');
const multer = require('multer');
const fs = require('fs');
const path = require('path');
const { v4: uuidv4 } = require('uuid');

const app = express();
const PORT = 3000;
const DATA_FILE = './data.json';
const UPLOAD_DIR = './public/uploads';
const DOCS_DIR = './public/docs';

// 디렉토리 초기화
if (!fs.existsSync(UPLOAD_DIR)) fs.mkdirSync(UPLOAD_DIR, { recursive: true });
if (!fs.existsSync(DOCS_DIR)) fs.mkdirSync(DOCS_DIR, { recursive: true });
if (!fs.existsSync(DATA_FILE)) {
  fs.writeFileSync(DATA_FILE, JSON.stringify({ works: [], votes: {}, adminPassword: 'admin1234' }, null, 2));
}

function getData() {
  return JSON.parse(fs.readFileSync(DATA_FILE, 'utf-8'));
}

function saveData(data) {
  fs.writeFileSync(DATA_FILE, JSON.stringify(data, null, 2));
}

// 이미지 업로드 설정
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
    if (file.mimetype.startsWith('image/')) cb(null, true);
    else cb(new Error('이미지 파일만 업로드 가능합니다.'));
  }
});

// 자료 파일 업로드 설정 (한글, PDF, Word, PPT 등)
const ALLOWED_DOC_EXTS = ['.hwp', '.hwpx', '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.txt', '.zip'];
const docStorage = multer.diskStorage({
  destination: DOCS_DIR,
  filename: (req, file, cb) => {
    // 원본 파일명 유지 (중복 방지를 위해 uuid 접두사)
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

app.use(express.json());
app.use(express.static('public'));

// 작품 목록 조회
app.get('/api/works', (req, res) => {
  const data = getData();
  const worksWithVotes = data.works.map(w => ({
    ...w,
    voteCount: Object.values(data.votes).filter(v => v.includes(w.id)).length
  }));
  worksWithVotes.sort((a, b) => b.voteCount - a.voteCount || new Date(a.uploadedAt) - new Date(b.uploadedAt));
  res.json(worksWithVotes);
});

// 투표 현황 조회 (특정 투표자)
app.get('/api/votes/:voterName', (req, res) => {
  const data = getData();
  const votes = data.votes[req.params.voterName.trim()] || [];
  res.json({ votes });
});

// 작품 업로드
app.post('/api/upload', upload.single('image'), (req, res) => {
  try {
    const { author, title } = req.body;
    if (!req.file || !author || !author.trim()) {
      return res.status(400).json({ error: '이름과 이미지를 모두 입력해주세요.' });
    }

    const data = getData();
    const work = {
      id: uuidv4(),
      author: author.trim(),
      title: title && title.trim() ? title.trim() : `${author.trim()}의 웰페이퍼`,
      filename: req.file.filename,
      uploadedAt: new Date().toISOString()
    };

    data.works.push(work);
    saveData(data);
    res.json({ success: true, work });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// 투표
app.post('/api/vote', (req, res) => {
  const { voterName, workId } = req.body;
  if (!voterName || !workId) {
    return res.status(400).json({ error: '이름과 작품 ID가 필요합니다.' });
  }

  const data = getData();
  const voter = voterName.trim();

  // 작품 존재 확인
  const workExists = data.works.some(w => w.id === workId);
  if (!workExists) return res.status(404).json({ error: '존재하지 않는 작품입니다.' });

  if (!data.votes[voter]) data.votes[voter] = [];

  // 이미 투표했으면 취소 (토글)
  if (data.votes[voter].includes(workId)) {
    data.votes[voter] = data.votes[voter].filter(id => id !== workId);
    saveData(data);
    return res.json({ success: true, action: 'removed', myVotes: data.votes[voter] });
  }

  // 최대 2표 제한
  if (data.votes[voter].length >= 2) {
    return res.status(400).json({ error: '최대 2개 작품에만 투표할 수 있습니다.' });
  }

  data.votes[voter].push(workId);
  saveData(data);
  res.json({ success: true, action: 'added', myVotes: data.votes[voter] });
});

// 결과 조회 (투표 마감 후 공개용)
app.get('/api/results', (req, res) => {
  const data = getData();
  const results = data.works.map(w => ({
    id: w.id,
    author: w.author,
    title: w.title,
    filename: w.filename,
    voteCount: Object.values(data.votes).filter(v => v.includes(w.id)).length
  }));
  results.sort((a, b) => b.voteCount - a.voteCount);
  const totalVoters = Object.keys(data.votes).length;
  res.json({ results, totalVoters });
});

// 어드민: 전체 초기화
app.delete('/api/admin/reset', (req, res) => {
  const { password } = req.body;
  const data = getData();
  if (password !== data.adminPassword) {
    return res.status(403).json({ error: '비밀번호가 틀렸습니다.' });
  }

  // 업로드 파일 삭제
  if (fs.existsSync(UPLOAD_DIR)) {
    fs.readdirSync(UPLOAD_DIR).forEach(f => {
      try { fs.unlinkSync(path.join(UPLOAD_DIR, f)); } catch {}
    });
  }

  saveData({ works: [], votes: {}, adminPassword: data.adminPassword });
  res.json({ success: true });
});

// 어드민: 작품 삭제
app.delete('/api/admin/work/:id', (req, res) => {
  const { password } = req.body;
  const data = getData();
  if (password !== data.adminPassword) {
    return res.status(403).json({ error: '비밀번호가 틀렸습니다.' });
  }

  const work = data.works.find(w => w.id === req.params.id);
  if (!work) return res.status(404).json({ error: '작품을 찾을 수 없습니다.' });

  // 파일 삭제
  try { fs.unlinkSync(path.join(UPLOAD_DIR, work.filename)); } catch {}

  // 관련 투표도 제거
  Object.keys(data.votes).forEach(voter => {
    data.votes[voter] = data.votes[voter].filter(id => id !== work.id);
  });

  data.works = data.works.filter(w => w.id !== req.params.id);
  saveData(data);
  res.json({ success: true });
});

// ── 자료 다운로드 API ──

// 자료 목록 조회
app.get('/api/docs', (req, res) => {
  const files = fs.existsSync(DOCS_DIR)
    ? fs.readdirSync(DOCS_DIR).map(f => {
        const stat = fs.statSync(path.join(DOCS_DIR, f));
        // 파일명에서 uuid 접두사 제거해 표시명 생성
        const displayName = f.replace(/^[0-9a-f]{8}_/, '');
        return { filename: f, displayName, size: stat.size, uploadedAt: stat.mtime };
      }).sort((a, b) => new Date(b.uploadedAt) - new Date(a.uploadedAt))
    : [];
  res.json(files);
});

// 자료 다운로드
app.get('/api/docs/download/:filename', (req, res) => {
  const filename = path.basename(req.params.filename);
  const filepath = path.join(DOCS_DIR, filename);
  if (!fs.existsSync(filepath)) return res.status(404).json({ error: '파일을 찾을 수 없습니다.' });
  const displayName = filename.replace(/^[0-9a-f]{8}_/, '');
  res.download(filepath, displayName);
});

// 어드민: 자료 업로드
app.post('/api/admin/docs/upload', (req, res, next) => {
  const pw = req.headers['x-admin-password'];
  const data = getData();
  if (pw !== data.adminPassword) return res.status(403).json({ error: '비밀번호가 틀렸습니다.' });
  next();
}, docUpload.single('file'), (req, res) => {
  if (!req.file) return res.status(400).json({ error: '파일이 없습니다.' });
  const displayName = req.file.filename.replace(/^[0-9a-f]{8}_/, '');
  res.json({ success: true, filename: req.file.filename, displayName });
});

// 어드민: 자료 삭제
app.delete('/api/admin/docs/:filename', (req, res) => {
  const { password } = req.body;
  const data = getData();
  if (password !== data.adminPassword) return res.status(403).json({ error: '비밀번호가 틀렸습니다.' });
  const filename = path.basename(req.params.filename);
  const filepath = path.join(DOCS_DIR, filename);
  if (!fs.existsSync(filepath)) return res.status(404).json({ error: '파일을 찾을 수 없습니다.' });
  try { fs.unlinkSync(filepath); } catch {}
  res.json({ success: true });
});

// 캐릭터 이미지 제공 (ASCII 경로도 추가)
app.get('/강이.png', (req, res) => res.sendFile(path.resolve('./강이.png')));
app.get('/cursor.png', (req, res) => res.sendFile(path.resolve('./강이.png')));
app.get('/건강균덩.png', (req, res) => res.sendFile(path.resolve('./건강균덩.png')));

// 투표 상태 조회
app.get('/api/status', (req, res) => {
  const data = getData();
  res.json({ votingEnded: data.votingEnded || false });
});

// 어드민: 비밀번호 확인
app.post('/api/admin/verify', (req, res) => {
  const { password } = req.body;
  const data = getData();
  res.json({ valid: password === data.adminPassword });
});

// 어드민: 투표 종료/재개 토글
app.post('/api/admin/end-voting', (req, res) => {
  const { password } = req.body;
  const data = getData();
  if (password !== data.adminPassword) {
    return res.status(403).json({ error: '비밀번호가 틀렸습니다.' });
  }
  data.votingEnded = !data.votingEnded;
  saveData(data);
  res.json({ success: true, votingEnded: data.votingEnded });
});

app.listen(PORT, '0.0.0.0', () => {
  console.log('');
  console.log('==========================================');
  console.log('  AI 웰페이퍼 공모전 사이트 시작!');
  console.log('==========================================');
  console.log(`  로컬:  http://localhost:${PORT}`);

  // 네트워크 IP 출력
  const { networkInterfaces } = require('os');
  const nets = networkInterfaces();
  for (const name of Object.keys(nets)) {
    for (const net of nets[name]) {
      if (net.family === 'IPv4' && !net.internal) {
        console.log(`  네트워크: http://${net.address}:${PORT}`);
      }
    }
  }
  console.log('==========================================');
  console.log('  어드민 비밀번호: admin1234');
  console.log('==========================================\n');
});
