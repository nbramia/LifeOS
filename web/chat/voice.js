// Voice mode for /chat (#361).
//
// Tap-to-talk (tap start, tap stop) — same interaction model as
// whisper-relay/static/app.js (onTalkClick), not hold-to-talk. Turn lifecycle:
// record → multipart POST /api/voice/turn/stream → SSE → done data → playback.

import { state, config, elements, endpoints } from './session.js';
import { addMessage, escapeHtml, formatContent, setStatus } from './thread.js';
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
// True if `token` — an AbortController some submitTurn() call captured as
// its own right after creating it (see `ownAbortController` below) — still
// identifies the turn currently in flight. False once a newer turn has
// replaced it, whether via a fresh submitTurn() call or cancelActiveTurn()
// clearing it first. Every settling path below (the success reconciliation,
// the AbortError branch, mid-stream-drop recovery, terminal failure, and the
// SSE handlers in consumeTurnStream) checks this before touching module
// state shared across turns, so a stale turn settling late can't clobber a
// newer turn's bookkeeping (#832) — the identity check #827 introduced for
// one branch, generalized to every exit path.
function isOwnTurn(token) {
  return activeTurnAbort === token;
}
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
// The in-flight turn's user bubble (#758). Held for the life of the turn so
// the authoritative `done` transcript can reconcile the one the earlier
// `transcript` SSE event rendered, instead of appending a second bubble.
let turnTranscriptEl = null;
// True once a turn's `done` payload has been processed (#758). The Cancel
// button doubles as a "stop playback" control while a completed reply's
// audio is still queued (voiceBusy/clipInFlight stay true across
// `await playbackChain`, see submitTurn()) -- tapping it then must stop the
// audio without deleting the transcript bubble for a turn that already
// completed and persisted server-side. Reset alongside turnTranscriptEl at
// the top of each turn.
let turnDone = false;
let ttsAudio = null;

// --- network resilience (#801) ---
//
// The last recorded clip, held until its turn *definitively* completes:
// success, an explicit user cancel, or an explicit dismiss (the "✕" on a
// failed turn's status row, below) -- never merely because a submission
// attempt failed. That's the whole point: a flaky connection must never
// force re-speaking the message. One slot, not a queue -- starting a new
// recording (a fresh, non-retry submitTurn() call) replaces whatever was
// held, same as today's "there's only one turn at a time" model.
let heldRecording = null; // { blob, mime } | null
// The status row (retrying/failed+Retry) appended after `turnTranscriptEl`
// -- or, when no transcript is known yet, after the thread's last message --
// tracked so a later state (a new retry attempt, success, a fresh turn) can
// remove/replace it without hunting the DOM. Never inside `.message-content`
// itself: `.message.user`'s own text is asserted verbatim by pre-existing
// tests (#758's eager-transcript suite), so this lives in a sibling node.
let turnStatusEl = null;

// Every write to `clipInFlight` goes through here (#734) rather than
// assigning the module variable directly, so the wake tap's AudioContext
// (`listenAudioCtx`, set up in startListening() below) suspends/resumes in
// lockstep with it -- see updateListenSuspension() just below, which also
// covers the `isRecording` half of the same problem (#724). Both branches
// are no-ops when Listening isn't running (listenAudioCtx null) or the
// context is already in the target state.
function setClipInFlight(value) {
  clipInFlight = value;
  updateListenSuspension();
}

// Shared by setClipInFlight() above and the `isRecording` writes in
// beginRecording()/beginWebAudioRecording()/stopRecordingAndSend() below
// (routed through setIsRecording(), #724): suspends the wake tap's
// AudioContext whenever a clip is playing OR a recording is in progress,
// resumes when neither. `ScriptProcessorNode.onaudioprocess` runs on the
// main thread regardless of whether handleListenFrame() has anything useful
// to do with the frame -- #734 covered the playback case, but the recording
// case had the identical shape and was still open: `listenProcessor` stayed
// connected and running for the entire time a recording was in progress,
// even though canDetectWake()'s own `!isRecording` guard already made every
// call a no-op (wake detection is meaningless while already recording), so
// the live callback was pure main-thread overhead contending with the
// recorder's/endpointer's own taps for that whole window -- exactly the
// aggregate-cost concern the taps-inventory doc above ensureAudioContext()
// warns about. suspend()/resume() stop and restart the whole graph's
// processing without touching the mic stream or the node wiring, so a wake
// match still works the instant recording ends, with zero extra
// getUserMedia calls.
//
// #740: suspend()/resume() above are NOT enough on their own -- they stop
// the graph's *processing*, but the underlying MediaStreamTrack keeps
// capturing regardless, so the same shouldSuspend condition below also
// drives setListenTracksEnabled(), which disables/re-enables the wake
// stream's own tracks in lockstep. See that function's comment and the
// taps-inventory block above ensureAudioContext() for why an open capture
// is a second, independent hazard from main-thread contention.
function updateListenSuspension() {
  const shouldSuspend = clipInFlight || isRecording;
  // #813 -- the dock's live-mic dot mirrors exactly what this function does
  // to the tap, so it can never claim the mic is live while the capture is
  // suspended. Computed before the `listenAudioCtx` bail-out below so the
  // "Listening isn't running at all" case turns the dot off too.
  updateListenIndicator(!!listenStream && !shouldSuspend);
  if (!listenAudioCtx) return;
  if (shouldSuspend && listenAudioCtx.state === 'running') {
    listenAudioCtx.suspend().catch(() => {});
  } else if (!shouldSuspend && listenAudioCtx.state === 'suspended') {
    listenAudioCtx.resume().catch(() => {});
  }
  setListenTracksEnabled(!shouldSuspend);
}

// #813 -- the dock's only signal that the mic is currently open. Driven
// solely by updateListenSuspension() above and stopListening() below, both
// of which own the real "is the wake tap holding a live capture" state, so
// the dot can't drift from it: it is deliberately NOT tied to the Listening
// checkbox, which stays checked through playback and recording while the
// capture itself is suspended (and stays checked outside voice mode, where
// no mic is held at all).
function updateListenIndicator(live) {
  const dot = elements.voiceListenDot;
  if (!dot) return;
  dot.classList.toggle('live', !!live);
}

// Every write to `isRecording` goes through here (#724), for the same
// reason setClipInFlight() above exists -- see updateListenSuspension().
function setIsRecording(value) {
  isRecording = value;
  updateListenSuspension();
}

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

