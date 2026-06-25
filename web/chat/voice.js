// Voice mode for /chat (#361).
//
// Tap-to-talk (tap start, tap stop) — same interaction model as
// whisper-relay/static/app.js (onTalkClick), not hold-to-talk. Turn lifecycle:
// record → multipart POST /api/voice/turn/stream → SSE → done data → playback.

import { state, config, elements, endpoints } from './session.js';
import { addMessage, setStatus } from './thread.js';
import { loadConversations } from './conversations.js';
import { setStoredConversationId } from './backend.js';
import { personaOrchestrates } from './persona.js';
import { startPendingQuestionPolling } from './pending-question.js';

const VOICE_MODE_KEY = 'lifeos:chat:voice_mode';
const DOCK_SETTINGS_KEY = 'lifeos:chat:dock_settings';
const MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4;codecs=mp4a.40.2',
  'audio/mp4',
  'audio/ogg;codecs=opus',
];
const SILENT_WAV =
  'data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQQAAAAAAA==';

const FAST_PLAYBACK_RATE = 2;
const MIN_RECORD_MS = 300;
const SILENCE_PEAK_THRESHOLD = 0.012;
const SILENCE_RMS_THRESHOLD = 0.006;

const platform = (() => {
  const ua = navigator.userAgent;
  const isIOS =
    /iPhone|iPod|iPad/i.test(ua) ||
    (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1);
  return { isIOS };
})();
const useWebAudioRecorder = platform.isIOS;

let mediaRecorder = null;
let selectedMime = '';
let chunks = [];
let pcmChunks = [];
let micStream = null;
let audioCtx = null;
let audioSource = null;
let audioProcessor = null;
let isRecording = false;
let isStarting = false;
let micAcquireFailed = false;
let recordStartedAt = 0;
let voiceBusy = false;

let activeTurnId = null;
let activeTurnAbort = null;
let activeAudios = [];
let playbackChain = Promise.resolve();
let isPlaying = false;
let thinkingEl = null;
let ttsAudio = null;

let dockSettings = { mute: false, auto: false, fast: false };

class EmptyRecordingError extends Error {
  constructor() {
    super('Empty recording');
    this.name = 'EmptyRecordingError';
  }
}

function isEmptyRecordingError(err) {
  return err?.name === 'EmptyRecordingError';
}

