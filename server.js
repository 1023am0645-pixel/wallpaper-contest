const { spawn } = require('child_process');

const child = spawn('python3', ['server.py'], {
  stdio: 'inherit',
  env: process.env
});

child.on('exit', code => {
  process.exit(code ?? 1);
});
