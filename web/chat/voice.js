// Voice mode for /chat (#361, PR-B).
//
// A Voice|Text toggle swaps the text composer for a hold-to-talk dock. In voice
// mode we record a turn with MediaRecorder, POST it as multipart to the
// LifeOS-proxied whisper-relay endpoint (/api/voice/turn/stream), and consume
// the SSE turn events — rendering the transcript + response in the shared thread
// and playing the returned audio clips in order. Conversation/persona/backend
// selection is owned by LifeOS (config); the gateway is pure transport.
//
// PR-B ships the core dock (talk + cancel + mode toggle). The full dock parity
// (Mute / 2x / Auto-continue / Replay / Skip-silent / status-audio polish) and
// the LifeOS|Agent backend toggle land in PR-C.

import { state, config, elements, endpoints } from './session.js';
import { addMessage, setStatus } from './thread.js';
import { loadConversations } from './conversations.js';

const VOICE_MODE_KEY = 'lifeos:chat:voice_mode';

let mediaRecorder = null;
let recordedChunks = [];
let recording = false;
let currentTurnId = null;

// Sequential audio playback queue (status clips then the main response).
let audioEl = null;
let playQueue = [];
let playing = false;

export function isVoiceMode() {
  return config.voiceMode === true;
}

function readVoiceMode() {
  try {
    return window.sessionStorage.getItem(VOICE_MODE_KEY) === '1';
  } catch (e) {
    return false;
  }
}

function storeVoiceMode(on) {
  try {
    window.sessionStorage.setItem(VOICE_MODE_KEY, on ? '1' : '0');
  } catch (e) {
    // sessionStorage unavailable — selection just won't persist
  }
}

export function initVoice() {
  config.voiceMode = readVoiceMode();
  applyVoiceMode();

  const talk = elements.voiceTalkBtn;
  if (talk) {
    // Hold-to-talk: press to record, release to send.
    talk.addEventListener('mousedown', startRecording);
    talk.addEventListener('touchstart', (e) => { e.preventDefault(); startRecording(); }, { passive: false });
    ['mouseup', 'mouseleave'].forEach(ev => talk.addEventListener(ev, stopRecording));
    ['touchend', 'touchcancel'].forEach(ev => talk.addEventListener(ev, (e) => { e.preventDefault(); stopRecording(); }, { passive: false }));
  }
  if (elements.voiceCancelBtn) {
    elements.voiceCancelBtn.addEventListener('click', cancelTurn);
  }
}

export function toggleVoiceMode() {
  config.voiceMode = !isVoiceMode();
  storeVoiceMode(config.voiceMode);
  applyVoiceMode();
}

function applyVoiceMode() {
  document.body.classList.toggle('voice-mode', isVoiceMode());
}

async function startRecording() {
  if (recording || state.isLoading) return;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    setStatus('error', 'Mic blocked');
    return;
  }
  recordedChunks = [];
  mediaRecorder = new MediaRecorder(stream);
  mediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size > 0) recordedChunks.push(e.data); };
  mediaRecorder.onstop = () => {
    stream.getTracks().forEach(t => t.stop());
    const blob = new Blob(recordedChunks, { type: mediaRecorder.mimeType || 'audio/webm' });
    if (blob.size > 0) submitTurn(blob);
  };
  mediaRecorder.start();
  recording = true;
  setTalkActive(true);
}

function stopRecording() {
  if (!recording) return;
  recording = false;
  setTalkActive(false);
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop();
  }
}

function setTalkActive(on) {
  if (elements.voiceTalkBtn) elements.voiceTalkBtn.classList.toggle('recording', on);
}

// Exported so the headless test harness can drive a turn without a real mic.
export async function submitTurn(blob) {
  state.isLoading = true;
  setStatus('loading', 'Listening…');

  const form = new FormData();
  form.append('audio', blob, 'turn.webm');
  form.append('backend', config.backend || 'lifeos');
  if (config.personaId) form.append('persona_id', config.personaId);
  if (state.currentConversationId) form.append('conversation_id', state.currentConversationId);

  try {
    const resp = await fetch(`${endpoints.voice}/turn/stream`, { method: 'POST', body: form });
    if (!resp.ok) throw new Error('voice turn failed');
    await consumeTurnStream(resp);
  } catch (e) {
    console.error('Voice turn error:', e);
    setStatus('error', 'Error');
    addMessage('⚠️ Voice turn failed. Please try again.', 'assistant');
    showCancel(false);
  }

  state.isLoading = false;
  if (elements.statusText && elements.statusText.textContent !== 'Error') {
    setStatus('', 'Ready');
  }
}

async function consumeTurnStream(resp) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();  // keep any partial line for the next chunk
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      try {
        handleVoiceEvent(JSON.parse(line.slice(6)));
      } catch (e) {
        // skip malformed JSON
      }
    }
  }
}

function handleVoiceEvent(data) {
  switch (data.type) {
    case 'started':
      currentTurnId = data.turn_id;
      showCancel(true);
      break;
    case 'transcript':
      if (data.text) addMessage(data.text, 'user');
      break;
    case 'response':
      if (data.text) addMessage(data.text, 'assistant');
      break;
    case 'status_audio':
    case 'main_audio':
      if (data.url) enqueueAudio(data.url);
      break;
    case 'done': {
      const d = data.data || {};
      if (d.conversation_id) state.currentConversationId = d.conversation_id;
      showCancel(false);
      currentTurnId = null;
      loadConversations();
      break;
    }
    case 'error':
      setStatus('error', 'Error');
      addMessage('⚠️ ' + (data.message || 'Voice error'), 'assistant');
      showCancel(false);
      currentTurnId = null;
      break;
    case 'cancelled':
      showCancel(false);
      currentTurnId = null;
      break;
    default:
      break;
  }
}

function enqueueAudio(url) {
  playQueue.push(url);
  if (!playing) playNext();
}

function playNext() {
  if (playQueue.length === 0) { playing = false; return; }
  playing = true;
  const url = playQueue.shift();
  if (!audioEl) audioEl = new Audio();
  audioEl.src = url;
  audioEl.onended = playNext;
  // The hold-to-talk gesture satisfies autoplay; ignore play() rejections.
  audioEl.play().catch(() => playNext());
}

function stopPlayback() {
  playQueue = [];
  playing = false;
  if (audioEl) {
    audioEl.pause();
    audioEl.onended = null;
  }
}

function showCancel(on) {
  if (elements.voiceCancelBtn) elements.voiceCancelBtn.classList.toggle('visible', on);
}

async function cancelTurn() {
  stopPlayback();
  const turnId = currentTurnId;
  showCancel(false);
  currentTurnId = null;
  if (!turnId) return;
  try {
    await fetch(`${endpoints.voice}/turn/${encodeURIComponent(turnId)}/cancel`, { method: 'POST' });
  } catch (e) {
    // best-effort cancel
  }
}