function loadDockSettings() {
  try {
    const raw = window.localStorage.getItem(DOCK_SETTINGS_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      dockSettings = { mute: !!p.mute, auto: !!p.auto, fast: !!p.fast };
    }
  } catch (e) {
    /* storage blocked */
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

function useSharedTtsAudio() {
  return platform.isIOS || /Android/i.test(navigator.userAgent);
}

function getTtsAudioElement() {
  if (!ttsAudio) {
    ttsAudio = new Audio();
    ttsAudio.setAttribute('playsinline', '');
    ttsAudio.preload = 'auto';
  }
  return ttsAudio;
}

function unlockTtsAudio() {
  if (!useSharedTtsAudio()) return;
  const audio = getTtsAudioElement();
  audio.volume = 1;
  audio.src = SILENT_WAV;
  audio.playbackRate = 1;
  audio.play().catch(() => {});
}

function getBackendMode() { return config.backend || 'lifeos'; }

function isVoiceMode() {
  return config.voiceMode === true;
}

// An explicit input-mode choice: a URL param (/chat?mode=voice | ?mode=text),
// which also sticks, then the stored preference. Returns true (voice), false
// (text), or null when there's no explicit choice — in which case the caller
// falls back to the server's configured default (LIFEOS_CHAT_DEFAULT_VOICE).
function resolveExplicitVoiceMode() {
  try {
    const mode = new URLSearchParams(window.location.search).get('mode');
    if (mode === 'voice') { storeVoiceMode(true); return true; }
    if (mode === 'text') { storeVoiceMode(false); return false; }
  } catch (e) {
    /* no URLSearchParams / blocked — fall through */
  }
  let stored = null;
  try {
    stored = window.sessionStorage.getItem(VOICE_MODE_KEY);
  } catch (e) {
    /* sessionStorage unavailable */
  }
  if (stored === '1') return true;
  if (stored === '0') return false;
  return null;
}

// The server-configured default mode (LIFEOS_CHAT_DEFAULT_VOICE). Off by default
// so a fresh clone without a voice gateway stays on text.
async function fetchDefaultVoiceMode() {
  try {
    const resp = await fetch(endpoints.chatConfig);
    if (resp.ok) return !!(await resp.json()).default_voice;
  } catch (e) {
    /* config unavailable — keep text */
  }
  return false;
}

function storeVoiceMode(on) {
  try {
    window.sessionStorage.setItem(VOICE_MODE_KEY, on ? '1' : '0');
  } catch (e) {
    /* sessionStorage unavailable */
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

function pickMimeType() {
  if (typeof MediaRecorder === 'undefined') return '';
  for (const mime of MIME_CANDIDATES) {
    if (MediaRecorder.isTypeSupported(mime)) return mime;
  }
  return '';
}

function canRecordAudio() {
  return (
    window.isSecureContext &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof MediaRecorder !== 'undefined' &&
    (useWebAudioRecorder || pickMimeType() !== '')
  );
}

function formatMicError(err) {
  const name = err?.name || '';
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return 'Microphone permission denied. Allow mic access for this site, then reload.';
  }
  if (name === 'NotFoundError' || name === 'OverconstrainedError') {
    return 'No microphone was found on this device.';
  }
  return err?.message || String(err);
}

export function initVoice() {
  const explicit = resolveExplicitVoiceMode();
  config.voiceMode = explicit === true;  // text until the server default resolves
  applyVoiceMode();
  if (explicit === null) {
    // No URL param / stored preference — honor the server default. Async, but
    // local + sub-frame, so any text→voice flip is imperceptible.
    fetchDefaultVoiceMode().then((isVoice) => {
      if (isVoice && resolveExplicitVoiceMode() === null && !config.voiceMode) {
        config.voiceMode = true;
        applyVoiceMode();
      }
    });
  }

  loadDockSettings();
  wireDockToggle(elements.voiceMute, 'mute', () => { if (dockSettings.mute) stopAllAudio(); });
  wireDockToggle(elements.voiceFast, 'fast', syncActivePlaybackRates);
  wireDockToggle(elements.voiceAuto, 'auto');

  const talk = elements.voiceTalkBtn;
  if (talk) {
    talk.addEventListener('click', onTalkClick, false);
    talk.addEventListener('contextmenu', (e) => e.preventDefault());
  }
  if (elements.voiceCancelBtn) {
    elements.voiceCancelBtn.addEventListener('click', () => {
      if (voiceBusy || isPlaying) cancelActiveTurn();
      else stopAllAudio();
    });
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

function setTalkActive(on) {
  // The talk button is a circular shutter; recording state is shown via CSS on
  // the .recording class (we don't overwrite the shutter-core span).
  if (elements.voiceTalkBtn) {
    elements.voiceTalkBtn.classList.toggle('recording', on);
  }
}

function onTalkClick(event) {
  event.preventDefault();
  unlockTtsAudio();
  if (!canRecordAudio()) {
    setStatus('error', 'Mic unavailable (HTTPS required)');
    return;
  }
  if (voiceBusy || state.isLoading) return;
  if (isStarting) return;

  if (isPlaying) {
    stopAllAudio();
    return;
  }

  if (isRecording) {
    stopRecordingAndSend().catch((err) => {
      setStatus('error', 'Error');
      addMessage('⚠️ ' + (err?.message || 'Recording failed'), 'assistant');
      setTalkActive(false);
    });
    return;
  }

  beginRecordingFromTap();
}

async function acquireMicStream() {
  const backoffMs = [0, 200, 400, 800];
  let lastErr;
  for (const delay of backoffMs) {
    if (delay) await new Promise((r) => setTimeout(r, delay));
    try {
      return await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      lastErr = err;
      if (err?.name !== 'NotAllowedError') throw err;
    }
  }
  throw lastErr;
}

function requestMicInGesture() {
  if (!window.isSecureContext) {
    return Promise.reject(new Error('Microphone requires HTTPS.'));
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    return Promise.reject(new Error('Microphone not available.'));
  }
  if (micStream?.getAudioTracks().some((t) => t.readyState === 'live')) {
    return Promise.resolve(micStream);
  }
  return acquireMicStream().then((stream) => {
    micStream = stream;
    return stream;
  });
}

function setupMediaRecorder(stream) {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    try { mediaRecorder.stop(); } catch (_) { /* ignore */ }
  }
  selectedMime = pickMimeType();
  try {
    mediaRecorder = selectedMime
      ? new MediaRecorder(stream, { mimeType: selectedMime })
      : new MediaRecorder(stream);
  } catch (_) {
    mediaRecorder = new MediaRecorder(stream);
    selectedMime = '';
  }
  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) chunks.push(e.data);
  };
}

function beginRecording(stream) {
  chunks = [];
  setupMediaRecorder(stream);
  mediaRecorder.start(250);
  recordStartedAt = Date.now();
  isRecording = true;
  setStatus('', 'Recording…');
  setTalkActive(true);
}

function ensureAudioContext() {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  if (!audioCtx || audioCtx.state === 'closed') audioCtx = new Ctx();
  if (audioCtx.state === 'suspended') audioCtx.resume();
}

function beginWebAudioRecording(stream) {
  ensureAudioContext();
  if (!audioCtx) throw new Error('Recording is not supported in this browser.');
  pcmChunks = [];
  audioSource = audioCtx.createMediaStreamSource(stream);
  audioProcessor = audioCtx.createScriptProcessor(4096, 1, 1);
  audioProcessor.onaudioprocess = (e) => {
    pcmChunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  };
  audioSource.connect(audioProcessor);
  audioProcessor.connect(audioCtx.destination);
  recordStartedAt = Date.now();
  isRecording = true;
  setStatus('', 'Recording…');
  setTalkActive(true);
}

function releaseCapture() {
  try {
    if (audioProcessor) {
      audioProcessor.onaudioprocess = null;
      audioProcessor.disconnect();
    }
    if (audioSource) audioSource.disconnect();
  } catch (_) { /* ignore */ }
  audioProcessor = null;
  audioSource = null;
  if (audioCtx) {
    audioCtx.close().catch(() => {});
    audioCtx = null;
  }
}

function encodeWav(samples, sampleRate) {
  const view = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const writeStr = (off, s) => {
    for (let i = 0; i < s.length; i += 1) view.setUint8(off + i, s.charCodeAt(i));
  };
  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  let off = 44;
  for (let i = 0; i < samples.length; i += 1) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    off += 2;
  }
  return new Blob([view], { type: 'audio/wav' });
}

function stopMediaRecorder() {
  return new Promise((resolve, reject) => {
    if (!mediaRecorder || mediaRecorder.state === 'inactive') {
      reject(new Error('Recorder not active'));
      return;
    }
    const mime = mediaRecorder.mimeType || selectedMime || 'audio/webm';
    mediaRecorder.onstop = () => {
      const blob = new Blob(chunks, { type: mime });
      chunks = [];
      if (blob.size === 0) {
        reject(new EmptyRecordingError());
        return;
      }
      resolve({ blob, mime });
    };
    if (mediaRecorder.state === 'recording') {
      if (typeof mediaRecorder.requestData === 'function') {
        mediaRecorder.requestData();
      }
      mediaRecorder.stop();
    }
  });
}

function stopWebAudioRecording() {
  return new Promise((resolve, reject) => {
    const sampleRate = audioCtx ? audioCtx.sampleRate : 44100;
    let total = 0;
    for (const c of pcmChunks) total += c.length;
    const flat = new Float32Array(total);
    let off = 0;
    for (const c of pcmChunks) {
      flat.set(c, off);
      off += c.length;
    }
    pcmChunks = [];
    releaseCapture();
    if (flat.length === 0) {
      reject(new EmptyRecordingError());
      return;
    }
    const { peak, rms } = pcmLevels(flat);
    if (isSilentLevels(peak, rms)) {
      reject(new EmptyRecordingError());
      return;
    }
    resolve({ blob: encodeWav(flat, sampleRate), mime: 'audio/wav' });
  });
}

function stopRecorder() {
  return useWebAudioRecorder ? stopWebAudioRecording() : stopMediaRecorder();
}

async function handleSkippedEmptyRecording() {
  setTalkActive(false);
  setStatus('', 'Ready');
  if (getAutoContinue()) await maybeAutoContinue();
}

async function beginRecordingFromTap() {
  isStarting = true;
  try {
    if (useWebAudioRecorder) ensureAudioContext();
    const stream = await requestMicInGesture();
    if (useWebAudioRecorder) beginWebAudioRecording(stream);
    else beginRecording(stream);
    micAcquireFailed = false;
  } catch (err) {
    if (err?.name === 'NotAllowedError' && !micAcquireFailed) {
      micAcquireFailed = true;
      setStatus('error', 'Tap to talk again — mic was not ready.');
    } else {
      setStatus('error', 'Mic error');
      addMessage('⚠️ ' + formatMicError(err), 'assistant');
    }
  } finally {
    isStarting = false;
  }
}

async function stopRecordingAndSend() {
  if (isStarting || !isRecording) return;

  const elapsed = Date.now() - recordStartedAt;
  if (elapsed < MIN_RECORD_MS) {
    await new Promise((r) => setTimeout(r, MIN_RECORD_MS - elapsed));
  }

  isRecording = false;
  setTalkActive(false);

  try {
    const { blob, mime } = await stopRecorder();
    if (await isSilentBlob(blob)) {
      await handleSkippedEmptyRecording();
      return;
    }
    await submitTurn({ blob, mime });
  } catch (err) {
    if (isEmptyRecordingError(err)) {
      await handleSkippedEmptyRecording();
      return;
    }
    throw err;
  }
}

function blobFilename(mime) {
  if (!mime) return 'recording.webm';
  if (mime.includes('wav')) return 'recording.wav';
  if (mime.includes('mp4') || mime.includes('aac')) return 'recording.m4a';
  if (mime.includes('ogg')) return 'recording.ogg';
  return 'recording.webm';
}

function playUrlOnElement(audio, url) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      fn(value);
    };
    const cleanup = () => {
      audio.onended = null;
      audio.onerror = null;
      audio.oncanplaythrough = null;
    };
    const start = () => {
      applyPlaybackRate(audio);
      audio.onended = () => { cleanup(); finish(resolve); };
      audio.onerror = () => { cleanup(); finish(reject, new Error('playback failed')); };
      audio.play().catch((err) => { cleanup(); finish(reject, err); });
    };
    cleanup();
    audio.pause();
    audio.src = url;
    audio.load();
    if (audio.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) {
      start();
    } else {
      audio.oncanplaythrough = () => start();
    }
  });
}