// An explicit "begin recording immediately" deep link (#731 -- the iPhone
// Action Button, via Shortcuts, opening this page). Distinct from
// ?mode=voice on purpose: that param alone only arms wake-listening (a
// live mic that waits for a spoken wake burst), never an actual recording
// -- see resolveExplicitVoiceMode() above. This one skips straight to a
// recording, since an Action Button press already *is* the user's intent
// to speak. It never fires from ?mode=voice alone, isn't persisted to
// storage (unlike the voice-mode preference), and only ever fires once per
// page load: a Shortcut always opens the same fixed URL, so the param has
// to be present on this navigation to have any effect -- an ordinary
// reload of a URL that never carried it stays inert. Requires voice mode
// to already be active (pass ?mode=voice alongside it), and fails closed
// through the same guard + messaging as a manual tap on the talk button.
function maybeAutoStartRecording() {
  let record = null;
  try {
    record = new URLSearchParams(window.location.search).get('record');
  } catch (e) {
    return;  // no URLSearchParams / blocked
  }
  if (record !== '1' || !isVoiceMode()) return;
  // Deferred to a microtask: initVoice() (this function's caller) runs
  // synchronously inside main.js's initChat(), which unconditionally calls
  // setStatus('', 'Ready') on the very next line after initVoice() returns.
  // Calling reportMicBlocked() (below) synchronously here would have its
  // setStatus('error', ...) call instantly clobbered by that reset. A
  // microtask runs after the current synchronous call stack -- including
  // that reset -- finishes, so this always executes after it instead.
  Promise.resolve().then(() => {
    unlockTtsAudio();  // same as a manual tap -- best-effort without a real gesture
    const blocked = micBlockReason();
    if (blocked) {
      reportMicBlocked(blocked);
      return;
    }
    beginRecordingFromTap();
  });
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
  maybeAutoStartRecording();  // #731 -- only ever fires alongside ?mode=voice&record=1
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
  // The record path's own stream releases the same way on leaving voice mode
  // (#724, see releaseMicStream()'s own doc comment) -- unlike Listening's
  // it is NOT re-acquired on entering voice mode; it stays lazily acquired
  // by the next tap or wake trigger, same as always.
  if (!isVoiceMode()) releaseMicStream();
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
      // Deliberately plain `{ audio: true }`, unlike WAKE_STREAM_CONSTRAINTS
      // (Listening section, below) -- see that constant's comment for the
      // reasoning (#740).
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

// The talk-button's own stream, `micStream` -- lazily acquired above by
// requestMicInGesture() the first time it's needed -- used to never be
// released at all: applyVoiceMode() only ever tore down Listening's
// separate hold on leaving voice mode, never this one, so once a session
// had recorded even once the mic stayed live for the rest of the page's
// life regardless of mode (#724). Guarded on `!isRecording && !isStarting`
// so this never yanks the stream out from under a recording that's already
// in progress or in the brief async gap while one is starting -- leaving
// voice mode mid-recording keeps today's existing (unrelated, unchanged)
// behavior of that recording continuing to completion; this only closes the
// gap for the common case of leaving voice mode with nothing actively
// recording, which is what "no lingering live mic hold" is about. Unlike
// Listening's stream, this one is never re-acquired on *entering* voice
// mode -- it never was, before this fix either -- it stays lazy, acquired
// only by the next actual tap or wake trigger.
function releaseMicStream() {
  if (isRecording || isStarting) return;
  if (micStream) {
    for (const t of micStream.getTracks()) t.stop();
    micStream = null;
  }
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
  setIsRecording(true);
  setStatus('', 'Recording…');
  setTalkActive(true);
  maybeStartEndpointing(stream);  // #718 -- no-op unless Auto + voice mode
}

// --- Audio taps: inventory and the main-thread-contention invariant (#734) ---
//
// This file runs up to three independent `ScriptProcessorNode`s, each on its
// own `AudioContext`:
//   1. `audioProcessor` (below, iOS WebAudio recorder path) -- connected only
//      while actually recording on that platform; torn down by
//      releaseCapture() the instant recording stops.
//   2. `listenProcessor` (startListening()/stopListening(), "Listening"
//      section below) -- connected for as long as voice mode + the Listening
//      toggle are both on, which since #710 shipping the toggle on by
//      default means essentially the whole time voice mode is open. Its
//      `AudioContext` is suspended (not disconnected -- see the invariant
//      below) whenever a clip is playing or a recording is in progress
//      (updateListenSuspension(), #734 + #724), so "connected" here is about
//      the node wiring's lifetime, not whether it's actually processing at
//      any given moment.
//   3. `endpointProcessor` (maybeStartEndpointing()/stopEndpointing(), "Smart
//      turn endpointing" section below) -- connected only while a recording
//      that Auto-continue governs is in progress; torn down on every stop
//      path via stopRecordingAndSend()'s synchronous stopEndpointing() call.
//
// `ScriptProcessorNode` is deprecated specifically because its
// `onaudioprocess` callback runs on the **main thread** -- every connected
// node is a periodic main-thread callback, and playback (decoding, DOM/UI
// work, GC) competes with it for the same thread. On real hardware that
// contention is audible: scratchy, popping playback. It does NOT show up as
// a test failure -- headless suites don't render real audio -- so this is a
// listen-to-it-on-a-real-device regression class, which is why the rule is
// written down here instead of left to be rediscovered.
//
// The invariant: **a tap that isn't actively needed must be disconnected or
// have its `AudioContext` suspended -- not merely have its output ignored.**
// Bug #734 was exactly this mistake: `handleListenFrame()` correctly
// suspended *detection* while a clip played (`canDetectWake()`'s
// `clipInFlight` guard) but left `listenProcessor` connected and firing --
// the callback kept running on the main thread for no purpose the entire
// time a reply was audible. The fix (`setClipInFlight()` above) suspends
// `listenAudioCtx` itself in lockstep with `clipInFlight`, so the callback
// stops firing rather than merely discarding what it computes. Suspend, not
// `getUserMedia`-releasing teardown: the mic stream and node wiring survive
// untouched, so resuming never re-prompts for mic permission. #724 found the
// identical gap for the *recording* window -- `canDetectWake()`'s
// `!isRecording` guard already made detection a no-op the whole time a
// recording was in progress, but `listenProcessor` itself stayed connected
// and running regardless, same contention, different trigger.
// `updateListenSuspension()` (by `setClipInFlight()`/`setIsRecording()`)
// generalizes the fix to both conditions at once.
//
// A second, independent hazard (#740): main-thread contention is not the
// only way an unneeded tap degrades playback. `AudioContext.suspend()`
// stops the graph's *processing*, but it does NOT stop the underlying
// `MediaStreamTrack` -- the microphone capture itself stays live regardless
// of whether anything reads it. #734's fix above was necessary but not
// sufficient: it shipped believing `listenAudioCtx.suspend()` fully
// deactivated the wake tap during playback, and on real hardware it did
// not -- popping persisted, correlating exactly with the Listening toggle,
// because the capture was never actually stopped. An open capture, on its
// own, degrades output independently of CPU: `getUserMedia({ audio: true })`
// (the un-narrowed constraints, before #740) enables echoCancellation --
// which has to reference the current output signal to cancel it, hooking
// the output path for as long as the capture stays open -- and a live
// capture can also force a play-and-record audio session on some platforms,
// which can resample or otherwise degrade output. Both last exactly as long
// as the track is capturing, not as long as the graph is processing.
// **The correct invariant is that the mic must not merely be un-read during
// playback, it must be un-captured**: `track.enabled = false` on top of
// `AudioContext.suspend()`, not instead of it (the suspend/resume is still
// correct and worth keeping for its own reason -- CPU). See
// `setListenTracksEnabled()` and `WAKE_STREAM_CONSTRAINTS` in the Listening
// section below for the fix, and `isListenTrackEnabled()` for the browser
// test seam that distinguishes "capturing" from "processing" the way
// `isListenTapRunning()` already distinguishes "processing" from
// "detecting".
//
// Anyone adding a fourth tap (or re-enabling one of these outside its
// documented window) must account for the aggregate main-thread cost across
// whichever taps can be simultaneously connected, not just their own. If a
// genuine need for more concurrent taps emerges, the real fix is migrating
// to `AudioWorklet` (runs off the main thread, the supported replacement for
// `ScriptProcessorNode`) rather than adding another main-thread callback to
// the pile -- deferred here as a larger, separate change.
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
  setIsRecording(true);
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
  // No auto-continue here (#721). This function's only caller,
  // stopRecordingAndSend(), reaches it whenever the recording is being
  // discarded rather than submitted: a manual tap-to-stop that caught too
  // little/no audio, a #718 hard-cap/candidate finalize whose captured clip
  // still reads as silent, or a #723 idle-timeout exit (discard) -- none
  // of these is "a turn was submitted and its reply finished playing".
  // Auto-continue has to key off exactly that (submitTurn()'s own
  // maybeAutoContinue() call below, after `await playbackChain`) --
  // re-arming here as well used to treat any of these discards as if a
  // reply had just played, instantly restarting recording with no way to
  // stop without leaving Auto mode entirely (#721's original bug, for the
  // manual-stop case specifically).
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

// `discard: true` skips straight to handleSkippedEmptyRecording()'s
// no-submit/no-auto-continue branch instead of evaluating the captured
// blob -- the same teardown a manual stop or a silent recording uses, just
// without the isSilentBlob() check. Two callers set it, for the same reason
// from different evidence: a spoken cancel (#722), where the user's own
// words are the instruction to throw the clip away, and an idle-timeout
// exit (#723), which already knows from endpointHasSpeech -- the same VAD
// signal #718's candidate/hard-cap logic keys off -- that the recording
// captured no speech at all. Either way a discard doesn't care what's in
// the clip, so there's nothing to decode it for, and neither can fall
// through to submitTurn() on a blob that isSilentBlob()'s independently
// tuned thresholds happen to judge as "not silent". Every other caller (a
// manual tap, #718's hard cap/candidate finalize) omits it and keeps the
// isSilentBlob()-decided behavior exactly.
async function stopRecordingAndSend({ discard = false } = {}) {
  if (isStarting || !isRecording) return;
  // Set (and tear down endpointing) synchronously, before the MIN_RECORD_MS
  // await below, so a concurrent second call -- #718's hard cap, a
  // candidate's "complete" verdict, and #723's idle timeout can each reach
  // this function -- sees `!isRecording` and returns immediately instead of
  // double-stopping. Goes through setIsRecording() so the wake tap's
  // suspension follows the recording state (#724).
  setIsRecording(false);
  stopEndpointing();

  const elapsed = Date.now() - recordStartedAt;
  if (elapsed < MIN_RECORD_MS) {
    await new Promise((r) => setTimeout(r, MIN_RECORD_MS - elapsed));
  }

  setTalkActive(false);

  try {
    const { blob, mime } = await stopRecorder();
    if (discard || await isSilentBlob(blob)) {
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
  setClipInFlight(true);
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
  return promise.finally(() => { setClipInFlight(false); });
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
  setClipInFlight(false);
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
// `listenProcessor` below is tap #2 of the audio-taps inventory documented
// above ensureAudioContext() -- see that comment for the main-thread-
// contention invariant this section's suspend/resume machinery
// (setClipInFlight()) exists to satisfy.
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
// Investigated for #724 (merge the two streams into one getUserMedia hold)
// and kept separate. Findings, so the next agent doesn't relitigate this
// from scratch:
//   - No hard technical incompatibility rules a shared stream out. A
//     `MediaRecorder` and a live `MediaStreamAudioSourceNode` CAN read the
//     same `MediaStream` concurrently (each track supports multiple
//     consumers) -- and on iOS specifically (`useWebAudioRecorder` above)
//     the record path doesn't even use `MediaRecorder`; it uses the same
//     `createMediaStreamSource`-based approach this section does, so that
//     candidate conflict doesn't apply there either.
//   - Merging streams would NOT touch the actual cost #724 was filed to
//     reduce. The "second always-on audio graph" (battery/CPU) is a
//     `ScriptProcessorNode`/`AudioContext` count problem, not a
//     `getUserMedia` count problem -- fixed narrowly instead, by extending
//     the exact suspend/resume pattern #734 built (setClipInFlight()) to
//     also cover the recording window (updateListenSuspension()/
//     setIsRecording(), same section above ensureAudioContext()). That
//     closes the real contention with zero stream-lifetime changes.
//   - Merging would NOT reliably reduce permission prompts either: a
//     granted mic permission is scoped to the page's origin, not to any
//     particular `getUserMedia` call or stream -- a second call on an
//     already-granted origin does not re-prompt on its own. (#724's own
//     issue text concedes this: permission persistence is an origin-level
//     browser setting, not something this file's call count controls.)
//   - What a merge WOULD cost: reference-counted release across three
//     independent consumers (the recorder/`audioProcessor`, `listenProcessor`,
//     `endpointProcessor`), each currently owned exclusively by its own
//     stream and free to `track.stop()` it without asking. A shared stream
//     means none of them may ever be the one to stop a track the others
//     still want -- real, tractable complexity, but complexity in exchange
//     for a benefit (one fewer live hardware capture) that's speculative
//     beyond a single iOS Safari user report, while the codebase's own
//     history (#734's whole existence) is evidence WebKit's iOS audio-graph
//     behavior is fragile territory worth extra caution in, not less. Given
//     voice mode is reported working well on both Linux desktop and iPhone
//     today, that trade isn't worth taking without a reproducible failure
//     to actually fix. Revisit if one shows up.
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
  setClipInFlight(true);
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
  }).finally(() => { setClipInFlight(false); });
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

// #740 -- getUserMedia constraints for the wake stream specifically (see
// startListening() below), deliberately different from the plain
// `{ audio: true }` acquireMicStream() (the talk-button/recording path)
// still uses. Bare `audio: true` accepts the browser's defaults, which
// enable echoCancellation, noiseSuppression, and autoGainControl.
// Echo cancellation in particular has to reference the current output
// signal to cancel it, so an AEC-enabled capture hooks the output path for
// as long as it stays open -- and with Listening shipping on by default,
// that's the entire time voice mode is open, including through every TTS
// playback. The wake detector is a simple energy VAD plus a whisper
// round-trip on the captured burst; it gets no benefit from any of the
// three, and detection is already suspended during playback
// (updateListenSuspension() below), so the assistant's own voice leaking
// into an un-cancelled capture is harmless -- nothing reads it while a clip
// plays. The *recording* stream (acquireMicStream()) deliberately keeps the
// browser defaults: AEC/NS/AGC genuinely help transcription quality of the
// user's own speech, and unlike the wake stream, the recording stream is
// only open for user-directed capture -- not held open, armed, through
// playback -- so it isn't implicated by this bug (see #740: popping
// correlates exactly with the Listening toggle, not with whether a prior
// recording's stream is still cached).
const WAKE_STREAM_CONSTRAINTS = {
  audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
};

let listenStream = null;
let listenAudioCtx = null;
let listenSource = null;
let listenProcessor = null;
let listenBuffer = [];       // Float32Array chunks accumulated since the current burst started
let listenSpeechMs = 0;
let listenSilenceMs = 0;
let listenInBurst = false;
let listenChecking = false;  // an STT round-trip for an already-captured burst is in flight

// #740 -- disables/re-enables the wake stream's own MediaStreamTracks,
// called by updateListenSuspension() above in lockstep with
// listenAudioCtx.suspend()/resume(). AudioContext.suspend() only stops
// *processing* what the track produces; the browser keeps the underlying
// capture itself live regardless, which is exactly the mechanism #734
// missed -- an open capture forces a play-and-record audio session (and,
// were WAKE_STREAM_CONSTRAINTS not already off, would keep an AEC pipeline
// hooked to the output) for as long as any track feeding it stays live, not
// just while something reads its output. Per spec, `track.enabled = false`
// stops the track from producing anything (silence instead) without
// touching the permission grant, so toggling it back never re-prompts and
// never requires a fresh getUserMedia call -- same no-extra-acquisition
// guarantee the context suspend/resume already provides. No-op when
// Listening isn't running (listenStream null).
function setListenTracksEnabled(enabled) {
  if (!listenStream) return;
  for (const t of listenStream.getAudioTracks()) t.enabled = enabled;
}

// Test seam (#740): whether the wake stream's own tracks are currently
// capturing, as opposed to isListenTapRunning() below (whether the
// AudioContext graph is processing). The two are set in lockstep by
// updateListenSuspension() but are independent browser-level states -- this
// lets a browser test assert the capture itself stops, not just the
// context, closing the exact gap #734 missed.
export function isListenTrackEnabled() {
  if (!listenStream) return null;
  const tracks = listenStream.getAudioTracks();
  if (tracks.length === 0) return null;
  return tracks.every((t) => t.enabled === true);
}

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
    // #740 -- the wake stream is requested WITHOUT the browser's default
    // audio-processing chain (echoCancellation/noiseSuppression/
    // autoGainControl all explicitly off), unlike acquireMicStream() below
    // (the talk-button/recording path), which deliberately keeps the
    // defaults. See WAKE_STREAM_CONSTRAINTS for the reasoning -- this is a
    // considered split, not an oversight.
    stream = await navigator.mediaDevices.getUserMedia(WAKE_STREAM_CONSTRAINTS);
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
  // Covers the edge case of the Listening toggle being switched on while a
  // recording (or clip) is already in progress -- without this the freshly
  // created context would start (and stay) in the 'running' state
  // regardless of `isRecording`/`clipInFlight` until the next write to
  // either one happened to flip it (#724).
  updateListenSuspension();
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
  updateListenIndicator(false);  // #813 -- no mic held, so no live dot
}

// Test seam (#734): whether the wake tap's own AudioContext is actually
// running right now, as opposed to merely "detection would accept a frame
// if one arrived" (canDetectWake() above, which stays false for several
// other reasons -- recording, a turn in flight -- that have nothing to do
// with the graph being suspended). Lets a browser test assert the graph
// itself stops processing while a clip plays and starts again once it ends,
// rather than inferring it indirectly through wake-match side effects.
export function isListenTapRunning() {
  return !!(listenAudioCtx && listenAudioCtx.state === 'running');
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
// `endpointProcessor` below is tap #3 of the audio-taps inventory documented
// above ensureAudioContext() -- see that comment for the main-thread-
// contention invariant (connected only while actually needed, torn down via
// stopEndpointing() on every stop path).
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
// checked for a spoken cancel (isCancelUtterance() below, #722) BEFORE the
// completeness decision. Cancel -> discard through stopRecordingAndSend's
// `discard: true` branch, no submit. Otherwise run through
// isTranscriptComplete() below. Complete -> finalize through the SAME path a
// manual stop uses (stopRecordingAndSend(), never a parallel submit
// implementation). Incomplete -> keep recording; speech resuming re-arms the
// candidate timer. Silence that keeps growing past HARD_CAP_MS finalizes
// regardless of any candidate verdict, so an ambiguous or unreachable check
// can never hang the mic open forever.
//
// Idle timeout (#723): a THIRD, disjoint budget for the opposite situation --
// no speech at ALL yet this recording, so SILENCE_MS/HARD_CAP_MS above (both
// scoped to trailing silence *after* speech) have nothing to measure. Without
// this, a recording nobody ever spoke into (walked away, wake-triggered by
// noise, reply didn't need one) sits open forever: the hard cap can't save it
// because endpointHasSpeech never flips true, so endpointSilenceMs never even
// starts accruing. After IDLE_TIMEOUT_MS of this, stop and DISCARD -- submit
// nothing, no turn/conversation artifacts -- via the SAME manual-stop
// discard path an empty/silent recording already uses
// (handleSkippedEmptyRecording(), reached through stopRecordingAndSend()'s
// discard param below), never a parallel teardown. Crucially this must
// never re-arm auto-continue (that re-arm is exactly what would reopen the
// mic in a loop) -- see stopRecordingAndSend()'s doc comment: the discard
// branch it shares with a manual empty-recording stop has had no
// maybeAutoContinue() call since #721, so this path inherits that for free.
//
// Precedence vs. #718 is a straight handoff, not a race: endpointIdleMs (idle
// timeout's own counter) only accrues while `!endpointHasSpeech`, and
// endpointSilenceMs (#718's) only starts once `endpointHasSpeech` is true --
// see handleEndpointFrame() below. The very first speech frame flips
// endpointHasSpeech permanently true for the rest of THIS recording (nothing
// resets it back to false except a brand-new recording's
// resetEndpointState()), so the two counters can never both be live at once.
// Speech seen this recording -> #718 owns the ending, always. No speech at
// all -> idle timeout owns it, always.
//
// Guards are re-checked after every await, the same pattern
// triggerWakeRecording() uses after its chime -- a manual stop tap, a cancel,
// or Auto/voice mode changing while a candidate round-trip is in flight must
// never let a stale "complete" verdict resurrect or double-submit a turn the
// user already ended a different way.
const ENDPOINT_DEFAULT_SILENCE_MS = 1600;
const ENDPOINT_DEFAULT_HARD_CAP_MS = 3000;
const ENDPOINT_DEFAULT_IDLE_TIMEOUT_MS = 10000;

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

// Trailing phrases that read as the user abandoning the turn mid-recording
// (#722) -- checked the same way ENDPOINT_TRAILING_FILLER_WORDS above is:
// against the NORMALIZED end of the transcript, never a substring/includes()
// match anywhere in it. That distinction is the entire point of this
// feature -- "cancel my 3pm with Dana" is a real request that must still be
// submitted; only a transcript that *ends with* one of these phrases (most
// commonly the whole utterance, e.g. "Cancel.") means abandon it.
const ENDPOINT_CANCEL_PHRASES = [
  'cancel', 'cancel that', 'never mind', 'nevermind', 'forget it', 'scratch that',
];

// Pure heuristic, same shape as isTranscriptComplete() above -- exported so
// it's testable on its own. Normalizes (lowercase, strip trailing
// punctuation) then checks whether the trailing N words, for each phrase's
// own word count, equal that phrase. A short standalone cancel ("Cancel.")
// is just the N=1 case of the same check, not a separate branch.
export function isCancelUtterance(transcript) {
  const normalized = (transcript || '')
    .trim()
    .toLowerCase()
    .replace(/[.?!,;:'"()[\]]+$/g, '')
    .trim();
  if (!normalized) return false;
  const words = normalized.split(/\s+/).filter(Boolean);
  return ENDPOINT_CANCEL_PHRASES.some((phrase) => {
    const phraseWords = phrase.split(' ');
    if (words.length < phraseWords.length) return false;
    return words.slice(-phraseWords.length).join(' ') === phrase;
  });
}

// LIFEOS_VOICE_ENDPOINT_SILENCE_MS / _HARD_CAP_MS / _IDLE_TIMEOUT_MS, read
// from GET /api/chat/config (settings.py) the same way LIFEOS_CHAT_DEFAULT_VOICE
// reaches fetchDefaultVoiceMode() above. All three start at the designed
// defaults and only move if/when the (already-cached, shared) config fetch
// resolves with a valid override, so a slow or unreachable config never
// blocks endpointing -- it just runs on the built-in timings meanwhile.
// There is no server-side use of these values; the settings only exist to
// make this client-side timing operator-tunable without an env-var-free
// config file.
let endpointSilenceMsSetting = ENDPOINT_DEFAULT_SILENCE_MS;
let endpointHardCapMsSetting = ENDPOINT_DEFAULT_HARD_CAP_MS;
let endpointIdleTimeoutMsSetting = ENDPOINT_DEFAULT_IDLE_TIMEOUT_MS;

function loadEndpointingConfig() {
  fetchChatConfig().then((data) => {
    const silence = Number(data.voice_endpoint_silence_ms);
    const hardCap = Number(data.voice_endpoint_hard_cap_ms);
    const idleTimeout = Number(data.voice_idle_timeout_ms);
    if (Number.isFinite(silence) && silence > 0) endpointSilenceMsSetting = silence;
    if (Number.isFinite(hardCap) && hardCap > 0) endpointHardCapMsSetting = hardCap;
    if (Number.isFinite(idleTimeout) && idleTimeout > 0) endpointIdleTimeoutMsSetting = idleTimeout;
  });
}

let endpointCtx = null;
let endpointSource = null;
let endpointProcessor = null;
let endpointSampleRate = 0;
let endpointFrames = [];            // Float32Array frames captured since the current recording started
let endpointHasSpeech = false;      // at least one speech frame seen this recording
let endpointSilenceMs = 0;          // trailing silence since the last speech frame
let endpointIdleMs = 0;             // (#723) silence elapsed while NO speech has been seen yet this recording
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
  endpointIdleMs = 0;
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

// Test seam (#734): whether the endpointing tap is currently wired up, so a
// browser test can assert it's torn down after every stop path (manual stop,
// hard-cap finalize, spoken-cancel discard) rather than inferring it
// indirectly.
export function isEndpointTapActive() {
  return !!endpointProcessor;
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
  if (!endpointHasSpeech) {
    // No speech at all yet this recording (#723) -- a disjoint silence
    // budget from endpointSilenceMs below, which only starts once speech has
    // been seen. See the "Idle timeout" doc comment above this section for
    // why the two can never both be counting.
    endpointIdleMs += frameMs;
    if (endpointIdleMs >= endpointIdleTimeoutMsSetting) finalizeIdleTimeout();
    return;
  }

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

// Discards through the SAME stop-the-recorder path as finalizeEndpointing()
// above -- stopRecordingAndSend({ discard: true }) -- never a parallel
// teardown implementation. `discard: true` routes past submitTurn()
// entirely into handleSkippedEmptyRecording(), the existing no-submit path a
// silent/empty recording already uses; that function's own doc comment is
// why a spoken cancel, like a manual stop, never re-arms auto-continue
// (#721) -- auto-continue only fires from submitTurn()'s own
// maybeAutoContinue() call after a reply actually plays, which a discarded
// recording never reaches. Called by checkEndpointCandidate() below on a
// cancel-utterance verdict; not exported -- unlike the hard cap,
// there's no real-timer path to it that a browser test can't otherwise
// reach, so checkEndpointCandidate() itself is the only test seam needed.
function discardEndpointing() {
  if (!isRecording) return;
  stopRecordingAndSend({ discard: true }).catch((err) => {
    setStatus('error', 'Error');
    addMessage('⚠️ ' + (err?.message || 'Recording failed'), 'assistant');
    setTalkActive(false);
  });
}

// Idle-timeout finalize (#723) -- stops and DISCARDS, never submits. Also
// through stopRecordingAndSend(), but with its `discard` param set:
// this is the SAME handleSkippedEmptyRecording() teardown a manual stop on
// an empty/silent recording already uses (see stopRecordingAndSend()'s doc
// comment), not a parallel discard implementation -- and that path has had
// no auto-continue re-arm since #721, so this inherits that for free. Same
// `!isRecording` guard/no-op-if-already-stopped reasoning as
// finalizeEndpointing() above. Exported for the same headless-test reason:
// it's the exact function real continuous no-speech silence crossing
// IDLE_TIMEOUT_MS calls, with no way to wait out that much real silence
// through the live audio graph in a browser test.
export function finalizeIdleTimeout() {
  if (!isRecording) return;
  stopRecordingAndSend({ discard: true }).catch((err) => {
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
// reached one -- suspended, superseded, or the relay call failed), or the
// string 'cancelled' on a spoken-cancel verdict (#722), so tests can assert
// on any of the three outcomes directly.
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
    // Checked BEFORE the completeness decision (#722) -- a cancel verdict
    // preempts it entirely, the same way it rides the same candidate-pause
    // transcript rather than opening a second detection path/timer/STT call.
    if (isCancelUtterance(transcript)) {
      discardEndpointing();
      return 'cancelled';
    }
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

// --- the user's own words in the thread ---

// Renders the spoken turn's transcript as a user bubble (#758), matching what
// the text path does at send time (askStream() in ask-stream.js). Called as
// soon as the transcript is known -- from the `transcript` SSE event, which the
// relay emits the moment STT lands and long before the reply finishes -- rather
// than only from the terminal `done` payload, so the thread doesn't sit empty
// while the assistant thinks.
//
// Idempotent per turn: a second call (the authoritative `done` transcript)
// reconciles the existing bubble in place instead of appending a duplicate.
function renderUserTranscript(text) {
  if (!text || !text.trim()) return;
  if (turnTranscriptEl) {
    const contentEl = turnTranscriptEl.querySelector('.message-content');
    if (contentEl) contentEl.innerHTML = formatContent(text);
    return;
  }
  turnTranscriptEl = addMessage(text, 'user');
  // showThinking() runs before the transcript is known, so the placeholder is
  // already in the thread -- move it back to the end to keep the thread in
  // user-then-assistant order.
  if (thinkingEl && thinkingEl.parentNode) {
    thinkingEl.parentNode.appendChild(thinkingEl);
  }
}

function clearUserTranscript() {
  if (turnTranscriptEl) {
    turnTranscriptEl.remove();
    turnTranscriptEl = null;
  }
}

// --- network-resilience UI: retrying/failed status row (#801) ---
//
// A small row appended right after the turn's bubble (or, if none exists
// yet -- an audio-only turn whose initial submission never even reached STT
// -- after the thread's last message) showing either a subtle "retrying"
// state or a definitive "failed" state with Retry/dismiss affordances.
// Deliberately its own DOM node, sibling to `.message.user`, never a child
// of it -- see `turnStatusEl`'s own comment for why.
function removeVoiceTurnStatus() {
  if (turnStatusEl) {
    turnStatusEl.remove();
    turnStatusEl = null;
  }
}

function insertVoiceTurnStatus(className) {
  removeVoiceTurnStatus();
  const el = document.createElement('div');
  el.className = 'voice-turn-status ' + className;
  const anchor = turnTranscriptEl;
  if (anchor && anchor.parentNode) {
    anchor.parentNode.insertBefore(el, anchor.nextSibling);
  } else {
    elements.messagesEl.appendChild(el);
  }
  turnStatusEl = el;
  return el;
}

// A subtle in-thread echo of the retry attempt already reflected in the top
// status bar (setStatus() call at the caller) -- only rendered when a real
// bubble exists to attach it to (a manual Retry-tap's reused bubble, or a
// caller-supplied transcript). An audio-only turn's very first submission
// attempt has no bubble yet (STT hasn't run), so for that case the top
// status bar is the only "retrying" signal -- still visible, just not
// double-shown in the thread.
function showRetryingStatus(attempt, max) {
  setStatus('loading', `Retrying… (${attempt}/${max})`);
  if (!turnTranscriptEl) return;
  const el = insertVoiceTurnStatus('retrying');
  el.textContent = `Retrying… (${attempt}/${max})`;
}

// Definitive failure: the recording is held (heldRecording, set by the
// caller) and this offers the only two things #801 promises -- resubmit the
// same audio, or explicitly throw it away. No third "do nothing" outcome
// silently loses it: the row simply stays until one of those two is tapped,
// or the next recording replaces it (submitTurn()'s own teardown, see
// `heldRecording`'s comment).
function showFailedStatus(onRetry, onDismiss) {
  const el = insertVoiceTurnStatus('failed');
  const label = document.createElement('span');
  label.className = 'voice-turn-status-label';
  label.textContent = 'Couldn’t send';
  const retryBtn = document.createElement('button');
  retryBtn.type = 'button';
  retryBtn.className = 'voice-turn-retry-btn';
  retryBtn.textContent = 'Retry';
  retryBtn.addEventListener('click', () => { removeVoiceTurnStatus(); onRetry(); });
  const dismissBtn = document.createElement('button');
  dismissBtn.type = 'button';
  dismissBtn.className = 'voice-turn-dismiss-btn';
  dismissBtn.title = 'Discard this recording';
  dismissBtn.textContent = '✕';
  dismissBtn.addEventListener('click', () => { removeVoiceTurnStatus(); onDismiss(); });
  el.appendChild(label);
  el.appendChild(retryBtn);
  el.appendChild(dismissBtn);
}

// Ensures a bubble exists to attach the failed state to. Normally one
// already does -- `turnTranscriptEl`, either from a caller-supplied
// transcript or the `transcript` SSE event -- but a network-class failure on
// the *initial* submission (never even reached the gateway) or a mid-stream
// drop before `transcript` ever arrived leaves it null: STT never ran, so
// there is no transcript to show. This still must not lose the recording
// (#801's whole point), so a placeholder user bubble is created purely as an
// anchor for the failed-state row and its Retry affordance -- never shown by
// a successful turn (renderUserTranscript() already no-ops on empty text, so
// this can't collide with the "no bubble on a transcript-less success" case,
// #758/test_turn_without_a_transcript_renders_no_user_bubble).
function ensureTurnBubble() {
  if (turnTranscriptEl) return turnTranscriptEl;
  turnTranscriptEl = addMessage('🎤 Voice message', 'user');
  if (thinkingEl && thinkingEl.parentNode) {
    thinkingEl.parentNode.appendChild(thinkingEl);
  }
  return turnTranscriptEl;
}

// --- SSE turn stream (ported from whisper-relay consumeTurnStream) ---
function parseSseChunk(buffer, onEvent) {
  const lines = buffer.split('\n');
  const remainder = lines.pop() || '';
  for (const line of lines) {
    if (!line.startsWith('data: ')) continue;
    let event;
    try {
      event = JSON.parse(line.slice(6));
    } catch {
      continue; // ignore malformed chunks
    }
    // Outside the try: `cancelled`/`error` events intentionally throw from
    // onEvent (handleEvent, below) to unwind consumeTurnStream's read loop --
    // that must propagate to submitTurn()'s catch, not get swallowed here
    // alongside a genuine JSON.parse failure.
    onEvent(event);
  }
  return remainder;
}

async function consumeTurnStream(response, ownAbortController) {
  playbackChain = Promise.resolve();
  reportedPlaybackFailure = false;
  let doneData = null;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const handleEvent = (event) => {
    // A turn's events arrive progressively over the life of this stream --
    // long enough for a newer turn to have started in the meantime (the user
    // cancelled and immediately re-recorded). `isOwnTurn` gates every branch
    // below that would otherwise touch module state a newer turn now owns
    // (#832); `doneData` is this call's own local, so it's always safe to
    // set regardless.
    if (event.type === 'started') {
      if (isOwnTurn(ownAbortController)) {
        activeTurnId = event.turn_id;
        showCancel(true);
      }
    }
    if (event.type === 'transcript') {
      if (isOwnTurn(ownAbortController)) renderUserTranscript(event.text);
    }
    if (event.type === 'cancelled') {
      // This frame means the turn was cancelled server-side -- possibly by
      // this tab's own Cancel button (cancelActiveTurn(), which already
      // cleared the bubble), but a turn's lifetime is server-owned (#611) and
      // can just as easily be cancelled from elsewhere (another tab/device
      // on the same conversation). Clear here too so an externally-cancelled
      // turn leaves no trace either -- idempotent if cancelActiveTurn()
      // already ran. Gated the same as every other branch here: a newer
      // turn's own bubble must survive a stale cancel arriving late (#832).
      if (isOwnTurn(ownAbortController)) clearUserTranscript();
      throw new DOMException('Turn cancelled', 'AbortError');
    }
    if (event.type === 'error') {
      // A definitive, server-reported failure -- whisper-relay's TurnPipeline
      // (turns.py) always runs `registry.end(turn_id)` in its `finally`
      // before yielding this, so the turn has already ended server-side.
      // Tagged so submitTurn()'s catch (#801) treats this as a normal failed
      // state, never a "mid-stream drop" needing the poll-for-completion
      // dance below -- there's nothing ambiguous left to resolve, and a
      // resubmit can never double-execute against a turn that's confirmed
      // over.
      const error = new Error(event.message || 'Turn failed');
      error.voiceTurnDefinitive = true;
      throw error;
    }
    if (event.type === 'status_audio') {
      if (event.message && isOwnTurn(ownAbortController)) setStatus('loading', event.message);  // spoken status text
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

// --- submission retry-with-backoff + mid-stream-drop recovery (#801) ---
//
// Scope: the retry ladder below covers ONLY the *initial* submission -- the
// POST that starts a turn, before any SSE bytes have come back. Once that
// response is `ok` and consumeTurnStream() starts reading, a failure is a
// "mid-stream drop" instead (handled separately, below) -- deliberately
// different semantics, because by then the turn may genuinely be running
// server-side and a blind resubmit risks double-executing it.
//
// Retryable = "the request never reached a response":
//   - fetch() itself rejecting -- offline, DNS failure, connection
//     refused/reset, a timeout the browser surfaces as a bare TypeError.
//     There is no HTTP status at all.
//   - HTTP 502 from THIS repo's own proxy. Read `api/routes/voice.py`:
//     `voice_proxy()` raises 502 from exactly one place --
//     `except (httpx.RequestError, httpx.InvalidURL)` around the call that
//     reaches the voice gateway -- i.e. only when the gateway itself could
//     not be reached or timed out reaching it. A 502 here can therefore
//     never mean a turn actually started server-side (the gateway never got
//     the request), so it carries the same "never reached a response"
//     guarantee a raw fetch rejection does.
// Never retryable:
//   - Any 4xx -- the request was rejected outright and won't get better
//     (per the issue: "a rejected request won't get better").
//   - Any OTHER 5xx (500, 503, 504, ...). Deliberate, not an oversight: none
//     of them is proven to mean "never ran" the way this proxy's 502 is --
//     `voice_turn_stream` (whisper-relay's route) always answers 200 and
//     streams a `{"type":"error",...}` SSE event for its own internal
//     failures (STT down, text backend unavailable), so an HTTP-level 5xx
//     other than 502 isn't a path this code has confirmed is side-effect
//     -free, and retrying blind against an unproven guarantee is exactly
//     the double-execution risk this feature exists to avoid elsewhere.
// An AbortError from the user's OWN cancel (activeTurnAbort) is handled by
// the caller before this classification is ever consulted.
// Test seam (#801): whether a recording is currently held for retry, without
// exposing the blob itself -- lets a browser test assert directly that a
// recording survived a failed submission, or was discarded on
// cancel/dismiss/success, the same pattern isEndpointTapActive()/
// isListenTapRunning() already use for their own internal state.
export function hasHeldRecording() {
  return !!heldRecording;
}

const SUBMIT_RETRY_DELAYS_MS = [1000, 3000, 9000];
const SUBMIT_RETRY_JITTER = 0.2; // +/-20%, so concurrent retries don't sync up

function isRetryableSubmitFailure(res) {
  return res.status === 502;
}

function jitteredDelay(baseMs) {
  const spread = baseMs * SUBMIT_RETRY_JITTER;
  return baseMs + (Math.random() * 2 - 1) * spread;
}

// Resolves after a jittered backoff, or rejects with the same AbortError
// shape cancelActiveTurn() already produces if `signal` fires first while
// waiting -- a spoken/tapped cancel during a pending retry behaves exactly
// like a cancel during the fetch itself (#801 interaction proof).
function waitForRetry(baseMs, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('Turn cancelled', 'AbortError')); return; }
    const timer = setTimeout(() => { cleanup(); resolve(); }, jitteredDelay(baseMs));
    const onAbort = () => { cleanup(); reject(new DOMException('Turn cancelled', 'AbortError')); };
    function cleanup() {
      clearTimeout(timer);
      signal.removeEventListener('abort', onAbort);
    }
    signal.addEventListener('abort', onAbort);
  });
}

// POSTs the turn-start request, retrying network-class failures per the
// policy above. Returns the (possibly non-ok, e.g. 4xx) Response for the
// caller to interpret -- only a network-class failure that exhausts every
// retry, or the user's own cancel, throws. Total attempts = 1 initial +
// `SUBMIT_RETRY_DELAYS_MS.length` retries (4, at today's ladder).
async function postTurnStart(form, signal) {
  const maxRetries = SUBMIT_RETRY_DELAYS_MS.length;
  for (let attempt = 0; ; attempt += 1) {
    let res;
    try {
      res = await fetch(`${endpoints.voice}/turn/stream`, { method: 'POST', body: form, signal });
    } catch (err) {
      if (err?.name === 'AbortError') throw err; // user cancel -- never retried
      if (attempt >= maxRetries) throw err; // network-class, retries exhausted
      await backoffBeforeRetry(attempt, signal);
      continue;
    }
    if (res.ok || !isRetryableSubmitFailure(res) || attempt >= maxRetries) return res;
    await backoffBeforeRetry(attempt, signal);
  }
}

async function backoffBeforeRetry(attempt, signal) {
  const maxAttempts = SUBMIT_RETRY_DELAYS_MS.length + 1;
  showRetryingStatus(attempt + 2, maxAttempts); // +2: 1-indexed, and this is the NEXT attempt
  await waitForRetry(SUBMIT_RETRY_DELAYS_MS[attempt], signal); // throws AbortError on cancel
}

// #801 mid-stream drop: the initial POST succeeded and consumeTurnStream()
// was reading real SSE frames when the connection died (a network error
// mid-read, or the stream ending with no `done`/`error`/`cancelled` ever
// seen -- see submitTurn()'s "Turn ended without a response" throw). Unlike
// the initial-submission ladder above, this is deliberately NOT
// auto-retried: the turn may still be running server-side, and blindly
// resubmitting the same audio risks running it twice.
//
// Investigated before choosing this (read-only, whisper-relay's
// `voice_gateway/`): `cancel.py`'s TurnRegistry only supports firing a
// cancel by turn_id, never querying status; `storage.py.read_meta()` writes
// a turn's full result (transcript, response_text, conversation_id) but has
// no HTTP route exposing it. What IS exposed and does answer "did it
// finish": `GET /api/voice/audio/{turn_id}` (`routes/voice.py`'s
// `_serve_clip()`) -- 404s until `turns.py` writes the final TTS clip, which
// only happens after the LLM reply is in hand, immediately before `done`.
// So a 200 there is proof the turn actually completed, even though nothing
// exposes the transcript/response text to redisplay -- a real gap, noted as
// a follow-up in the PR report.
//
// Chosen semantics: poll that endpoint briefly (HEAD, no body download).
// Found -> the turn completed; there is nothing left to retry, so the
// recording is discarded and the recovered audio is offered via the
// existing tap-to-replay affordance instead of a lost turn. Not found
// within the window -> genuinely unknown, so no auto-retry (that could
// double-execute); the recording stays held and Retry becomes an explicit,
// user-initiated action, same as any other terminal failure.
const MIDSTREAM_POLL_ATTEMPTS = 3;
const MIDSTREAM_POLL_INTERVAL_MS = 1500;

async function pollForCompletedAudio(turnId) {
  for (let i = 0; i < MIDSTREAM_POLL_ATTEMPTS; i += 1) {
    if (i > 0) await new Promise((r) => setTimeout(r, MIDSTREAM_POLL_INTERVAL_MS));
    try {
      const res = await fetch(`${endpoints.voice}/audio/${encodeURIComponent(turnId)}`, { method: 'HEAD' });
      if (res.ok) return true;
    } catch (e) {
      /* unreachable -- keep polling within the budget, same as "not found yet" */
    }
  }
  return false;
}

function recoverCompletedTurn(turnId) {
  heldRecording = null; // confirmed complete server-side -- nothing left to retry
  turnDone = true;
  setStatus('', 'Ready');
  const audioUrl = `${endpoints.voice}/audio/${encodeURIComponent(turnId)}`;
  const el = addMessage(
    'The connection dropped before the reply could be shown here, but it finished — tap to hear it.',
    'assistant',
  );
  attachReplay(el, [audioUrl]);
}

function handleTerminalFailure(message) {
  const bubble = ensureTurnBubble();
  addMessage('⚠️ ' + message, 'assistant');
  showFailedStatus(
    () => {
      const rec = heldRecording;
      if (!rec) return;
      submitTurn({ blob: rec.blob, mime: rec.mime, retryBubble: bubble });
    },
    () => { heldRecording = null; },
  );
}

// `ownAbortController` is checked at every point this async function is
// about to touch module state shared across turns: once up front (a newer
// turn may already own things by the time this is even called) and again
// after the poll's await (a newer turn may have started while it ran) --
// #832, generalizing the identity check #827 introduced for one branch.
// Nothing awaits between either check and the state it guards, so neither
// window can go stale between the check and the touch.
async function handleMidStreamDrop(err, ownAbortController) {
  if (!isOwnTurn(ownAbortController)) return;
  const turnId = activeTurnId;
  if (turnId) {
    setStatus('loading', 'Reconnecting…');
    const completed = await pollForCompletedAudio(turnId);
    if (!isOwnTurn(ownAbortController)) return;
    if (completed) {
      recoverCompletedTurn(turnId);
      return;
    }
  }
  setStatus('error', 'Error');
  handleTerminalFailure(err?.message || 'Voice turn failed');
}

// Exported so the headless test harness can drive a turn without a real mic
// (getUserMedia/MediaRecorder don't run headless). `retryBubble` is internal
// (#801) -- set only by the failed-state Retry button's own click handler
// above, never by a real caller, so a retried turn reconciles into the SAME
// bubble instead of submitTurn() resetting to null and letting
// renderUserTranscript() create a second one.
export async function submitTurn({ blob, mime, transcript, retryBubble } = {}) {
  voiceBusy = true;
  state.isLoading = true;
  const mode = getBackendMode();
  setStatus('loading', mode === 'agent' ? 'Agent thinking…'
    : mode === 'hermes' ? 'Hermes thinking…' : 'Thinking…');
  removeVoiceTurnStatus();
  if (retryBubble) {
    turnTranscriptEl = retryBubble;
  } else {
    // A fresh (non-retry) turn supersedes whatever the last held
    // recording/failed status was -- #801's "one held blob, replaced by the
    // next recording, not an unbounded queue". Any earlier failed bubble the
    // user never tapped Retry/dismiss on stays in the thread as history; it
    // just loses the now-stale affordance along with removeVoiceTurnStatus()
    // above, since its blob is about to be overwritten below.
    turnTranscriptEl = null;
  }
  turnDone = false;
  // A caller-supplied transcript needs no STT round trip, so it can go in the
  // thread immediately (#758); an audio turn's bubble lands on the relay's
  // `transcript` SSE event instead.
  renderUserTranscript(transcript);
  showThinking();
  activeTurnId = null;
  activeTurnAbort = new AbortController();
  // Captured so the catch below (#827) can tell "this turn's own controller
  // is still current" from "a newer turn already replaced it" -- distinct
  // from activeTurnAbort itself, which a later submitTurn() call reassigns.
  const ownAbortController = activeTurnAbort;

  // #801 -- hold the recording until the turn *definitively* completes (see
  // `heldRecording`'s own comment). Set here, unconditionally, so a fresh
  // recording AND a manual retry (which passes the same blob back in) both
  // keep exactly one slot current.
  if (blob) heldRecording = { blob, mime };

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

  // #801 -- once the initial POST answers `ok` and the SSE stream starts
  // being read, a failure switches from "retry the submission" to "mid-
  // stream drop" semantics (see handleMidStreamDrop()'s own comment for
  // why they differ).
  let turnAccepted = false;
  try {
    const res = await postTurnStart(form, activeTurnAbort.signal);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      // Guarded like every other settling touch below (#832): a newer turn
      // may already own the thinking placeholder by the time this awaited
      // response comes back.
      if (isOwnTurn(ownAbortController)) clearThinking();
      throw new Error(data.detail || `Request failed (${res.status})`);
    }
    turnAccepted = true;

    const data = await consumeTurnStream(res, ownAbortController);
    if (!data) {
      if (isOwnTurn(ownAbortController)) clearThinking();
      throw new Error('Turn ended without a response');
    }
    if (!isOwnTurn(ownAbortController)) {
      // A newer turn already owns everything below -- this stale turn has
      // nothing left to reconcile into shared state (#832). The stream was
      // still fully drained by consumeTurnStream() above either way.
      return;
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
    turnDone = true;
    heldRecording = null; // #801 -- the turn reached the server and completed; nothing to retry
    // `done` is authoritative — reconciles the bubble the `transcript` event
    // already rendered, or renders it if that event never arrived.
    renderUserTranscript(data.transcript);
    const playbackUrls = [...(data.status_audio_urls || []), data.audio_url].filter(Boolean);
    if (data.response_text) {
      const el = addMessage(data.response_text, 'assistant');
      attachReplay(el, playbackUrls);  // tap to replay
    }

    await playbackChain;
    // A tap on Cancel during playback (the "stop playback" use of that
    // button once a reply has already landed -- see cancelActiveTurn()'s own
    // comment) is exactly what settles `playbackChain` early, and that same
    // tap already reset activeTurnAbort/voiceBusy/status itself. Checked
    // again here, after the await, for the same reason as everywhere else in
    // this function (#832): don't redo that reset onto whatever turn is
    // current by the time this resumes.
    if (!isOwnTurn(ownAbortController)) return;
    showCancel(false);
    setStatus('', 'Ready');
    await maybeAutoContinue();  // re-record if Auto-continue is on
  } catch (err) {
    if (err?.name === 'AbortError') {
      // A local Cancel-button tap runs cancelActiveTurn() synchronously
      // before this ever settles -- it already reset thinking/cancel/status
      // and nulled activeTurnAbort, so redoing that here would risk
      // clobbering a next turn that started in the meantime. A turn
      // cancelled server-side (the 'cancelled' SSE branch above, reachable
      // with no local cancelActiveTurn() call -- see #827) never goes
      // through that reset, and activeTurnAbort is still THIS turn's own
      // controller in that case -- checked by identity, not mere presence,
      // so a stale turn settling after a newer one has already started
      // can't stomp on the newer turn's UI either.
      if (isOwnTurn(ownAbortController)) {
        clearThinking();
        showCancel(false);
        setStatus('', 'Ready');
      }
      return;
    }
    if (!isOwnTurn(ownAbortController)) return; // superseded -- nothing left to report (#832)
    clearThinking();
    showCancel(false);
    // #801 -- a mid-stream drop (the SSE stream died after a genuine `ok`
    // response, and this wasn't a definitive server-reported error) gets the
    // poll-then-explicit-retry-only treatment; every other failure (the
    // initial submission never got a usable response at all, or the server
    // told us definitively it failed) goes straight to the ordinary
    // failed+Retry state.
    if (turnAccepted && !err?.voiceTurnDefinitive) {
      await handleMidStreamDrop(err, ownAbortController);
    } else {
      setStatus('error', 'Error');
      handleTerminalFailure(err?.message || 'Voice turn failed');
    }
  } finally {
    // Every branch above already bails out (via `return`) once it detects a
    // newer turn has taken over, so in practice this only ever runs while
    // still current -- but it's the very last thing this turn does, and the
    // one reset every prior version of this code applied unconditionally
    // (#832), so it gets the same identity check as everywhere else rather
    // than relying on that invariant holding forever.
    if (isOwnTurn(ownAbortController)) {
      activeTurnId = null;
      activeTurnAbort = null;
      voiceBusy = false;
      state.isLoading = false;
    }
  }
}

function showCancel(on) {
  if (elements.voiceCancelBtn) elements.voiceCancelBtn.classList.toggle('visible', on);
}

// Exported so the headless test harness can drive it directly (#758) --
// same reason submitTurn() is exported, and needed to test the "stop
// playback after done" case without faking real audio element timing.
export function cancelActiveTurn() {
  activeTurnAbort?.abort();
  if (activeTurnId) {
    fetch(`${endpoints.voice}/turn/${encodeURIComponent(activeTurnId)}/cancel`, { method: 'POST' }).catch(() => {});
  }
  playbackChain = Promise.resolve();
  stopAllAudio();
  clearThinking();
  // A cancelled turn is never persisted, so it leaves no trace in the thread —
  // drop the user bubble along with the thinking placeholder (#758). But this
  // button is also the "stop playback" control once a turn has already
  // reached `done` (voiceBusy/clipInFlight stay true while audio plays out,
  // see submitTurn()'s `await playbackChain`) -- that bubble is the
  // authoritative, already-persisted transcript, so leave it alone then.
  if (!turnDone) clearUserTranscript();
  // #801 -- an explicit cancel is one of the three discard triggers
  // (success, cancel, explicit dismiss) for the held recording, and clears
  // any retrying/failed status row along with it. Already null/absent by
  // the time a turn has reached `done` (cleared on success; stop-playback
  // cancel never has anything left to discard), so this is a no-op on that
  // path rather than a special case.
  heldRecording = null;
  removeVoiceTurnStatus();
  activeTurnId = null;
  activeTurnAbort = null;
  voiceBusy = false;
  state.isLoading = false;
  showCancel(false);
  setStatus('', 'Ready');
}
