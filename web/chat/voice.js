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
import { setStoredConversationId } from './backend.js';

const VOICE_MODE_KEY = 'lifeos:chat:voice_mode';
const DOCK_SETTINGS_KEY = 'lifeos:chat:dock_settings';

// Ported thresholds from whisper-relay static/app.js.
const FAST_PLAYBACK_RATE = 2;
const MIN_RECORD_MS = 300;
const SILENCE_PEAK_THRESHOLD = 0.012;
const SILENCE_RMS_THRESHOLD = 0.006;

let mediaRecorder = null;
let recordedChunks = [];
let recording = false;
let recordStartedAt = 0;

let activeTurnId = null;
let activeTurnAbort = null;
let activeAudios = [];
let playbackChain = Promise.resolve();
let isPlaying = false;
let thinkingEl = null;

// Dock toggles (Mute / Auto-continue / 2x fast speech), persisted in
// localStorage. Ported from whisper-relay's dockSettings.
let dockSettings = { mute: false, auto: false, fast: false };

function loadDockSettings() {
  try {
    const raw = window.localStorage.getItem(DOCK_SETTINGS_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      dockSettings = { mute: !!p.mute, auto: !!p.auto, fast: !!p.fast };
    }
  } catch (e) {
    /* storage blocked — keep defaults */
  }
  return dockSettings;
}

function persistDockSettings() {
  try {
    window.localStorage.setItem(DOCK_SETTINGS_KEY, JSON.stringify(dockSettings));
  } catch (e) {
    /* storage blocked */
  }
}

function shouldPlayAudio() { return !dockSettings.mute; }
function getAutoContinue() { return dockSettings.auto; }
function getPlaybackRate() { return dockSettings.fast ? FAST_PLAYBACK_RATE : 1; }

function applyPlaybackRate(audio) {
  audio.playbackRate = getPlaybackRate();
  audio.preservesPitch = true;
}

function syncActivePlaybackRates() {
  for (const a of activeAudios) applyPlaybackRate(a);
}

function getBackendMode() { return config.backend || 'lifeos'; }  // LifeOS|Agent toggle → PR-D

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

function wireDockToggle(checkbox, key, onChange) {
  if (!checkbox) return;
  checkbox.checked = dockSettings[key];
  checkbox.addEventListener('change', () => {
    dockSettings[key] = checkbox.checked;
    persistDockSettings();
    if (onChange) onChange();
  });
}

export function initVoice() {
  config.voiceMode = readVoiceMode();
  applyVoiceMode();

  // Dock toggles (Mute / 2x fast / Auto-continue), persisted in localStorage.
  loadDockSettings();
  wireDockToggle(elements.voiceMute, 'mute', () => { if (dockSettings.mute) stopAllAudio(); });
  wireDockToggle(elements.voiceFast, 'fast', syncActivePlaybackRates);
  wireDockToggle(elements.voiceAuto, 'auto');

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
    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      const mime = mediaRecorder.mimeType || 'audio/webm';
      const blob = new Blob(recordedChunks, { type: mime });
      if (blob.size === 0) return;
      // Skip-silent: if nothing was said, drop the turn (re-listen if Auto on).
      if (await isSilentBlob(blob)) {
        if (getAutoContinue()) maybeAutoContinue();
        return;
      }
      submitTurn({ blob, mime });
    };
    mediaRecorder.start();
  } catch (e) {
    // MediaRecorder unsupported / failed — don't leak the live mic.
    stream.getTracks().forEach(t => t.stop());
    setStatus('error', 'Mic error');
    return;
  }
  recording = true;
  recordStartedAt = Date.now();
  setTalkActive(true);
}

async function stopRecording() {
  if (!recording) return;
  recording = false;
  setTalkActive(false);
  // Don't cut a too-short clip (whisper-relay MIN_RECORD_MS).
  const elapsed = Date.now() - recordStartedAt;
  if (elapsed < MIN_RECORD_MS) {
    await new Promise(r => setTimeout(r, MIN_RECORD_MS - elapsed));
  }
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

// --- skip-silent: detect an empty/quiet recording before sending ---
function pcmLevels(samples) {
  if (!samples || !samples.length) return { peak: 0, rms: 0 };
  let sumSq = 0, peak = 0;
  for (let i = 0; i < samples.length; i += 1) {
    const abs = Math.abs(samples[i]);
    if (abs > peak) peak = abs;
    sumSq += samples[i] * samples[i];
  }
  return { peak, rms: Math.sqrt(sumSq / samples.length) };
}

function isSilentLevels(peak, rms) {
  return peak < SILENCE_PEAK_THRESHOLD && rms < SILENCE_RMS_THRESHOLD;
}

async function isSilentBlob(blob) {
  if (!blob || blob.size === 0) return true;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return false;
  const ctx = new Ctx();
  try {
    const buffer = await ctx.decodeAudioData(await blob.arrayBuffer());
    const { peak, rms } = pcmLevels(buffer.getChannelData(0));
    return isSilentLevels(peak, rms);
  } catch (e) {
    return false;
  } finally {
    ctx.close().catch(() => {});
  }
}

// --- replay: tap a response to replay its audio clips ---
function parseAudioUrls(el) {
  try { return JSON.parse(el.dataset.audioUrls || '[]'); } catch (e) { return []; }
}

function attachReplay(el, audioUrls) {
  if (!el || !audioUrls || !audioUrls.length) return;
  el.classList.add('replayable');
  el.dataset.audioUrls = JSON.stringify(audioUrls);
  el.setAttribute('role', 'button');
  el.setAttribute('tabindex', '0');
  el.title = 'Tap to replay';
  el.addEventListener('click', () => replayMessage(el));
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); replayMessage(el); }
  });
}

async function replayMessage(el) {
  if (!shouldPlayAudio()) return;
  await playUrls(parseAudioUrls(el));
}

async function playUrls(urls) {
  if (!shouldPlayAudio() || !urls.length) return;
  stopAllAudio();
  isPlaying = true;
  try {
    for (const url of urls) {
      await new Promise((resolve) => {
        const audio = new Audio(url);
        applyPlaybackRate(audio);
        activeAudios.push(audio);
        audio.onended = resolve;
        audio.onerror = resolve;  // a failed clip shouldn't stall replay
        audio.play().catch(resolve);
      });
    }
  } finally {
    isPlaying = false;
  }
}

// --- auto-continue: re-enter recording after a turn when enabled ---
async function maybeAutoContinue() {
  if (!getAutoContinue()) return;
  try {
    await startRecording();
  } catch (e) {
    /* mic re-acquire may need a tap on some platforms — stay idle */
  }
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
    if (event.type === 'status_audio') {
      if (event.message) setStatus('loading', event.message);  // spoken status text
      if (shouldPlayAudio() && event.url) {
        isPlaying = true;
        enqueueClip(event.url);
      }
    }
    if (event.type === 'main_audio') {
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
      setStoredConversationId(data.conversation_id);  // per-backend persistence
      loadConversations();
    }
    clearThinking();
    if (data.transcript) addMessage(data.transcript, 'user');
    const playbackUrls = [...(data.status_audio_urls || []), data.audio_url].filter(Boolean);
    if (data.response_text) {
      const el = addMessage(data.response_text, 'assistant');
      attachReplay(el, playbackUrls);  // tap to replay
    }

    await playbackChain;
    isPlaying = false;
    showCancel(false);
    setStatus('', 'Ready');
    await maybeAutoContinue();  // re-record if Auto-continue is on
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
