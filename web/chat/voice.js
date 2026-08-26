// Voice mode for /chat (#361).
//
// Tap-to-talk (tap start, tap stop) — same interaction model as
// whisper-relay/static/app.js (onTalkClick), not hold-to-talk. Turn lifecycle:
// record → multipart POST /api/voice/turn/stream → SSE → done data → playback.

import { state, config, elements, endpoints } from './session.js';
import { addMessage, escapeHtml, setStatus } from './thread.js';
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
// Whether a real clip is currently loading/playing, on *either* the shared
// (iOS/Android) or per-clip (desktop) element -- set/cleared by
// playSingleUrl() itself, tied to that specific call's own promise via
// `.finally()`. This replaced a turn-lifecycle `isPlaying` flag that
// tracked "is a turn nominally still running" instead of "is audio actually
// audible right now": a turn that threw after its audio event but before
// playback settled left that flag stuck, and defensively resetting it in
// every exit path (see git history, #608) opened a *different* hole --
// audio already handed to playbackChain keeps playing after the turn's
// promise settles, so a reset tied to the turn's lifecycle goes stale in
// exactly the window a tap most needs it (onTalkClick's stop-vs-record
// branch, the cancel button). Tying the flag to the clip's own settlement
// instead makes both callers correct regardless of what the enclosing
// turn's control flow does.
let clipInFlight = false;
let thinkingEl = null;
let ttsAudio = null;

// Dock defaults for a first-time visitor (no stored settings yet). 2x playback,
// auto-continue, and wake-word listening are on so voice mode is conversational
// out of the box: speak, hear the reply at 2x, and be heard again without
// touching anything. Mute stays off for the same reason. A stored choice always
// wins over these — loadDockSettings() overwrites the whole object.
let dockSettings = { mute: false, auto: true, fast: true, listen: true };

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
      dockSettings = { mute: !!p.mute, auto: !!p.auto, fast: !!p.fast, listen: !!p.listen };
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
function isListeningEnabled() { return !!dockSettings.listen; }

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
  // A real clip may already be loading or playing on this shared element.
  // Reassigning `.src` here would abandon whatever `playUrlOnElement()` call
  // is in flight for it: that call's `oncanplaythrough` handler is still
  // bound to the old clip's `resolve`, but with the resource swapped out
  // from under it, it fires against this silent one instead once *it*
  // becomes ready -- silently completing the turn without the real clip
  // ever having played (#608).
  if (clipInFlight) return;
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

// GET /chat/config, once per page load. Both the default-mode resolution and the
// insecure-context escape hatch read this, and either may run first, so the
// promise is cached rather than re-fetched. Always resolves — an unreachable
// config degrades to {} (text mode, no HTTPS link).
let chatConfigPromise = null;

function fetchChatConfig() {
  if (!chatConfigPromise) {
    chatConfigPromise = fetch(endpoints.chatConfig)
      .then((resp) => (resp.ok ? resp.json() : {}))
      .catch(() => ({}));
  }
  return chatConfigPromise;
}

