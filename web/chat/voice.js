// Voice mode for /chat (#361, PR-B).
//
// A Voice|Text toggle swaps the text composer for a hold-to-talk dock. The turn
// lifecycle is a faithful port of whisper-relay's proven static/app.js (record →
// multipart POST to the LifeOS-proxied /api/voice/turn/stream → consume SSE →
// render the transcript + response from the final `done` data → play audio clips
// in order via a promise chain → cancel via AbortController). Conversation /
// persona / backend selection is LifeOS-owned (config); the gateway is pure
// transport.
//
// PR-B ships the core dock (talk + cancel + mode toggle). The full dock parity
// (Mute / 2x / Auto-continue / Replay / Skip-silent) and the LifeOS|Agent
// backend toggle land in PR-C — the seams (shouldPlayAudio / applyPlaybackRate /
// getBackendMode) are stubbed here so PR-C only extends them.

import { state, config, elements, endpoints } from './session.js';
import { addMessage, setStatus } from './thread.js';
import { loadConversations } from './conversations.js';

const VOICE_MODE_KEY = 'lifeos:chat:voice_mode';

let mediaRecorder = null;
let recordedChunks = [];
let recording = false;

let activeTurnId = null;
let activeTurnAbort = null;
let activeAudios = [];
let playbackChain = Promise.resolve();
let isPlaying = false;
let thinkingEl = null;

// --- PR-C seams (stubbed for PR-B) ---
function shouldPlayAudio() { return true; }          // Mute → PR-C
function applyPlaybackRate(_audio) { /* 2x → PR-C */ }
function getBackendMode() { return config.backend || 'lifeos'; }  // LifeOS|Agent toggle → PR-C

function isVoiceMode() {
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
    talk.addEventListener('mousedown', startRecording);
    talk.addEventListener('touchstart', (e) => { e.preventDefault(); startRecording(); }, { passive: false });
    ['mouseup', 'mouseleave'].forEach(ev => talk.addEventListener(ev, stopRecording));
    ['touchend', 'touchcancel'].forEach(ev => talk.addEventListener(ev, (e) => { e.preventDefault(); stopRecording(); }, { passive: false }));
  }
  if (elements.voiceCancelBtn) {
    elements.voiceCancelBtn.addEventListener('click', cancelActiveTurn);
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

// --- recording (hold-to-talk) ---
async function startRecording() {
  if (recording || state.isLoading) return;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    setStatus('error', 'Mic blocked');
    return;
  }
  try {
    recordedChunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size > 0) recordedChunks.push(e.data); };
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      const mime = mediaRecorder.mimeType || 'audio/webm';
      const blob = new Blob(recordedChunks, { type: mime });
      if (blob.size > 0) submitTurn({ blob, mime });
    };
    mediaRecorder.start();
  } catch (e) {
    // MediaRecorder unsupported / failed — don't leak the live mic.
    stream.getTracks().forEach(t => t.stop());
    setStatus('error', 'Mic error');
    return;
  }
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

function blobFilename(mime) {
  if (!mime) return 'recording.webm';
  if (mime.includes('wav')) return 'recording.wav';
  if (mime.includes('mp4') || mime.includes('aac')) return 'recording.m4a';
  if (mime.includes('ogg')) return 'recording.ogg';
  return 'recording.webm';
}

// --- audio playback (sequential promise chain — survives a failed clip) ---
function playSingleUrl(url) {
  return new Promise((resolve, reject) => {
    const audio = new Audio(url);
    applyPlaybackRate(audio);
    activeAudios.push(audio);
    audio.onended = () => resolve();
    audio.onerror = () => reject(new Error('playback failed'));
    audio.play().catch(reject);
  });
}

function enqueueClip(url) {
  if (!shouldPlayAudio()) return playbackChain;
  playbackChain = playbackChain
    .then(() => playSingleUrl(url))
    .catch((err) => {
      if (!isBenignPlaybackError(err)) console.warn('voice playback error:', err);
    });
  return playbackChain;
}

function stopAllAudio() {
  for (const a of activeAudios) {
    a.onended = null;
    a.onerror = null;
    a.pause();
    a.currentTime = 0;
  }
  activeAudios = [];
  isPlaying = false;
}

