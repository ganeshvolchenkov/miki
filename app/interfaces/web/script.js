const $ = (id) => document.getElementById(id);
const chatLog = $('chat-log');
const chatForm = $('chat-form');
const chatInput = $('chat-input');
const sendBtn = $('send-btn');
const slashMenu = $('slash-menu');
const faceCanvas = $('pixel-face');
const faceCtx = faceCanvas.getContext('2d');

const COMMANDS = [
    { cmd: '/mail', desc: 'Emails that need your attention, and why' },
    { cmd: '/phone', desc: 'Link Miki to your phone (Telegram)' },
    { cmd: '/interview', desc: 'I ask you questions to get to know you' },
    { cmd: '/profile', desc: 'Who I think you are, and what I don\'t know yet' },
    { cmd: '/remember', desc: 'Store something about you on purpose' },
    { cmd: '/memory', desc: 'What I remember (add a word to search)' },
    { cmd: '/forget', desc: 'Delete a memory (text or id)' },
    { cmd: '/brain', desc: 'How connected my memory is (rebuild | learn | sleep | open)' },
    { cmd: '/model', desc: 'Switch the OpenAI model (cheaper models cost less)' },
    { cmd: '/help', desc: 'Show available commands' },
];

let messageCount = 0;
let sentAt = null;