// The server-configured default mode (LIFEOS_CHAT_DEFAULT_VOICE). Off by default
// so a fresh clone without a voice gateway stays on text.
async function fetchDefaultVoiceMode() {
  return !!(await fetchChatConfig()).default_voice;
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

// Which recording precondition failed, or '' when the mic is usable. These used
// to collapse into one boolean reported as "HTTPS required", which sent users
// chasing a TLS problem for three causes that have nothing to do with TLS
// (#516). Order matters: an insecure context also hides getUserMedia, so it must
// be checked first to be named as the real cause.
const MIC_BLOCK_MESSAGES = {
  insecure_context: 'Mic blocked — this page is not on HTTPS',
  no_getusermedia: 'Mic unavailable — this browser exposes no microphone API',
  no_mediarecorder: 'Mic unavailable — this browser has no MediaRecorder',
  no_mime: 'Mic unavailable — no supported audio format in this browser',
};

function micBlockReason() {
  if (!window.isSecureContext) return 'insecure_context';
  if (!navigator.mediaDevices?.getUserMedia) return 'no_getusermedia';
  if (typeof MediaRecorder === 'undefined') return 'no_mediarecorder';
  if (!useWebAudioRecorder && pickMimeType() === '') return 'no_mime';
  return '';
}

// The reason already written to the thread, so repeated taps on a blocked talk
// button don't stack identical bubbles. Keyed by reason, not a plain boolean, so
// a *different* cause still gets reported.
let reportedMicBlock = '';

// Report the specific blocked reason in the thread. For an insecure context the
// fix is reachable — the same app is fronted over HTTPS at `secure_url`
// (TAILNET_HTTPS_URL) — so offer a tappable link to this same page there. The
// user taps it; we never auto-redirect. With no configured secure_url (a fresh
// clone, no Tailscale) the message stands alone.
async function reportMicBlocked(reason) {
  const message = MIC_BLOCK_MESSAGES[reason];
  setStatus('error', message);  // every tap gets feedback…
  if (reportedMicBlock === reason) return;  // …but the thread bubble lands once
  reportedMicBlock = reason;  // set before awaiting, so a fast second tap loses
  const secureUrl = reason === 'insecure_context'
    ? ((await fetchChatConfig()).secure_url || '').replace(/\/+$/, '')
    : '';
  if (!secureUrl) {
    addMessage('⚠️ ' + message, 'assistant');
    return;
  }
  const target = secureUrl + window.location.pathname + window.location.search;
  const el = addMessage('', 'assistant');
  const content = el.querySelector('.message-content');
  if (content) {
    content.innerHTML = `⚠️ ${escapeHtml(message)}. `
      + `<a class="source-link" href="${escapeHtml(target)}">🔒 Open over HTTPS</a>`;
  }
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
  // Loaded before the first applyVoiceMode() call below (#710): that call
  // syncs the Listening mic hold to dockSettings.listen, so the setting has
  // to be in memory before voice mode is first applied, not after.
  loadDockSettings();
  loadEndpointingConfig();  // #718 -- fire-and-forget; defaults apply until it resolves

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

  wireDockToggle(elements.voiceMute, 'mute', () => { if (dockSettings.mute) stopAllAudio(); });
  wireDockToggle(elements.voiceFast, 'fast', syncActivePlaybackRates);
  wireDockToggle(elements.voiceAuto, 'auto');
  wireDockToggle(elements.voiceListen, 'listen', onListenToggleChange);

  const talk = elements.voiceTalkBtn;
  if (talk) {
    talk.addEventListener('click', onTalkClick, false);
    talk.addEventListener('contextmenu', (e) => e.preventDefault());
  }
  if (elements.voiceCancelBtn) {
    elements.voiceCancelBtn.addEventListener('click', () => {
      if (voiceBusy || clipInFlight) cancelActiveTurn();
      else stopAllAudio();
    });
  }

  // Text|Voice mode pill (#684) — replaces the old mic/keyboard icon toggle;
  // mirrors backend.js's explicit-set pattern (each button picks its own
  // mode) rather than a single toggle button.
  if (elements.modeTextBtn) elements.modeTextBtn.addEventListener('click', () => setVoiceMode(false));
  if (elements.modeVoiceBtn) elements.modeVoiceBtn.addEventListener('click', () => setVoiceMode(true));
}

export function setVoiceMode(on) {
  if (on === isVoiceMode()) return;
  config.voiceMode = on;
  storeVoiceMode(on);
  applyVoiceMode();
}

function applyVoiceMode() {
  document.body.classList.toggle('voice-mode', isVoiceMode());
  if (elements.modeTextBtn) elements.modeTextBtn.classList.toggle('active', !isVoiceMode());
  if (elements.modeVoiceBtn) elements.modeVoiceBtn.classList.toggle('active', isVoiceMode());
  // Listening (#710) is only meaningful in voice mode -- leaving voice mode
  // releases its mic hold entirely; entering it (with the toggle already on)
  // re-acquires it. startListening()/stopListening() are both idempotent.
  if (isVoiceMode() && isListeningEnabled()) startListening();
  else stopListening();
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
  const blocked = micBlockReason();
  if (blocked) {
    reportMicBlocked(blocked);
    return;
  }
  if (voiceBusy || state.isLoading) return;
  if (isStarting) return;

  if (clipInFlight) {
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
  // Same reasons, same wording as the talk-button guard — this path is also
  // reached without a tap (auto-continue), so it can't assume onTalkClick ran.
  const blocked = micBlockReason();
  if (blocked) {
    return Promise.reject(new Error(MIC_BLOCK_MESSAGES[blocked]));
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
  maybeStartEndpointing(stream);  // #718 -- no-op unless Auto + voice mode
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
  maybeStartEndpointing(stream);  // #718 -- no-op unless Auto + voice mode
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
  // No auto-continue here (#721). stopRecordingAndSend() -- this function's
  // only caller -- is itself only reached from onTalkClick's stop branch, so
  // every empty/silent recording this handles is a manual tap-to-stop, not a
  // completed turn. Auto-continue has to key off "a turn was submitted and
  // its reply finished playing" (submitTurn()'s own maybeAutoContinue() call
  // below, after `await playbackChain`) -- re-arming here as well used to
  // treat a deliberate stop (typically a quick tap that catches too little
  // or no audio) as if a reply had just played, instantly restarting
  // recording with no way to stop without leaving Auto mode entirely.
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
  // Set (and tear down endpointing) synchronously, before the MIN_RECORD_MS
  // await below, so a concurrent second call -- #718's hard cap and a
  // candidate's "complete" verdict can each reach this function -- sees
  // `!isRecording` and returns immediately instead of double-stopping.
  isRecording = false;
  stopEndpointing();

  const elapsed = Date.now() - recordStartedAt;
  if (elapsed < MIN_RECORD_MS) {
    await new Promise((r) => setTimeout(r, MIN_RECORD_MS - elapsed));
  }

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
      audio.__abortPlayback = null;
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
    // Lets stopAllAudio() settle this promise with a benign rejection
    // instead of orphaning it (#617) -- nulling onended/onerror below stops
    // them from ever firing on their own once this clip is interrupted.
    audio.__abortPlayback = () => {
      cleanup();
      finish(reject, new DOMException('Playback stopped', 'AbortError'));
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
  // Set *before* touching the element: playUrlOnElement()'s Promise executor
  // runs synchronously (assigning `.src` immediately, before this function
  // gets anything back), so a flag set only after that call returns would
  // still leave a window, right at the start of a clip's load, where
  // unlockTtsAudio() could steal the element out from under it (#608).
  // Cleared via `.finally()` below -- tied 1:1 to this call's own promise,
  // regardless of what the enclosing turn's control flow does. Both readers
  // (unlockTtsAudio()'s guard, and onTalkClick's/the cancel button's
  // stop-vs-continue branch) need precisely "is a real clip currently
  // loading or playing right now", not "is a turn nominally still running".
  clipInFlight = true;
  let promise;
  if (useSharedTtsAudio()) {
    const audio = getTtsAudioElement();
    if (!activeAudios.includes(audio)) activeAudios.push(audio);
    promise = playUrlOnElement(audio, url);
  } else {
    promise = new Promise((resolve, reject) => {
      const audio = new Audio(url);
      applyPlaybackRate(audio);
      activeAudios.push(audio);
      audio.onended = () => resolve();
      audio.onerror = () => reject(new Error('playback failed'));
      // Same abort hook as the shared-element path above, for the same
      // reason (#617) -- a Promise only settles once, so this is a no-op
      // if onended/onerror already fired.
      audio.__abortPlayback = () => reject(new DOMException('Playback stopped', 'AbortError'));
      audio.play().catch(reject);
    });
  }
  return promise.finally(() => { clipInFlight = false; });
}

function enqueueClip(url) {
  if (!shouldPlayAudio()) return playbackChain;
  playbackChain = playbackChain
    .then(() => playSingleUrl(url))
    .catch((err) => {
      if (isBenignPlaybackError(err)) return;
      console.warn('voice playback error:', err);
      reportPlaybackFailed();
    });
  return playbackChain;
}

function stopAllAudio() {
  for (const a of activeAudios) {
    // Settle a still-in-flight playSingleUrl()/playUrlOnElement() promise
    // for this element with a benign rejection before its handlers are
    // nulled below -- otherwise that promise is orphaned (never resolves
    // or rejects), which hangs whatever awaits it: enqueueClip()'s
    // playbackChain link and, transitively, submitTurn()'s
    // `await playbackChain` for a live turn interrupted by replay (#617).
    // isBenignPlaybackError() already treats AbortError as expected, so
    // this doesn't surface as a playback-failed bubble.
    a.__abortPlayback?.();
    a.__abortPlayback = null;
    a.onended = null;
    a.onerror = null;
    // A clip still loading (not yet past playUrlOnElement()'s canplaythrough
    // wait) has this armed too. Left uncleared, a later canplaythrough for
    // the same element -- browsers can and do refire it, e.g. after the
    // seek below -- calls its stale start(), which re-attaches onended/
    // onerror and calls .play() again, resuming the very clip this
    // function was just asked to stop.
    a.oncanplaythrough = null;
    a.pause();
    a.currentTime = 0;
  }
  activeAudios = [];
  // Nulling the handlers above (rather than firing them) means the
  // in-flight playSingleUrl() promise for a stopped clip never settles, so
  // its own `.finally()` never clears clipInFlight on its own -- reset it
  // explicitly here so a stopped clip doesn't leave unlockTtsAudio() (or
  // the stop-vs-continue checks above) permanently guarded off.
  clipInFlight = false;
}

function isBenignPlaybackError(err) {
  const name = err?.name || '';
  const msg = (err?.message || '').toLowerCase();
  return name === 'NotAllowedError' || name === 'AbortError'
    || msg.includes('not allowed by the user agent') || msg.includes('aborted');
}

// Reported at most once per turn, so several clips failing in the same turn
// (a status_audio clip and the main_audio clip, say) don't stack duplicate
// bubbles. Reset per turn in consumeTurnStream(). A voice turn's spoken reply
// *is* the output (#608) -- rendering the text alone with no signal that
// speech failed reads as the assistant ignoring the user, so this follows the
// same idiom as reportMicBlocked() rather than only logging to the console.
let reportedPlaybackFailure = false;

function reportPlaybackFailed() {
  if (reportedPlaybackFailure) return;
  reportedPlaybackFailure = true;
  addMessage('⚠️ Couldn’t play the spoken reply', 'assistant');
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
  for (const url of urls) {
    await playSingleUrl(url);
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

// --- Listening: wake-word ("Hermes") detection (#710) ---
//
// A fourth dock toggle, default off, persisted like its siblings (dockSettings
// above). While on and in voice mode, this holds its *own* mic stream --
// deliberately never the `micStream` the talk button acquires and reuses
// across taps (requestMicInGesture() above) -- and runs a lightweight
// energy-based VAD in JS. No third-party wake-word engine, no Web Speech API
// (that ships audio to Google -- a hard no for an all-local project). When a
// speech burst ends, the short clip is POSTed to a bare STT endpoint and the
// transcript is fuzzy-matched against "Hermes". A match calls
// beginRecordingFromTap() -- the same function the talk button itself calls
// -- so a wake trigger is indistinguishable from a tap.
//
// `${endpoints.voice}/transcribe` is forwarded by the existing generic
// `/api/voice/*` reverse proxy (api/routes/voice.py) with no LifeOS-side
// route change. It does NOT exist in whisper-relay as of this writing --
// see docs/guides/voice-setup.md. Until the gateway adds it, every wake
// check 404s (transcribeClip() below treats that as "no match" and returns
// quietly) and Listening never actually triggers; the toggle, mic hold, and
// VAD all still work standalone and are ready the moment the route ships.
const WAKE_VAD_RMS_THRESHOLD = 0.02;  // energy floor to call a frame "speech" -- above SILENCE_RMS_THRESHOLD's ambient-noise cutoff
const WAKE_SILENCE_MS = 600;          // trailing silence that ends a speech burst
const WAKE_MIN_SPEECH_MS = 250;       // shorter bursts are clicks/pops, not a word -- skipped without an STT round-trip
const WAKE_MAX_BURST_MS = 4000;       // safety cap so sustained background noise can't buffer forever
const WAKE_PROCESSOR_BUFFER = 4096;   // same block size beginWebAudioRecording() uses
const WAKE_WORD = 'hermes';
const WAKE_MAX_EDIT_DISTANCE = 1;     // tolerates "Hermès"/"Hermie's"/"hermez"-ish whisper-isms

// --- wake chime: a "heard you" sound on a confirmed wake match (#726) ---
//
// A bundled set of short confirmation sounds lives at
// `web/chat/wake-sounds/*`, described by `web/chat/wake-sounds/manifest.json`
// (`{"sounds": [...]}`) -- see docs/guides/voice-setup.md for the set and
// its attribution. Everything below is defensive against that directory or
// manifest being absent, empty, or malformed regardless -- a from-source
// build missing the assets, a stripped-down install, or any other reason
// the fetch below 404s or the JSON doesn't parse as expected -- in which
// case the manifest resolves empty and playWakeChime() resolves immediately
// with no sound played, i.e. today's behavior with no chime at all.
const WAKE_CHIME_DIR = '/static/chat/wake-sounds/';
const WAKE_CHIME_MANIFEST_URL = `${WAKE_CHIME_DIR}manifest.json`;
const WAKE_CHIME_TIMEOUT_MS = 1500;  // caps a long/stalled clip -- must never hang the wake

// Fetched (at most) once per page load, then cached -- both the resolved
// list and the fact that a fetch was already attempted, so a malformed
// manifest or a network hiccup doesn't retry forever but also doesn't wedge
// future calls behind a promise that already settled empty.
let wakeChimeManifestPromise = null;

function loadWakeChimeManifest() {
  if (!wakeChimeManifestPromise) {
    wakeChimeManifestPromise = fetch(WAKE_CHIME_MANIFEST_URL)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => (Array.isArray(data?.sounds)
        ? data.sounds.filter((s) => typeof s === 'string' && s)
        : []))
      .catch(() => []);  // 404 / network error / malformed JSON -- no chime, never throws
  }
  return wakeChimeManifestPromise;
}

// Desktop path: a fresh, throwaway `<audio>` per chime, same as before #725.
// Desktop's autoplay policy doesn't require a prior gesture-unlocked element
// the way iOS/Android's does (this whole file's shared-element machinery
// exists only to route around that mobile restriction), so there's nothing
// to gain from sharing `ttsAudio` here -- and not sharing means a wake chime
// can never contend with `ttsAudio` for desktop, whose per-clip `new Audio()`
// path (playSingleUrl()'s non-shared branch) has no equivalent contention to
// begin with. Resolves on `ended`, on the safety timeout (a long/stalled
// clip must never hang the wake), or immediately on a synchronous throw.
// Never rejects.
function playWakeChimeStandalone(url) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    const timer = setTimeout(finish, WAKE_CHIME_TIMEOUT_MS);
    const settleAndClear = () => { clearTimeout(timer); finish(); };
    try {
      const audio = new Audio(url);
      audio.addEventListener('ended', settleAndClear);
      audio.addEventListener('error', settleAndClear);
      audio.play().catch(settleAndClear);
    } catch (e) {
      settleAndClear();
    }
  });
}

// Mobile path (#725): the chime used to play via `new Audio()` -- an element
// never unlocked by a user gesture. iOS/Android block that unconditionally,
// and the wake path has no gesture of its own (it fires from the wake-word
// STT callback), so the chime was always silent there. Every other
// non-gesture playback on this platform (status/main TTS audio) already
// solves this by routing through the single shared element `unlockTtsAudio()`
// unlocked from an earlier tap -- this does the same, via the same
// `playUrlOnElement()` helper real clips use, rather than a second
// playback routine.
//
// Guarded against the #608 hazard `unlockTtsAudio()` itself guards against:
// touching `.src` while a real clip is mid-load/playback on this element
// would abandon that clip's `oncanplaythrough` the same way. Callers only
// ever reach this once `baseWakeGuardsOk()` (which requires `!clipInFlight`)
// has already passed in `triggerWakeRecording()` -- but that check happens
// before `loadWakeChimeManifest()`'s promise settles, which is always at
// least one microtask away (even cached, `.then()` never runs synchronously)
// and, on an uncached first load, a real network round-trip. A real clip
// could start in that window (a turn's `status_audio`/`main_audio` event
// firing), so the guard here is load-bearing, not defensive-only.
// If it fires, the chime is simply skipped -- no chime is the existing,
// accepted degraded behavior (see loadWakeChimeManifest()'s own doc comment)
// and is far preferable to stealing a real reply out from under the user.
function playWakeChimeShared(url) {
  if (clipInFlight) return Promise.resolve();
  const audio = getTtsAudioElement();
  if (!activeAudios.includes(audio)) activeAudios.push(audio);
  clipInFlight = true;
  const settle = playUrlOnElement(audio, url).catch(() => {});
  return Promise.race([
    settle,
    new Promise((resolve) => setTimeout(resolve, WAKE_CHIME_TIMEOUT_MS)),
  ]).then(() => {
    // Whichever settled first: if the timeout won while the clip was still
    // loading/playing, abort it explicitly rather than leaving it to finish
    // on its own -- the same mechanism stopAllAudio() uses. A no-op if the
    // clip already finished (playUrlOnElement()'s own finish() already
    // nulled __abortPlayback in that case). Doesn't touch `.src`/playbackRate
    // beyond that: the next real clip's own playUrlOnElement() call always
    // reassigns both before playing, so nothing here can leak into it.
    audio.__abortPlayback?.();
  }).finally(() => { clipInFlight = false; });
}

// Plays one randomly-chosen chime and resolves once it's done. Never
// rejects: a missing/broken sound file, or a guard refusing to touch the
// shared element, degrades to "no chime", not a wake failure.
function playWakeChime() {
  return loadWakeChimeManifest().then((sounds) => {
    if (!sounds.length) return;
    const name = sounds[Math.floor(Math.random() * sounds.length)];
    const url = WAKE_CHIME_DIR + encodeURIComponent(name);
    return useSharedTtsAudio() ? playWakeChimeShared(url) : playWakeChimeStandalone(url);
  }).catch(() => {});  // belt-and-suspenders -- loadWakeChimeManifest() already never throws
}

let listenStream = null;
let listenAudioCtx = null;
let listenSource = null;
let listenProcessor = null;
let listenBuffer = [];       // Float32Array chunks accumulated since the current burst started
let listenSpeechMs = 0;
let listenSilenceMs = 0;
let listenInBurst = false;
let listenChecking = false;  // an STT round-trip for an already-captured burst is in flight

// The shared guards: recording, a turn in flight, or TTS playing
// (clipInFlight -- the self-trigger guard, independent of the mute toggle,
// so the assistant saying "Hermes" can never wake it) all suspend Listening.
// `isStarting` covers the brief async gap while a recording is being
// acquired (a tap, or auto mode's own post-TTS re-record) -- without it, a
// wake burst could start accumulating in the instant between a turn ending
// and auto-continue's beginRecordingFromTap() actually flipping isRecording.
function baseWakeGuardsOk() {
  return isListeningEnabled() && isVoiceMode() && !isRecording && !isStarting
    && !voiceBusy && !state.isLoading && !clipInFlight;
}

// Also excludes "a wake check is already in flight" -- used to gate whether
// new audio frames accumulate into a burst at all, so overlapping wake
// checks never fire. (Not used by the actual trigger below -- see there.)
function canDetectWake() {
  return baseWakeGuardsOk() && !listenChecking;
}

function onListenToggleChange() {
  if (!isVoiceMode()) return;  // held for the next time voice mode is entered
  if (isListeningEnabled()) startListening();
  else stopListening();
}

async function startListening() {
  if (listenStream || !isListeningEnabled() || !isVoiceMode()) return;
  if (micBlockReason()) return;  // same preconditions as the talk button; fail silent here
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    return;  // denied/unavailable -- Listening just doesn't run
  }
  // A concurrent toggle-off (or leaving voice mode) while the await above was
  // pending already released everything this function would set up.
  if (!isListeningEnabled() || !isVoiceMode()) {
    for (const t of stream.getTracks()) t.stop();
    return;
  }
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) {
    for (const t of stream.getTracks()) t.stop();
    return;
  }
  listenStream = stream;
  listenAudioCtx = new Ctx();
  listenSource = listenAudioCtx.createMediaStreamSource(listenStream);
  listenProcessor = listenAudioCtx.createScriptProcessor(WAKE_PROCESSOR_BUFFER, 1, 1);
  listenBuffer = [];
  listenSpeechMs = 0;
  listenSilenceMs = 0;
  listenInBurst = false;
  listenProcessor.onaudioprocess = handleListenFrame;
  listenSource.connect(listenProcessor);
  // A ScriptProcessorNode only fires onaudioprocess while connected into the
  // graph's output; it never writes to that output buffer, so nothing here
  // is actually audible.
  listenProcessor.connect(listenAudioCtx.destination);
}

