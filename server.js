const { spawn } = require('child_process');

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: 'inherit', env: process.env });
    child.on('exit', code => code === 0 ? resolve() : reject(new Error(`${command} exited with ${code}`)));
    child.on('error', reject);
  });
}

async function main() {
  try {
    await run('python3', ['-c', 'import boto3']);
  } catch {
    console.log('[setup] boto3가 없어 실행 전에 설치합니다.');
    await run('python3', ['-m', 'pip', 'install', '-r', 'requirements.txt', '--user']);
  }

  const child = spawn('python3', ['server.py'], {
    stdio: 'inherit',
    env: process.env
  });

  child.on('exit', code => {
    process.exit(code ?? 1);
  });
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
