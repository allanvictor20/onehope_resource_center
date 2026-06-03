// ── BACKEND CONFIG ──
// Local development: http://localhost:8000
// Production: https://your-deployed-url.com
const BACKEND_URL = 'http://localhost:8000';

// ── STATE ──
let conversationHistory = [];
let currentChatId = null;
let currentChatTitle = null;
let isLoading = false;
let _pendingMessages = [];

// Chat sessions persisted in localStorage
let chatSessions = JSON.parse(localStorage.getItem('onehope_chats') || '[]');

// ── WELCOME SCREEN HTML (reused by newChat) ──
const WELCOME_HTML = `
  <div class="welcome-screen" id="welcomeScreen">
    <div class="welcome-icon"><span>O</span></div>
    <h2>Welcome to <em>OneHope</em> Resources</h2>
    <p>I know every resource in the OneHope library — manuals, VDS documents, training materials, movies, brochures and more. Ask me anything and I'll find it for you.</p>
    <div class="suggestion-grid">
      <button class="suggestion-card" onclick="sendSuggestion(this)">
        <span class="s-icon">🌱</span>
        <span class="s-text">I'm a new volunteer — walk me through OneHope's main programmes</span>
      </button>
      <button class="suggestion-card" onclick="sendSuggestion(this)">
        <span class="s-icon">🎬</span>
        <span class="s-text">What movies does OneHope have available?</span>
      </button>
      <button class="suggestion-card" onclick="sendSuggestion(this)">
        <span class="s-icon">📣</span>
        <span class="s-text">Get me vision casting materials for Pastors</span>
      </button>
      <button class="suggestion-card" onclick="sendSuggestion(this)">
        <span class="s-icon">🌍</span>
        <span class="s-text">What resources do we have in Luganda?</span>
      </button>
    </div>
  </div>
`;

// ── SIDEBAR ──
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebarOverlay').classList.toggle('show');
}

function closeSidebarMobile() {
  if (window.innerWidth <= 768) {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebarOverlay').classList.remove('show');
  }
}

// ── RENDER SIDEBAR HISTORY ──
function renderSidebar() {
  const container = document.getElementById('sidebarChats');
  if (chatSessions.length === 0) {
    container.innerHTML = '<div class="no-history">No conversations yet</div>';
    return;
  }
  container.innerHTML = '';
  [...chatSessions].reverse().forEach(session => {
    const item = document.createElement('div');
    item.className = 'chat-item' + (session.id === currentChatId ? ' active' : '');
    item.dataset.id = session.id;
    item.innerHTML = `
      <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>
      <span class="chat-item-text">${escapeHtml(session.title)}</span>
      <button class="chat-item-delete" onclick="deleteChat(event, '${session.id}')" title="Delete">
        <svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
      </button>
    `;
    item.addEventListener('click', (e) => {
      if (e.target.closest('.chat-item-delete')) return;
      loadChat(session.id);
    });
    container.appendChild(item);
  });
}

function deleteChat(e, id) {
  e.stopPropagation();
  chatSessions = chatSessions.filter(s => s.id !== id);
  saveSessions();
  if (currentChatId === id) newChat();
  else renderSidebar();
}

function saveSessions() {
  localStorage.setItem('onehope_chats', JSON.stringify(chatSessions));
  renderSidebar();
}

// ── LOAD A PAST CHAT ──
function loadChat(id) {
  const session = chatSessions.find(s => s.id === id);
  if (!session) return;

  currentChatId = id;
  currentChatTitle = session.title;
  conversationHistory = [...session.history];
  _pendingMessages = [...session.messages];

  document.getElementById('topbarTitle').textContent = session.title;

  const messagesEl = document.getElementById('messages');
  messagesEl.innerHTML = '';
  session.messages.forEach(msg => {
    if (msg.role === 'user') appendUserBubble(msg.content);
    else appendAiBubble(msg.content, msg.sources || [], [], false);
  });

  renderSidebar();
  scrollToBottom();
  closeSidebarMobile();
}

// ── NEW CHAT ──
function newChat() {
  saveCurrentChat();
  currentChatId = null;
  currentChatTitle = null;
  conversationHistory = [];
  _pendingMessages = [];

  document.getElementById('topbarTitle').textContent = 'OneHope Resource Centre';
  document.getElementById('messages').innerHTML = WELCOME_HTML;

  renderSidebar();
  closeSidebarMobile();
  document.getElementById('userInput').focus();
}

// ── SAVE CURRENT CHAT ──
function saveCurrentChat() {
  if (!currentChatId || _pendingMessages.length === 0) return;
  const existing = chatSessions.find(s => s.id === currentChatId);
  if (existing) {
    existing.history = [...conversationHistory];
    existing.messages = [..._pendingMessages];
    existing.title = currentChatTitle;
  } else {
    chatSessions.push({
      id: currentChatId,
      title: currentChatTitle,
      history: [...conversationHistory],
      messages: [..._pendingMessages],
    });
  }
  saveSessions();
}

// ── DOM HELPERS ──
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function formatText(text) {
  if (!text) return '';
  let result = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  result = result.replace(/\*(.*?)\*/g, '<em>$1</em>');
  result = result.split('\n').map(line => line.trim() ? `<p>${line}</p>` : '').join('');
  return result || `<p>${text}</p>`;
}

function scrollToBottom() {
  const wrap = document.getElementById('messagesWrap');
  wrap.scrollTop = wrap.scrollHeight;
}

function hideWelcome() {
  const ws = document.getElementById('welcomeScreen');
  if (ws) ws.style.display = 'none';
}