// Releases the mic entirely -- called on toggle-off and on leaving voice
// mode (applyVoiceMode()). Idempotent: safe to call when nothing is held.
function stopListening() {
  try {
    if (listenProcessor) {
      listenProcessor.onaudioprocess = null;
      listenProcessor.disconnect();
    }
    if (listenSource) listenSource.disconnect();
  } catch (_) { /* ignore */ }
  listenProcessor = null;
  listenSource = null;
  if (listenAudioCtx) {
    listenAudioCtx.close().catch(() => {});
    listenAudioCtx = null;
  }
  if (listenStream) {
    for (const t of listenStream.getTracks()) t.stop();
    listenStream = null;
  }
  listenBuffer = [];
  listenInBurst = false;
  listenSpeechMs = 0;
  listenSilenceMs = 0;
  listenChecking = false;
}

function handleListenFrame(e) {
  if (!canDetectWake()) {
    // Suspended -- drop whatever's accumulated so a burst never stitches
    // together audio from before and after a suspend/resume boundary (e.g.
    // speech right before the talk button was tapped, then more after).
    listenBuffer = [];
    listenInBurst = false;
    listenSpeechMs = 0;
    listenSilenceMs = 0;
    return;
  }
  const samples = new Float32Array(e.inputBuffer.getChannelData(0));
  const frameMs = (samples.length / e.inputBuffer.sampleRate) * 1000;
  const { rms } = pcmLevels(samples);

  if (rms >= WAKE_VAD_RMS_THRESHOLD) {
    listenInBurst = true;
    listenSpeechMs += frameMs;
    listenSilenceMs = 0;
    listenBuffer.push(samples);
  } else if (listenInBurst) {
    listenSilenceMs += frameMs;
    listenBuffer.push(samples);  // a little trailing silence rides along in the clip
    if (listenSilenceMs >= WAKE_SILENCE_MS) {
      finishBurst(e.inputBuffer.sampleRate);
      return;
    }
  } else {
    return;  // ambient silence -- nothing buffered yet
  }

  if (listenSpeechMs + listenSilenceMs >= WAKE_MAX_BURST_MS) finishBurst(e.inputBuffer.sampleRate);
}

