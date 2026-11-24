// {{TITLE}} -- the page's behaviour
const statusEl = document.getElementById('status');
document.getElementById('action').addEventListener('click', () => {
  statusEl.textContent = 'Started at ' + new Date().toLocaleTimeString();
});
