import RFB from './vendor/novnc/core/rfb.js';

const screen = document.querySelector('#screen');
const state = document.querySelector('#state');
const button = document.querySelector('#control');
const error = document.querySelector('#error');
const realm = location.pathname.split('/')[2];
const fragment = new URLSearchParams(location.hash.slice(1));
const token = fragment.get('ticket');
// A view-only terminal link has no takeover capability; don't offer one.
const viewOnly = fragment.get('view') === '1';
if (viewOnly) button.hidden = true;
// Capabilities never remain in history, requests, Referer, or browser storage.
history.replaceState(null, '', location.pathname);
let connection;
let socket;
let generation = 0;
let control = false;
let interruptedControl = false;
let retries = 0;
let retryTimer;
let stopped = false;
const recoveryMessage = 'Human control was interrupted. Agent access remains paused. Take over again, then Hand back to resume agent access.';
function disconnect(intentional = false) {
  // noVNC's default close has no status (1005), which is NOT a handback.
  if (intentional && socket?.readyState === WebSocket.OPEN) socket.close(1000, 'Hand back');
  connection?.disconnect();
}
function fail(message) {
  clearTimeout(retryTimer);
  state.textContent = 'Disconnected';
  button.disabled = true;
  error.hidden = false;
  error.textContent = interruptedControl ? `${recoveryMessage} ${message}` : message;
  document.body.dataset.connected = 'false';
}
function connect() {
  if (stopped) return;
  clearTimeout(retryTimer);
  const current = ++generation;
  disconnect();
  button.disabled = true;
  state.textContent = 'Connecting…';
  error.hidden = true;
  const url = new URL(`/api/realms/${encodeURIComponent(realm)}/vnc`, location.origin);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  if (control) url.searchParams.set('control', '1');
  socket = new WebSocket(url.href, ['binary', `realm.${token}`]);
  connection = new RFB(screen, socket);
  socket.addEventListener('close', event => {
    if (current !== generation || stopped || event.code !== 1008) return;
    interruptedControl ||= control;
    control = false;
    stopped = true;
    document.body.dataset.control = 'false';
    fail('Viewer authorization ended. Use Full view (/realm full) to open a current view.');
  });
  connection.viewOnly = !control;
  connection.scaleViewport = true;
  connection.resizeSession = false;
  connection.showDotCursor = true;
  connection.addEventListener('connect', () => {
    if (current !== generation) return;
    retries = 0;
    if (control) interruptedControl = false;
    document.body.dataset.connected = 'true';
    document.body.dataset.control = String(control);
    state.textContent = control ? 'Your control · live' : interruptedControl ? 'Human control held · view only' : 'Live · view only';
    button.textContent = control ? 'Hand back' : 'Take over';
    button.disabled = false;
    error.hidden = !interruptedControl;
    error.textContent = interruptedControl ? recoveryMessage : '';
  });
  connection.addEventListener('disconnect', () => {
    if (current !== generation || stopped) return;
    // A lost takeover lease must never be reclaimed without another click.
    interruptedControl ||= control;
    control = false;
    document.body.dataset.connected = 'false';
    document.body.dataset.control = 'false';
    button.disabled = true;
    if (retries >= 5) {
      fail('The realm stopped or viewer authorization expired. Use Full view (/realm full) to reconnect.');
      return;
    }
    state.textContent = 'Reconnecting…';
    retryTimer = setTimeout(connect, 1000 * 2 ** retries++);
  });
  connection.addEventListener('securityfailure', () => {
    if (current !== generation) return;
    stopped = true;
    fail('Viewer authorization failed. Open a fresh link with /realm full.');
  });
}
window.addEventListener('pagehide', () => {
  stopped = true;
  generation++;
  clearTimeout(retryTimer);
  disconnect(true);
});
button.addEventListener('click', () => {
  generation++;
  disconnect(true);
  if (control) interruptedControl = false;
  control = !control;
  connect();
});
if (!token || !/^[A-Za-z0-9_-]{40,128}$/.test(token)) fail('Open this viewer with /realm full. A current realm link is required.');
else connect();