function finishBurst(sampleRate) {
  const speechMs = listenSpeechMs;
  const chunks = listenBuffer;
  listenBuffer = [];
  listenInBurst = false;
  listenSpeechMs = 0;
  listenSilenceMs = 0;
  if (speechMs < WAKE_MIN_SPEECH_MS) return;  // too short to be a word

  let total = 0;
  for (const c of chunks) total += c.length;
  const flat = new Float32Array(total);
  let off = 0;
  for (const c of chunks) { flat.set(c, off); off += c.length; }
  checkForWakeWord(flat, sampleRate);
}

// Whisper-isms tolerated: accent marks ("Hermès"), trailing punctuation
// ("Hermes,"), and Whisper occasionally splitting the word around a stray
// space ("her mes"). Normalize by stripping diacritics/punctuation and
// lowercasing, then accept either an individual word or the whole
// (space-collapsed) transcript within edit distance 1 of "hermes" -- covers
// "Hermes" / "Hermès" / "Hermes," / "her mes" / "Hermie's" (-> "hermies",
// distance 1) without accepting unrelated short words.
function normalizeForWakeMatch(text) {
  return (text || '')
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')  // Hermès -> Hermes
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, '')                        // drop punctuation
    .replace(/\s+/g, ' ')
    .trim();
}