function isBenignPlaybackError(err) {
  const name = err?.name || '';
  const msg = (err?.message || '').toLowerCase();
  return name === 'NotAllowedError' || name === 'AbortError'
    || msg.includes('not allowed by the user agent') || msg.includes('aborted');
}

// --- thinking placeholder in the thread ---
function showThinking() {
  clearThinking();
  thinkingEl = addMessage('', 'assistant');
  const content = thinkingEl.querySelector('.message-content');
  if (content) content.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
}

function clearThinking() {
  if (thinkingEl) {
    thinkingEl.remove();
    thinkingEl = null;
  }
}

// --- SSE turn stream (ported from whisper-relay consumeTurnStream) ---
function parseSseChunk(buffer, onEvent) {
  const lines = buffer.split('\n');
  const remainder = lines.pop() || '';
  for (const line of lines) {
    if (!line.startsWith('data: ')) continue;
    try {
      onEvent(JSON.parse(line.slice(6)));
    } catch {
      /* ignore malformed chunks */
    }
  }
  return remainder;
}

async function consumeTurnStream(response) {
  playbackChain = Promise.resolve();
  let doneData = null;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const handleEvent = (event) => {
    if (event.type === 'started') {
      activeTurnId = event.turn_id;
      showCancel(true);
    }
    if (event.type === 'cancelled') {
      throw new DOMException('Turn cancelled', 'AbortError');
    }
    if (event.type === 'error') {
      throw new Error(event.message || 'Turn failed');
    }
    if (event.type === 'status_audio' || event.type === 'main_audio') {
      if (shouldPlayAudio() && event.url) {
        isPlaying = true;
        enqueueClip(event.url);
      }
    }
    if (event.type === 'done') {
      doneData = event.data;
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = parseSseChunk(buffer, handleEvent);
  }
  // Flush a final event that wasn't newline-terminated.
  buffer += decoder.decode();
  parseSseChunk(`${buffer}\n`, handleEvent);
  return doneData;
}

// Exported so the headless test harness can drive a turn without a real mic
// (getUserMedia/MediaRecorder don't run headless).
export async function submitTurn({ blob, mime, transcript } = {}) {
  setStatus('loading', getBackendMode() === 'agent' ? 'Agent thinking…' : 'Thinking…');
  showThinking();
  activeTurnId = null;
  activeTurnAbort = new AbortController();

  const form = new FormData();
  if (blob) form.append('audio', blob, blobFilename(mime));
  if (transcript) form.append('transcript', transcript);
  if (state.currentConversationId) form.append('conversation_id', state.currentConversationId);
  form.append('backend', getBackendMode());
  if (getBackendMode() === 'lifeos' && config.personaId) {
    form.append('persona_id', config.personaId);
  }

  try {
    const res = await fetch(`${endpoints.voice}/turn/stream`, {
      method: 'POST', body: form, signal: activeTurnAbort.signal,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      clearThinking();
      throw new Error(data.detail || `Request failed (${res.status})`);
    }

    const data = await consumeTurnStream(res);
    if (!data) {
      clearThinking();
      throw new Error('Turn ended without a response');
    }

    if (data.conversation_id) {
      state.currentConversationId = data.conversation_id;
      loadConversations();
    }
    clearThinking();
    if (data.transcript) addMessage(data.transcript, 'user');
    if (data.response_text) addMessage(data.response_text, 'assistant');

    await playbackChain;
    isPlaying = false;
    showCancel(false);
    setStatus('', 'Ready');
  } catch (err) {
    if (err?.name === 'AbortError') return;  // cancelled — the cancel handler resets UI
    clearThinking();
    showCancel(false);
    setStatus('error', 'Error');
    addMessage('⚠️ ' + (err?.message || 'Voice turn failed'), 'assistant');
  } finally {
    activeTurnId = null;
    activeTurnAbort = null;
  }
}

function showCancel(on) {
  if (elements.voiceCancelBtn) elements.voiceCancelBtn.classList.toggle('visible', on);
}

function cancelActiveTurn() {
  activeTurnAbort?.abort();
  if (activeTurnId) {
    fetch(`${endpoints.voice}/turn/${encodeURIComponent(activeTurnId)}/cancel`, { method: 'POST' }).catch(() => {});
  }
  playbackChain = Promise.resolve();
  stopAllAudio();
  clearThinking();
  activeTurnId = null;
  activeTurnAbort = null;
  showCancel(false);
  setStatus('', 'Ready');
}