// ── APPEND BUBBLES ──
function appendUserBubble(text) {
  const el = document.createElement('div');
  el.className = 'msg-group';
  el.innerHTML = `<div class="msg-user"><div class="msg-user-bubble">${escapeHtml(text)}</div></div>`;
  document.getElementById('messages').appendChild(el);
  scrollToBottom();
  return el;
}

function appendAiBubble(text, sources, suggestions, needsClarification) {
  let sourcesHTML = '';
  if (!needsClarification && sources && sources.length > 0) {
    sourcesHTML = '<div class="source-pills">';
    sources.forEach(src => {
      sourcesHTML += `<a href="${src.drive_url}" target="_blank" rel="noopener" class="source-pill">
        <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zm4 18H6V4h7v5h5v11z"/></svg>
        ${escapeHtml(src.file_name)}
      </a>`;
    });
    sourcesHTML += '</div>';
  }

  let pillsHTML = '';
  if (suggestions && suggestions.length > 0) {
    const pillClass = needsClarification ? 'ai-clarification-pill' : 'ai-suggestion-pill';
    pillsHTML = '<div class="ai-suggestions">';
    suggestions.forEach(s => {
      const escaped = escapeHtml(s);
      pillsHTML += `<button class="${pillClass}" onclick="sendSuggestionText(this)">${escaped}</button>`;
    });
    pillsHTML += '</div>';
  }

  const el = document.createElement('div');
  el.className = 'msg-group';
  el.innerHTML = `
    <div class="msg-ai">
      <div class="ai-avatar"><span>O</span></div>
      <div class="msg-ai-content">
        <div class="ai-label">OneHope Assistant</div>
        <div class="msg-ai-bubble">${formatText(text)}${sourcesHTML}${pillsHTML}</div>
      </div>
    </div>
  `;
  document.getElementById('messages').appendChild(el);
  scrollToBottom();
  return el;
}

function appendErrorBubble(message) {
  const el = document.createElement('div');
  el.className = 'msg-group';
  el.innerHTML = `
    <div class="msg-ai">
      <div class="ai-avatar"><span>O</span></div>
      <div class="msg-ai-content">
        <div class="ai-label">OneHope Assistant</div>
        <div class="error-bubble">
          <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>
          <span>${escapeHtml(message)}</span>
        </div>
      </div>
    </div>
  `;
  document.getElementById('messages').appendChild(el);
  scrollToBottom();
}

function addTypingIndicator() {
  const el = document.createElement('div');
  el.className = 'typing-indicator';
  el.id = 'typingIndicator';
  el.innerHTML = `
    <div class="ai-avatar"><span>O</span></div>
    <div class="typing-bubble"><span></span><span></span><span></span></div>
  `;
  document.getElementById('messages').appendChild(el);
  scrollToBottom();
}

function removeTypingIndicator() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

// ── SUGGESTIONS ──
function sendSuggestion(btn) {
  const text = btn.querySelector('.s-text');
  if (!text) return;
  document.getElementById('userInput').value = text.textContent.trim();
  sendMessage();
}

function sendSuggestionText(btn) {
  document.getElementById('userInput').value = btn.textContent.trim();
  sendMessage();
}

// ── MAIN SEND ──
async function sendMessage() {
  if (isLoading) return;
  const input = document.getElementById('userInput');
  const sendBtn = document.getElementById('sendBtn');
  const userText = input.value.trim();
  if (!userText) return;

  if (!currentChatId) {
    currentChatId = 'chat_' + Date.now();
    currentChatTitle = userText.substring(0, 40) + (userText.length > 40 ? '…' : '');
    _pendingMessages = [];
    document.getElementById('topbarTitle').textContent = currentChatTitle;
  }

  hideWelcome();
  input.value = '';
  input.style.height = 'auto';
  sendBtn.disabled = true;
  isLoading = true;

  appendUserBubble(userText);
  _pendingMessages.push({ role: 'user', content: userText });
  addTypingIndicator();

  try {
    const response = await fetch(`${BACKEND_URL}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: userText,
        conversation_history: conversationHistory
      })
    });

    const data = await response.json();
    removeTypingIndicator();

    if (!response.ok) {
      appendErrorBubble(data.detail || 'An error occurred on the server.');
    } else {
      const replyText = data.answer || "Sorry, I couldn't generate a response.";
      const sources = data.sources || [];
      const suggestions = data.suggestions || [];
      const needsClarification = data.needs_clarification || false;

      conversationHistory.push({ role: 'user', content: userText });
      conversationHistory.push({ role: 'model', content: replyText });
      if (conversationHistory.length > 20) conversationHistory = conversationHistory.slice(-20);

      appendAiBubble(replyText, sources, suggestions, needsClarification);
      _pendingMessages.push({ role: 'ai', content: replyText, sources });

      saveCurrentChat();
      renderSidebar();
    }
  } catch (err) {
    removeTypingIndicator();
    appendErrorBubble(`Could not reach the backend at ${BACKEND_URL}. Make sure the server is running.`);
  }

  sendBtn.disabled = false;
  isLoading = false;
  input.focus();
}

// ── TEXTAREA AUTO-RESIZE ──
document.getElementById('userInput').addEventListener('input', function () {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

document.getElementById('userInput').addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// ── INIT ──
renderSidebar();

fetch(`${BACKEND_URL}/`)
  .then(r => r.json())
  .then(data => console.log(`✅ Backend connected. Chunks indexed: ${data.chunks_indexed}`))
  .catch(() => console.warn('⚠️ Backend not reachable at', BACKEND_URL));