function levenshtein(a, b) {
  const m = a.length, n = b.length;
  if (!m) return n;
  if (!n) return m;
  let prev = Array.from({ length: n + 1 }, (_, j) => j);
  for (let i = 1; i <= m; i += 1) {
    const cur = [i];
    for (let j = 1; j <= n; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
    }
    prev = cur;
  }
  return prev[n];
}

function isCloseToWakeWord(token) {
  return !!token && levenshtein(token, WAKE_WORD) <= WAKE_MAX_EDIT_DISTANCE;
}

function matchesWakeWord(transcript) {
  const norm = normalizeForWakeMatch(transcript);
  if (!norm) return false;
  if (norm.split(' ').some(isCloseToWakeWord)) return true;
  return isCloseToWakeWord(norm.replace(/\s+/g, ''));  // "her mes" -> "hermes"
}

async function transcribeClip(blob) {
  const form = new FormData();
  form.append('audio', blob, 'wake.wav');
  const res = await fetch(`${endpoints.voice}/transcribe`, { method: 'POST', body: form });
  if (!res.ok) return '';  // includes 404 -- the relay route doesn't exist yet
  const data = await res.json().catch(() => ({}));
  return (data.transcript || '').toString();
}

// Exported so the headless test harness can drive the post-capture pipeline
// without real audio hardware -- getUserMedia energy analysis doesn't run
// headless, the same reason submitTurn() is exported below. Feeds synthetic
// PCM straight into the same STT-call/match/trigger logic a real captured
// burst reaches via finishBurst() above; nothing here is a parallel/fake
// implementation of that logic.
export async function checkForWakeWord(samples, sampleRate) {
  listenChecking = true;
  try {
    const blob = encodeWav(samples, sampleRate);
    let transcript;
    try {
      transcript = await transcribeClip(blob);
    } catch (e) {
      return false;  // relay unreachable/erroring -- miss this burst, not fatal
    }
    if (!matchesWakeWord(transcript)) return false;
    return await triggerWakeRecording();
  } finally {
    listenChecking = false;
  }
}