// ---------- helpers ----------
function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Minimal markdown: ```code blocks```, `inline code`, **bold**, bare links.
function renderMarkdown(text) {
    const parts = escapeHtml(text).split(/```(?:[\w-]+)?\n?([\s\S]*?)```/g);
    return parts.map((part, i) => {
        if (i % 2 === 1) return `<pre><code>${part.replace(/\n$/, '')}</code></pre>`;
        return part
            .replace(/`([^`\n]+)`/g, '<code>$1</code>')
            .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
            .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    }).join('');
}

function timeStamp() {
    return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function hideHero() {
    const hero = $('hero');
    if (hero) hero.remove();
}

function scrollDown() {
    chatLog.scrollTop = chatLog.scrollHeight;
}

function countMessage() {
    messageCount += 1;
    $('sb-msgs').textContent = messageCount;
}

// Reveal an element's text progressively (keeps markup intact by only animating text nodes).
// Throttled to ~12 updates/s: every update re-lays-out the bubble and repaints the scrolled panel,
// which is expensive under software rendering.
function typeInto(el) {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let n;
    while ((n = walker.nextNode())) nodes.push({ node: n, text: n.nodeValue });
    const total = nodes.reduce((a, x) => a + x.text.length, 0);
    if (total < 3) return;
    nodes.forEach((x) => { x.node.nodeValue = ''; });
    const duration = Math.min(1400, Math.max(300, total * 8));
    const start = performance.now();
    let lastPaint = 0;
    el.classList.add('typing-active');
    function step(now) {
        const done = now - start >= duration;
        if (done || now - lastPaint >= 80) {
            lastPaint = now;
            let left = done ? total : Math.floor(((now - start) / duration) * total);
            for (const x of nodes) {
                const k = Math.min(left, x.text.length);
                x.node.nodeValue = x.text.slice(0, k);
                left -= k;
            }
            scrollDown();
        }
        if (!done) requestAnimationFrame(step);
        else el.classList.remove('typing-active');
    }
    requestAnimationFrame(step);
}

function buildMessage(role, innerHtml, via) {
    const wrap = document.createElement('div');
    wrap.className = `msg ${role}`;
    wrap.innerHTML =
        `<div class="meta"><b>${role === 'miki' ? 'MIKI' : 'YOU'}</b>${via ? `<span class="via">${via}</span>` : ''}<span>${timeStamp()}</span></div>` +
        `<div class="bubble">${innerHtml}</div>`;
    chatLog.appendChild(wrap);
    scrollDown();
    countMessage();
    return wrap;
}

// ---------- API called from Python ----------
function appendMessage(role, text) {
    hideHero();
    if (role === 'user') {
        buildMessage('user', escapeHtml(text));
    } else {
        const div = document.createElement('div');
        div.className = 'sys' + (/^error/i.test(text) ? ' error' : '');
        div.textContent = text;
        chatLog.appendChild(div);
        scrollDown();
    }
}

function appendMemoryToast(items) {
    const box = document.createElement('div');
    box.className = 'toast';
    box.innerHTML = '<div class="toast-head">◆ REMEMBERED</div>' + items.map((item) => {
        const entities = (item.entities || []).map((e) => `<span class="ent">${escapeHtml(e)}</span>`).join('');
        return `<div class="toast-item"><div class="toast-title">${escapeHtml(item.title)}</div>` +
               `<div class="toast-meta">${escapeHtml(item.category)}${entities ? ' · ' + entities : ''}</div></div>`;
    }).join('');
    chatLog.appendChild(box);
    scrollDown();
}

// A message that arrived from (or was sent to) the phone: shown in the chat, tagged so it's clear where it came from.
function appendPhoneMessage(role, text) {
    hideHero();
    const wrap = buildMessage(role === 'assistant' ? 'miki' : 'user', role === 'assistant' ? renderMarkdown(text) : escapeHtml(text), 'PHONE');
    return wrap;
}

function createAssistantBubble(id) {
    hideHero();
    const wrap = buildMessage('miki', '<span class="typing"><i></i><i></i><i></i></span>');
    wrap.id = 'bubble-' + id;
}

function updateAssistantBubble(id, text) {
    const wrap = document.getElementById('bubble-' + id);
    if (!wrap) return;
    const bubble = wrap.querySelector('.bubble');
    bubble.innerHTML = renderMarkdown(text);
    typeInto(bubble);
    if (sentAt !== null) {
        $('sb-lat').textContent = ((performance.now() - sentAt) / 1000).toFixed(1) + 's';
        sentAt = null;
    }
}

function removeTyping() {
    document.querySelectorAll('.msg.miki').forEach((m) => {
        if (m.querySelector('.typing')) m.remove();
    });
}

function setBusy(busy) {
    chatInput.disabled = busy;
    sendBtn.disabled = busy;
    document.body.dataset.state = busy ? 'thinking' : 'idle';
    $('voice-state').textContent = busy ? 'Thinking' : 'Online · Ready';
    scope.sync();
    if (!busy) chatInput.focus();
}

function setVoiceState(state) {
    document.body.dataset.state = state;
    $('voice-state').textContent = state === 'speaking' ? 'Speaking' : state === 'thinking' ? 'Thinking' : 'Online · Ready';
    scope.sync();
}

function setModel(name) {
    $('model-name').textContent = name;
    $('model-inline').textContent = name;
    $('sb-model').textContent = name;
    document.querySelectorAll('.model-btn').forEach((b) => b.classList.toggle('active', b.dataset.id === name));
}

function setPill(name, ok, text) {
    const pill = $('pill-' + name);
    if (!pill) return;
    pill.hidden = false;
    pill.classList.toggle('ok', !!ok);
    pill.classList.toggle('bad', !ok);
    pill.querySelector('.pill-text').textContent = text;
    if (name === 'memory') $('sb-mem').textContent = (text.match(/\d+/) || ['—'])[0];
}

function appendModelPicker(current, models) {
    hideHero();
    const card = document.createElement('div');
    card.className = 'picker';
    card.innerHTML = '<h3>SELECT MODEL</h3><p>Cheaper tiers cost far less per message. Your choice is remembered.</p>';
    models.forEach((m) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'model-btn' + (m.id === current ? ' active' : '');
        btn.dataset.id = m.id;
        btn.innerHTML =
            `<div class="grow"><div class="name">${escapeHtml(m.id)}</div><div class="note-t">${escapeHtml(m.note)}</div></div>` +
            `<span class="tier ${escapeHtml(m.tier)}">${escapeHtml(m.tier)}</span>`;
        btn.addEventListener('click', () => sendText('/model ' + m.id));
        card.appendChild(btn);
    });
    const hint = document.createElement('div');
    hint.className = 'custom-hint';
    hint.textContent = 'Any other model: /model <name>';
    card.appendChild(hint);
    chatLog.appendChild(card);
    scrollDown();
}

// ---------- sending ----------
function sendText(text) {
    if (!text) return;
    if (!text.startsWith('/')) sentAt = performance.now();
    if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.process_message(text);
    } else {
        appendMessage('user', text);
        appendMessage('system', 'Python API not ready yet…');
    }
}

chatForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = chatInput.value.trim();
    if (!text || chatInput.disabled) return;
    chatInput.value = '';
    closeSlashMenu();
    sendText(text);
});

document.querySelectorAll('.suggestion').forEach((b) => b.addEventListener('click', () => sendText(b.dataset.prompt)));
$('model-chip').addEventListener('click', () => sendText('/model'));
document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        chatInput.focus();
    }
});

// ---------- slash command menu ----------
let slashIndex = 0;
let slashMatches = [];

function closeSlashMenu() {
    slashMenu.classList.remove('open');
    slashMatches = [];
}

function refreshSlashMenu() {
    const value = chatInput.value;
    if (!value.startsWith('/') || value.includes(' ')) return closeSlashMenu();
    slashMatches = COMMANDS.filter((c) => c.cmd.startsWith(value.toLowerCase()));
    if (!slashMatches.length) return closeSlashMenu();
    slashIndex = Math.min(slashIndex, slashMatches.length - 1);
    slashMenu.innerHTML = slashMatches
        .map((c, i) => `<div class="slash-item${i === slashIndex ? ' sel' : ''}" data-cmd="${c.cmd}"><b>${c.cmd}</b><span>${escapeHtml(c.desc)}</span></div>`)
        .join('');
    slashMenu.classList.add('open');
}

function pickSlash(cmd) {
    chatInput.value = cmd + ' ';
    closeSlashMenu();
    chatInput.focus();
}

slashMenu.addEventListener('mousedown', (e) => {
    const item = e.target.closest('.slash-item');
    if (item) {
        e.preventDefault();
        pickSlash(item.dataset.cmd);
    }
});

chatInput.addEventListener('input', () => { slashIndex = 0; refreshSlashMenu(); });
chatInput.addEventListener('keydown', (e) => {
    if (!slashMenu.classList.contains('open')) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        slashIndex = (slashIndex + (e.key === 'ArrowDown' ? 1 : -1) + slashMatches.length) % slashMatches.length;
        refreshSlashMenu();
    } else if (e.key === 'Tab' || (e.key === 'Enter' && chatInput.value.toLowerCase() !== slashMatches[slashIndex].cmd)) {
        e.preventDefault();
        pickSlash(slashMatches[slashIndex].cmd);
    } else if (e.key === 'Escape') {
        closeSlashMenu();
    }
});
chatInput.addEventListener('blur', () => setTimeout(closeSlashMenu, 120));

// ---------- mail: click a row to open it in Gmail, x to dismiss ----------
function setMailCount(n) {
    const badge = $('mail-count');
    badge.textContent = n ? String(n) : '';
    badge.style.display = n ? 'inline-block' : 'none';
}

$('mail-content').addEventListener('click', (e) => {
    const row = e.target.closest('.mail-row');
    if (!row || !(window.pywebview && window.pywebview.api)) return;
    if (e.target.closest('.mx')) {
        window.pywebview.api.dismiss_mail(row.dataset.id);
        row.remove();
        return;
    }
    window.pywebview.api.open_mail(row.dataset.thread);
});

// ---------- brain graph: a still picture, redrawn only when memory changes ----------
const brainCanvas = $('brain-canvas');
const brainCtx = brainCanvas.getContext('2d');
const brainTip = $('brain-tip');
let brainNodes = [];

function setBrainGraph(graph, stats) {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = brainCanvas.clientWidth || 254, h = brainCanvas.clientHeight || 260;
    brainCanvas.width = Math.round(w * dpr);
    brainCanvas.height = Math.round(h * dpr);
    brainCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    brainCtx.clearRect(0, 0, w, h);

    const byId = {};
    brainNodes = graph.nodes.map((n) => {
        const node = { ...n, px: n.x * w, py: n.y * h };
        byId[n.id] = node;
        return node;
    });

    const linkStyle = { hub: 'rgba(236,232,223,0.22)', category: 'rgba(236,232,223,0.12)', entity: 'rgba(245,184,61,0.22)', related: 'rgba(255,91,46,0.5)' };
    brainCtx.lineWidth = 1;
    for (const link of graph.links) {
        const a = byId[link.source], b = byId[link.target];
        if (!a || !b) continue;
        brainCtx.strokeStyle = linkStyle[link.kind] || 'rgba(236,232,223,0.12)';
        brainCtx.beginPath(); brainCtx.moveTo(a.px, a.py); brainCtx.lineTo(b.px, b.py); brainCtx.stroke();
    }
    for (const n of brainNodes) {
        brainCtx.beginPath();
        if (n.kind === 'me') { brainCtx.fillStyle = '#ff5b2e'; brainCtx.arc(n.px, n.py, 7, 0, 6.2832); brainCtx.fill(); }
        else if (n.kind === 'hub') { brainCtx.strokeStyle = '#ece8df'; brainCtx.lineWidth = 1.2; brainCtx.fillStyle = '#0b0b0a'; brainCtx.arc(n.px, n.py, 4.5, 0, 6.2832); brainCtx.fill(); brainCtx.stroke(); }
        else if (n.kind === 'memory') { brainCtx.fillStyle = '#ff5b2e'; brainCtx.arc(n.px, n.py, 3, 0, 6.2832); brainCtx.fill(); }
        else { brainCtx.strokeStyle = '#f5b83d'; brainCtx.lineWidth = 1.2; brainCtx.arc(n.px, n.py, 3, 0, 6.2832); brainCtx.stroke(); }
    }
    brainCtx.font = '9px "JetBrains Mono", monospace';
    brainCtx.fillStyle = 'rgba(236,232,223,0.6)';
    brainCtx.textAlign = 'center';
    for (const n of brainNodes) {
        if (n.kind === 'hub' || n.kind === 'me') brainCtx.fillText(n.label, n.px, n.py + (n.kind === 'me' ? 18 : 15));
    }
    $('brain-stats').textContent = stats.memories
        ? `${stats.memories} memories · ${stats.entities} entities · ${stats.memory_links} links`
        : 'Nothing remembered yet. Tell me about yourself.';
}

brainCanvas.addEventListener('mousemove', (e) => {
    const rect = brainCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    let best = null, bestDist = 12;
    for (const n of brainNodes) {
        const d = Math.hypot(n.px - x, n.py - y);
        if (d < bestDist) { best = n; bestDist = d; }
    }
    if (!best) { brainTip.style.display = 'none'; return; }
    brainTip.textContent = best.label;
    brainTip.style.display = 'block';
    brainTip.style.left = Math.min(best.px + 8, rect.width - 120) + 'px';
    brainTip.style.top = Math.max(0, best.py - 22) + 'px';
});
brainCanvas.addEventListener('mouseleave', () => { brainTip.style.display = 'none'; });

// ---------- pixel face (Python sends frames; the gentle bob is animated here) ----------
let faceGrid = null;
let faceAmp = 0.06;
let faceSpeed = 2.0;

function renderFace(grid, amplitude, speed, eyeColor) {
    faceGrid = grid;
    faceAmp = amplitude;
    faceSpeed = speed;
    faceCanvas.parentElement.style.setProperty('--glow', eyeColor);
}

let faceDrawn = { grid: null, bob: null };

function drawFace() {
    if (!faceGrid) return;
    // Slow, smooth bob (3-6 s period). Snapped to whole pixels so we only repaint when it visibly changes.
    const bob = Math.round(Math.sin((performance.now() / 1000) * faceSpeed * 0.9) * faceAmp * 60);
    if (faceDrawn.grid === faceGrid && faceDrawn.bob === bob) return;
    faceDrawn = { grid: faceGrid, bob };
    faceCtx.clearRect(0, 0, faceCanvas.width, faceCanvas.height);
    const px = faceCanvas.width / 16;
    for (let y = 0; y < faceGrid.length; y++) {
        for (let x = 0; x < faceGrid[y].length; x++) {
            const color = faceGrid[y][x];
            if (color !== 'transparent') {
                faceCtx.fillStyle = color;
                faceCtx.fillRect(x * px, y * px + bob, px + 0.5, px + 0.5);
            }
        }
    }
}
// ~12 fps is plenty for a slow bob; timers are throttled by the browser when the window is hidden.
setInterval(drawFace, 80);

// ---------- watch-bezel ticks around the face ----------
(function buildBezel() {
    const g = $('bezel');
    let out = '';
    for (let i = 0; i < 72; i++) {
        const major = i % 6 === 0;
        const a = (i / 72) * Math.PI * 2;
        const r1 = 86, r2 = major ? 78 : 82;
        out += `<line x1="${90 + Math.sin(a) * r1}" y1="${90 - Math.cos(a) * r1}" x2="${90 + Math.sin(a) * r2}" y2="${90 - Math.cos(a) * r2}" ` +
               `stroke="${major ? 'rgba(236,232,223,0.55)' : 'rgba(236,232,223,0.18)'}" stroke-width="${major ? 1.4 : 1}"/>`;
    }
    g.innerHTML = out;
})();

// ---------- oscilloscope line: still when idle, animated (~12 fps) only while thinking / speaking ----------
const scope = (function () {
    const canvas = $('scope');
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height, mid = H / 2;
    let amp = 0.1, running = false, raf = 0, last = 0;

    function draw(t) {
        const thinking = document.body.dataset.state === 'thinking';
        ctx.clearRect(0, 0, W, H);
        ctx.strokeStyle = 'rgba(236,232,223,0.10)';
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(W, mid); ctx.stroke();
        ctx.strokeStyle = thinking ? '#f5b83d' : '#ff5b2e';
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        for (let x = 0; x <= W; x += 6) {
            const env = Math.sin((x / W) * Math.PI);
            const y = mid + env * amp * (H * 0.42) * (
                Math.sin(x * 0.045 + t * 1.6) + 0.5 * Math.sin(x * 0.11 - t * 2.3) + 0.25 * Math.sin(x * 0.21 + t * 0.9)
            ) / 1.75;
            x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        }
        ctx.stroke();
    }

    function frame(now) {
        if (!running) return;
        if (now - last >= 80) {
            last = now;
            const target = document.body.dataset.state === 'thinking' ? 0.5 : 0.75;
            amp += (target - amp) * 0.12;
            draw(now / 1000);
        }
        raf = requestAnimationFrame(frame);
    }

    return {
        draw,
        sync() {
            const active = document.body.dataset.state !== 'idle';
            if (active && !running) {
                running = true;
                raf = requestAnimationFrame(frame);
            } else if (!active && running) {
                running = false;
                cancelAnimationFrame(raf);
                amp = 0.1;
                draw(0);
            }
        },
    };
})();
scope.draw(0);

// ---------- clock, greeting, day progress, uptime ----------
const startedAt = Date.now();

function tickClock() {
    const now = new Date();
    $('clock').textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    $('date-line').textContent = now.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' }).toUpperCase();
    const h = now.getHours();
    const part = h < 5 ? 'late' : h < 12 ? 'morning' : h < 18 ? 'afternoon' : 'evening';
    $('greeting').innerHTML = part === 'late' ? 'Still <em>up</em>?' : `Good <em>${part}</em>.`;
    const pct = ((h * 60 + now.getMinutes()) / 1440) * 100;
    $('day-fill').style.width = pct + '%';
    $('day-pct').textContent = Math.round(pct) + '%';
}
tickClock();
setInterval(tickClock, 30000);

function tickUptime() {
    const minutes = Math.floor((Date.now() - startedAt) / 60000);
    $('sb-up').textContent = minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
}
setInterval(tickUptime, 30000);

// ---------- cursor spotlight on the dot grid (a small element; no full-screen repaint) ----------
const spot = $('spot');
let pendingMove = null;
window.addEventListener('mousemove', (e) => {
    if (pendingMove) { pendingMove = e; return; }
    pendingMove = e;
    requestAnimationFrame(() => {
        const x = pendingMove.clientX, y = pendingMove.clientY;
        pendingMove = null;
        spot.style.transform = `translate3d(${x}px, ${y}px, 0)`;
        // Keep the lit dots aligned with the static 28px grid underneath.
        spot.style.setProperty('--bx', ((28 - ((x - 180) % 28)) % 28) + 'px');
        spot.style.setProperty('--by', ((28 - ((y - 180) % 28)) % 28) + 'px');
        spot.classList.add('on');
    });
});
document.addEventListener('mouseleave', () => spot.classList.remove('on'));

chatInput.focus();
