/* Shared helpers for every BGV page: API access, escaping, toasts, theme. */

// Served by the backend -> same origin. Opened from disk or a dev server (Live Server) -> talk to :8000.
const API_BASE = (location.protocol === 'file:' || ['5500', '5501', '3000'].includes(location.port))
  ? 'http://localhost:8000/api'
  : '/api';

// Everything that comes from the database or an uploaded spreadsheet must go through esc()
// before being placed in innerHTML.
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

// fetch + JSON + readable errors. Throws Error(message) for network failures and non-2xx.
async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, options);
  } catch (err) {
    throw new Error('Could not reach the server. Is the backend running?');
  }

  let data = null;
  try { data = await res.json(); } catch (_) { /* empty / non-JSON body */ }

  if (!res.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map(d => d.msg).join('; ');
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return data;
}

function postJson(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
}

function postFile(path, file) {
  const formData = new FormData();
  formData.append('file', file);
  return api(path, { method: 'POST', body: formData });
}

function showToast(message, type = 'success') {
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container';
    container.setAttribute('aria-live', 'polite');
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.setAttribute('role', type === 'error' ? 'alert' : 'status');

  const icon = document.createElement('span');
  icon.className = 'toast-icon';
  icon.textContent = type === 'success' ? '✓' : '!';
  const text = document.createElement('span');
  text.textContent = message;
  toast.append(icon, text);
  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.add('toast-out');
    setTimeout(() => toast.remove(), 200);
  }, type === 'error' ? 6000 : 3500);
}

function updateToggleIcon() {
  const btn = document.getElementById('themeToggle');
  if (!btn) return;
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  btn.textContent = dark ? '☀️' : '🌙';
  btn.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
}

function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('bgv-theme', next); } catch (_) { /* private mode */ }
  updateToggleIcon();
}

document.addEventListener('DOMContentLoaded', updateToggleIcon);

function statusClass(status) {
  if (status === 'completed') return 'verified';
  if (status === 'partial') return 'partial';
  if (status === 'failed') return 'failed';
  return 'pending';
}

function statusLabel(status) {
  return { completed: 'Verified', partial: 'Partial', failed: 'Failed', processing: 'Processing' }[status] || 'Pending';
}

function formatDate(dateStr, withTime = false) {
  if (!dateStr) return '—';
  // The API sends naive UTC timestamps; mark them as UTC so they show in local time.
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(dateStr) ? dateStr : `${dateStr}Z`);
  if (isNaN(d)) return '—';
  const date = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  return withTime ? `${date} · ${d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}` : date;
}

// Poll a batch until it stops processing. Keeps working after a page refresh because the
// state lives on the server. Returns a function that stops the polling.
function watchBatch(batchId, onUpdate, onDone, intervalMs = 2500) {
  let stopped = false;
  async function tick() {
    if (stopped) return;
    try {
      const s = await api(`/candidates/batch/${batchId}/status`);
      onUpdate(s);
      if (s.processing_status !== 'processing') {
        stopped = true;
        onDone && onDone(s);
        return;
      }
    } catch (err) {
      console.error('Batch status poll failed:', err);
    }
    setTimeout(tick, intervalMs);
  }
  tick();
  return () => { stopped = true; };
}