// The exact code path a talk-button tap uses to start recording. Re-checks
// the guards immediately before firing (not just at burst-capture time) --
// this is what actually prevents a double-trigger with auto mode's own
// post-TTS re-record: if auto-continue's beginRecordingFromTap() already won
// the race by the time this wake match's STT round-trip resolves,
// isRecording/isStarting are already true and this quietly no-ops.
//
// A wake-confirmation chime (#726) plays here, before recording starts --
// never after, so it's never captured as part of the user's turn. While it
// plays, `listenChecking` is still true (checkForWakeWord() below only
// clears it in its `finally`, which runs after this whole async function
// settles), so handleListenFrame() keeps dropping frames the entire time --
// the chime can't be mistaken for speech or re-trigger detection. Guards are
// re-checked below because playback is async: recording, a manual talk-tap,
// or leaving Listening/voice mode could all happen while the chime plays.
async function triggerWakeRecording() {
  if (!baseWakeGuardsOk()) return false;
  unlockTtsAudio();
  await playWakeChime();
  if (!baseWakeGuardsOk()) return false;  // state may have changed during chime playback
  await beginRecordingFromTap();
  return true;
}

// --- Smart turn endpointing: pause + semantic completeness (#718) ---
//
// Auto-continue (above) already reopens the mic after each reply; this layers
// on *when to stop* a recording that started that way (or from a manual tap
// -- endpointing applies to any recording while Auto is on, not only an
// auto-triggered one). It runs its own small WebAudio graph on the SAME
// stream beginRecordingFromTap() already acquired -- never a second
// getUserMedia call -- computing the same energy-VAD RMS check
// handleListenFrame() above uses for wake detection.
//
// Pipeline: after SILENCE_MS of trailing silence *following speech*, the
// recording-so-far is a *candidate* endpoint, not a final decision --
// POSTed to the same bare-STT route (`/api/voice/transcribe`) Listening's
// wake check already uses (no conversation/turn artifacts either way), then
// run through isTranscriptComplete() below. Complete -> finalize through the
// SAME path a manual stop uses (stopRecordingAndSend(), never a parallel
// submit implementation). Incomplete -> keep recording; speech resuming
// re-arms the candidate timer. Silence that keeps growing past HARD_CAP_MS
// finalizes regardless of any candidate verdict, so an ambiguous or
// unreachable check can never hang the mic open forever.
//
// Guards are re-checked after every await, the same pattern
// triggerWakeRecording() uses after its chime -- a manual stop tap, a cancel,
// or Auto/voice mode changing while a candidate round-trip is in flight must
// never let a stale "complete" verdict resurrect or double-submit a turn the
// user already ended a different way.
const ENDPOINT_DEFAULT_SILENCE_MS = 1600;
const ENDPOINT_DEFAULT_HARD_CAP_MS = 3000;

