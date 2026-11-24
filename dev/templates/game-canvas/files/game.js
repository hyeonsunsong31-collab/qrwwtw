// {{TITLE}} -- game loop, input and drawing
const canvas = document.getElementById('game');
const ctx = canvas.getContext('2d');
const keys = new Set();
addEventListener('keydown', (e) => { keys.add(e.code); if (e.code.startsWith('Arrow') || e.code === 'Space') e.preventDefault(); });
addEventListener('keyup', (e) => keys.delete(e.code));

const player = { x: 400, y: 250, size: 24, speed: 240 };
let coin = spawn();
let score = 0;
let last = performance.now();

function spawn() {
  return { x: 30 + Math.random() * (canvas.width - 60), y: 30 + Math.random() * (canvas.height - 60), r: 10 };
}

function update(dt) {
  const s = player.speed * dt;
  if (keys.has('ArrowLeft') || keys.has('KeyA')) player.x -= s;
  if (keys.has('ArrowRight') || keys.has('KeyD')) player.x += s;
  if (keys.has('ArrowUp') || keys.has('KeyW')) player.y -= s;
  if (keys.has('ArrowDown') || keys.has('KeyS')) player.y += s;
  player.x = Math.max(0, Math.min(canvas.width - player.size, player.x));
  player.y = Math.max(0, Math.min(canvas.height - player.size, player.y));
  const cx = player.x + player.size / 2, cy = player.y + player.size / 2;
  if (Math.hypot(cx - coin.x, cy - coin.y) < coin.r + player.size / 2) {
    score += 1;
    document.getElementById('score').textContent = score;
    coin = spawn();
  }
}

function draw(t) {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#facc15';
  ctx.beginPath();
  ctx.arc(coin.x, coin.y, coin.r + Math.sin(t / 200) * 2, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#38bdf8';
  ctx.fillRect(player.x, player.y, player.size, player.size);
}

function frame(t) {
  const dt = Math.min(0.05, (t - last) / 1000);
  last = t;
  update(dt);
  draw(t);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