function playSingleUrl(url) {
  if (useSharedTtsAudio()) {
    const audio = getTtsAudioElement();
    if (!activeAudios.includes(audio)) activeAudios.push(audio);
    return playUrlOnElement(audio, url);
  }
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
  unlockTtsAudio();
  await playUrls(parseAudioUrls(el));
}

async function playUrls(urls) {
  if (!shouldPlayAudio() || !urls.length) return;
  stopAllAudio();
  isPlaying = true;
  try {
    for (const url of urls) {
      await playSingleUrl(url);
    }
  } finally {
    isPlaying = false;
  }
}

async function maybeAutoContinue() {
  if (!getAutoContinue()) return;
  try {
    await beginRecordingFromTap();
  } catch (e) {
    /* mic re-acquire may need a tap on some platforms */
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
  voiceBusy = true;
  state.isLoading = true;
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
  // Per-turn model pick — forwarded as `model_override`, mirroring how text
  // turns send it on /api/ask/stream (web/chat/ask-stream.js). Omitted for
  // 'auto' so the default turn stays byte-identical; only the lifeos backend
  // honors model picks. whisper-relay relays the field to /api/ask/stream
  // (whisper-relay#24) — until that ships the gateway drops it, degrading
  // gracefully to the default orchestrator.
  if (getBackendMode() === 'lifeos' && config.model && config.model !== 'auto') {
    form.append('model_override', config.model);
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
      // Orchestrating-persona voice turn (#412): the relay's `done` payload
      // doesn't surface the `claude_code` routing the text path keys off, so
      // gate on the selected persona instead — only an orchestrating bot (e.g.
      // doctor) spawns a session that can ask. Poll the linked conversation so
      // a `[CLARIFY]`/`[GOAL]` can be answered here without Telegram.
      if (personaOrchestrates()) {
        startPendingQuestionPolling(data.conversation_id);
      }
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
    voiceBusy = false;
    state.isLoading = false;
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
  voiceBusy = false;
  state.isLoading = false;
  showCancel(false);
  setStatus('', 'Ready');
}