// Trailing words/phrases that read as "still talking" even after a pause --
// conjunctions/connectives that normally lead into more speech, plus common
// spoken-filler hedges. Checked against the transcript's last word (or last
// two, for "i mean") with trailing punctuation stripped. Deliberately
// conservative: a false "incomplete" verdict just keeps recording a beat
// longer, bounded by the hard cap either way; a false "complete" verdict
// sends a fragment as the actual turn.
const ENDPOINT_TRAILING_FILLER_WORDS = [
  'and', 'but', 'so', 'because', 'or', 'if', 'then', 'um', 'uh', 'like',
];
const ENDPOINT_TRAILING_FILLER_PHRASES = ['i mean'];
const ENDPOINT_TERMINAL_PUNCTUATION_RE = /[.?!]["')\]]*$/;

// Pure heuristic -- exported (window.lifeChatVoice below) so it's testable on
// its own, with no page/recording/network round-trip involved. Terminal
// punctuation (. ? !) at the end -> complete. A trailing conjunction/filler
// word, or no terminal punctuation at all, -> incomplete. Whisper frequently
// omits end-of-utterance punctuation on short clips, so "no punctuation"
// alone is a weak signal -- but a false negative here only costs one more
// candidate check (or, worst case, the hard cap), never a hang, so the
// heuristic leans toward "keep listening" over "guess complete."
export function isTranscriptComplete(transcript) {
  const text = (transcript || '').trim();
  if (!text) return false;
  const endsWithPunctuation = ENDPOINT_TERMINAL_PUNCTUATION_RE.test(text);
  const words = text
    .toLowerCase()
    .replace(/[.?!,;:'"()[\]]+$/g, '')
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  const lastWord = words[words.length - 1] || '';
  const lastTwoWords = words.slice(-2).join(' ');
  const endsWithFiller =
    ENDPOINT_TRAILING_FILLER_WORDS.includes(lastWord)
    || ENDPOINT_TRAILING_FILLER_PHRASES.includes(lastTwoWords);
  if (endsWithFiller) return false;
  return endsWithPunctuation;
}

// LIFEOS_VOICE_ENDPOINT_SILENCE_MS / _HARD_CAP_MS, read from GET
// /api/chat/config (settings.py) the same way LIFEOS_CHAT_DEFAULT_VOICE
// reaches fetchDefaultVoiceMode() above. Both start at the designed defaults
// and only move if/when the (already-cached, shared) config fetch resolves
// with a valid override, so a slow or unreachable config never blocks
// endpointing -- it just runs on the built-in timings meanwhile. There is no
// server-side use of these values; the settings only exist to make this
// client-side timing operator-tunable without an env-var-free config file.
let endpointSilenceMsSetting = ENDPOINT_DEFAULT_SILENCE_MS;
let endpointHardCapMsSetting = ENDPOINT_DEFAULT_HARD_CAP_MS;

function loadEndpointingConfig() {
  fetchChatConfig().then((data) => {
    const silence = Number(data.voice_endpoint_silence_ms);
    const hardCap = Number(data.voice_endpoint_hard_cap_ms);
    if (Number.isFinite(silence) && silence > 0) endpointSilenceMsSetting = silence;
    if (Number.isFinite(hardCap) && hardCap > 0) endpointHardCapMsSetting = hardCap;
  });
}

let endpointCtx = null;
let endpointSource = null;
let endpointProcessor = null;
let endpointSampleRate = 0;
let endpointFrames = [];            // Float32Array frames captured since the current recording started
let endpointHasSpeech = false;      // at least one speech frame seen this recording
let endpointSilenceMs = 0;          // trailing silence since the last speech frame
let endpointCandidateFired = false; // a candidate check already ran for the current silence run
let endpointChecking = false;       // a candidate transcribe/completeness round-trip is in flight
// Bumped every time a NEW recording's endpointing tap is (re)installed
// (maybeStartEndpointing()). `endpointingActive()`'s `isRecording` check
// alone isn't enough to catch a stale candidate: Auto-continue can start a
// brand-new recording (isRecording flips true again) in the gap between a
// cancelled/finalized recording and a still-in-flight candidate check for
// THAT earlier recording resolving. checkEndpointCandidate() below captures
// this token at the start and compares it after every await, so a stale
// verdict can never finalize a DIFFERENT (newer) recording than the one it
// was transcribing.
let endpointRecordingToken = 0;

// Only meaningful while Auto-continue AND voice mode AND an actual recording
// are all true -- Auto off, leaving voice mode, or the recording already
// having ended all mean today's manual-stop-only behavior, unchanged.
function endpointingActive() {
  return getAutoContinue() && isVoiceMode() && isRecording;
}

function resetEndpointState() {
  endpointFrames = [];
  endpointHasSpeech = false;
  endpointSilenceMs = 0;
  endpointCandidateFired = false;
  endpointChecking = false;
}

// A no-op when Auto is off or voice mode isn't active, so a non-auto
// recording never pays for this graph at all.
function maybeStartEndpointing(stream) {
  if (!getAutoContinue() || !isVoiceMode()) return;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  resetEndpointState();
  endpointRecordingToken += 1;
  try {
    endpointCtx = new Ctx();
    endpointSource = endpointCtx.createMediaStreamSource(stream);
    endpointProcessor = endpointCtx.createScriptProcessor(WAKE_PROCESSOR_BUFFER, 1, 1);
    endpointSampleRate = endpointCtx.sampleRate;
    endpointProcessor.onaudioprocess = handleEndpointFrame;
    endpointSource.connect(endpointProcessor);
    endpointProcessor.connect(endpointCtx.destination);
  } catch (e) {
    stopEndpointing();  // don't leave a half-wired graph behind
  }
}

function stopEndpointing() {
  try {
    if (endpointProcessor) {
      endpointProcessor.onaudioprocess = null;
      endpointProcessor.disconnect();
    }
    if (endpointSource) endpointSource.disconnect();
  } catch (_) { /* ignore */ }
  endpointProcessor = null;
  endpointSource = null;
  if (endpointCtx) {
    endpointCtx.close().catch(() => {});
    endpointCtx = null;
  }
  resetEndpointState();
}

function flattenEndpointFrames() {
  let total = 0;
  for (const c of endpointFrames) total += c.length;
  const flat = new Float32Array(total);
  let off = 0;
  for (const c of endpointFrames) { flat.set(c, off); off += c.length; }
  return flat;
}

function handleEndpointFrame(e) {
  if (!endpointingActive()) return;
  const samples = new Float32Array(e.inputBuffer.getChannelData(0));
  const frameMs = (samples.length / e.inputBuffer.sampleRate) * 1000;
  // Always keep the "audio so far" mirror current, even while a candidate
  // check is in flight below -- otherwise speech spoken during that
  // round-trip would be missing from the NEXT candidate's transcript.
  endpointFrames.push(samples);
  if (endpointChecking) return;  // a check already owns the decision right now

  const { rms } = pcmLevels(samples);
  if (rms >= WAKE_VAD_RMS_THRESHOLD) {
    endpointHasSpeech = true;
    endpointSilenceMs = 0;
    endpointCandidateFired = false;  // speech resumed -- re-arm the candidate timer
    return;
  }
  if (!endpointHasSpeech) return;  // ambient silence before any speech -- not timed yet

  endpointSilenceMs += frameMs;

  if (endpointSilenceMs >= endpointHardCapMsSetting) {
    finalizeEndpointing();
    return;
  }
  if (!endpointCandidateFired && endpointSilenceMs >= endpointSilenceMsSetting) {
    endpointCandidateFired = true;
    checkEndpointCandidate(flattenEndpointFrames(), endpointSampleRate);
  }
}

// Finalizes through the SAME path a manual stop uses -- stopRecordingAndSend()
// -- never a parallel submit implementation. Called both by the hard cap
// above and by a candidate check's own "complete" verdict below. Safe to call
// more than once (the hard cap firing right around when a candidate resolves,
// say): stopRecordingAndSend() itself guards on `!isRecording`, so whichever
// call gets there first wins and the other is a no-op. Exported so the
// headless test harness can exercise the hard-cap path directly -- it's the
// exact function real continuous silence crossing HARD_CAP_MS calls, with no
// way to wait out that much real silence through the live audio graph in a
// browser test (see checkEndpointCandidate's doc comment for the same
// reasoning re: candidate checks).
export function finalizeEndpointing() {
  if (!isRecording) return;
  stopRecordingAndSend().catch((err) => {
    setStatus('error', 'Error');
    addMessage('⚠️ ' + (err?.message || 'Recording failed'), 'assistant');
    setTalkActive(false);
  });
}

// The candidate-endpoint pipeline. Exported (samples/sampleRate params, like
// checkForWakeWord above) so the headless test harness can drive it directly
// -- the live onaudioprocess VAD above can't run headless either. This is the
// exact function handleEndpointFrame() itself calls once real silence
// crosses SILENCE_MS; nothing here is a parallel/fake implementation of that
// logic. Returns the completeness verdict (or null when the check never
// reached one -- suspended, superseded, or the relay call failed) so tests
// can assert on it directly.
//
// `token` pins this check to the recording it started transcribing for
// (see endpointRecordingToken above) -- `isRecording` alone can't tell a
// cancelled recording apart from a brand-new one Auto-continue has since
// started, so both guard checks below compare the token too.
export async function checkEndpointCandidate(samples, sampleRate) {
  if (endpointChecking) return null;
  endpointChecking = true;
  const token = endpointRecordingToken;
  try {
    if (!endpointingActive() || token !== endpointRecordingToken) return null;
    const blob = encodeWav(samples, sampleRate);
    let transcript;
    try {
      transcript = await transcribeClip(blob);
    } catch (e) {
      return null;  // relay unreachable/erroring -- miss this candidate, the hard cap still protects us
    }
    // Recording ended (or Auto/voice mode changed, or a NEW recording has
    // since started) while we awaited -- a stale verdict must never act.
    if (!endpointingActive() || token !== endpointRecordingToken) return null;
    const complete = isTranscriptComplete(transcript);
    if (complete) finalizeEndpointing();
    return complete;
  } finally {
    endpointChecking = false;
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
  reportedPlaybackFailure = false;
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
        enqueueClip(event.url);
      }
    }
    if (event.type === 'main_audio') {
      if (shouldPlayAudio() && event.url) {
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
  const mode = getBackendMode();
  setStatus('loading', mode === 'agent' ? 'Agent thinking…'
    : mode === 'hermes' ? 'Hermes thinking…' : 'Thinking…');
  showThinking();
  activeTurnId = null;
  activeTurnAbort = new AbortController();

  const form = new FormData();
  if (blob) form.append('audio', blob, blobFilename(mime));
  if (transcript) form.append('transcript', transcript);
  if (state.currentConversationId) form.append('conversation_id', state.currentConversationId);
  form.append('backend', mode);
  // Persona rides along on lifeos and hermes alike now (#593), mirroring the
  // `backend !== 'agent'` gate askStream() uses for text turns — this used to
  // gate on 'lifeos' only, which left a spoken Hermes turn with no persona
  // and no spoken-style rules once it reached the Hermes proxy. The agent
  // backend keeps its current field-dropping behavior (it has no persona
  // pass-through at all, on either surface).
  if (mode !== 'agent' && config.personaId) {
    form.append('persona_id', config.personaId);
  }
  // Per-turn model pick — forwarded as `model_override`, mirroring how text
  // turns send it on /api/ask/stream (web/chat/ask-stream.js). Omitted for
  // 'auto' so the default turn stays byte-identical; only the lifeos backend
  // honors model picks — deliberately NOT extended to hermes (#593): model
  // selection on that backend belongs to the harness, not to LifeOS.
  // whisper-relay relays the field to /api/ask/stream (whisper-relay#24) —
  // until that ships the gateway drops it, degrading gracefully to the
  // default orchestrator.
  if (mode === 'lifeos' && config.model && config.model !== 'auto') {
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
      // Also gated on the lifeos backend specifically (#593): the spawn is
      // LifeOS-native. An orchestrating persona_id sent to the Hermes proxy
      // used to be rejected there with a 400 (hermes_proxy.py), so a
      // Hermes-backend turn never had a session to poll for; since #642
      // Hermes drives that persona itself (lifeos_agent_spawn) instead of
      // 400ing, but that's still not a LifeOS-linked session this client can
      // poll, so the gate stays lifeos-only (personaOrchestrates() is false
      // for hermes now too, #642 — see persona.js).
      if (mode === 'lifeos' && personaOrchestrates()) {
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
