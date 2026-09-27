import Daily from "@daily-co/daily-js";
import type { PipecatClient } from "@pipecat-ai/client-js";

// Browser-side mic processing for the voice pages.
//
// SmallWebRTCTransport never calls getUserMedia itself: its DailyMediaManager
// asks daily-js for the mic, and daily-js does the actual capture. Its
// constructor reuses an existing Daily call instance (getCallInstance() ??
// createCallObject()), so creating that instance first, with explicit
// constraints in the supported `inputSettings.audio.settings`, is how the app
// chooses echo cancellation / noise suppression / AGC without touching
// node_modules. Browser echo cancellation is the ONLY echo canceller in this
// system: the server never receives the speaker signal it would need to
// cancel echo itself.

export interface MicProcessing {
  echoCancellation: boolean;
  noiseSuppression: boolean;
  autoGainControl: boolean;
  // OS-level "focus on the user's voice" (Chrome maps it to platform effects
  // where they exist). Off unless explicitly tested: whether it applies, and
  // whether it distorts speech, depends on the device (DEVICE_TEST_PROTOCOL.md).
  voiceIsolation: boolean;
}

function envFlag(value: string | undefined, fallback: boolean): boolean {
  if (value === undefined || value.trim() === "") return fallback;
  return ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
}

// Returns null when explicit processing is switched off
// (VITE_VOICE_AUDIO_PROCESSING=false): Daily's own defaults then apply, the
// behavior before this module existed.
export function micProcessingFromEnv(): MicProcessing | null {
  const env = import.meta.env;
  if (!envFlag(env.VITE_VOICE_AUDIO_PROCESSING, true)) return null;

  // ?agc=0 / ?ns=0 / ?ec=0 (or =1) override one setting for a single page
  // load, so the device A/B in server/voice_eval/TEST_PLAN.md needs no rebuild.
  const query = new URLSearchParams(window.location.search);
  const pick = (param: string, envValue: string | undefined) =>
    query.has(param) ? envFlag(query.get(param) ?? undefined, true) : envFlag(envValue, true);
  return {
    echoCancellation: pick("ec", env.VITE_MIC_ECHO_CANCELLATION),
    noiseSuppression: pick("ns", env.VITE_MIC_NOISE_SUPPRESSION),
    autoGainControl: pick("agc", env.VITE_MIC_AUTO_GAIN_CONTROL),
    voiceIsolation: query.has("vi") ? envFlag(query.get("vi") ?? undefined, false) : envFlag(env.VITE_MIC_VOICE_ISOLATION, false),
  };
}

function constraintsFor(p: MicProcessing): MediaTrackConstraints {
  return { ...p, channelCount: 1 };
}

// Call before constructing the PipecatClient for each session.
export async function prepareMicProcessing(p: MicProcessing | null): Promise<void> {
  if (!p) return;
  // A previous session leaves its (already left) call instance behind, and
  // DailyMediaManager would silently reuse it with its old settings.
  const stale = Daily.getCallInstance();
  if (stale) await stale.destroy();
  Daily.createCallObject({ inputSettings: { audio: { settings: constraintsFor(p) } } });
}

// One row per setting: what we asked for, what the live track reports, and
// whether they disagree. "not requested" rows are informational (the browser
// picks them); only requested rows can mismatch.
const REPORTED = [
  "echoCancellation",
  "noiseSuppression",
  "autoGainControl",
  "sampleRate",
  "channelCount",
  "voiceIsolation",
  "deviceId",
] as const;
type Reported = (typeof REPORTED)[number];
type SettingValue = boolean | number | string | null;

export interface MicSettingRow {
  setting: Reported;
  requested: SettingValue | "not requested";
  actual: SettingValue | "not reported";
  mismatch: boolean;
}

export interface MicReport {
  rows: MicSettingRow[];
  mismatch: Reported[];
  deviceLabel?: string;
  // Chrome's OS-level "voice isolation" constraint; reported, never requested.
  voiceIsolationSupported: boolean;
  explicitProcessing: boolean;
}

function readSettings(track: MediaStreamTrack, p: MicProcessing | null): MicReport {
  // getSettings() on the LIVE track is the ground truth; a constraint that
  // getUserMedia accepted can still be silently not applied.
  const s = track.getSettings() as MediaTrackSettings & Record<string, unknown>;
  const requested: Partial<Record<Reported, SettingValue>> = p ? constraintsFor(p) as Partial<Record<Reported, SettingValue>> : {};
  const rows = REPORTED.map((setting): MicSettingRow => {
    const raw = s[setting];
    // Newer Chrome may report echoCancellation as a string mode ("all", "remote-only").
    const actual: SettingValue | "not reported" =
      raw === undefined ? "not reported" : typeof raw === "object" ? String(raw) : (raw as SettingValue);
    const want = setting in requested ? (requested[setting] as SettingValue) : "not requested";
    const applied = typeof actual === "string" && setting === "echoCancellation" ? actual !== "none" : actual;
    const mismatch = want !== "not requested" && actual !== "not reported" && applied !== want;
    return { setting, requested: want, actual, mismatch };
  });
  const supported = navigator.mediaDevices.getSupportedConstraints() as Record<string, boolean>;
  return {
    rows,
    mismatch: rows.filter((r) => r.mismatch).map((r) => r.setting),
    // Labels are only exposed after mic permission; never contains patient data.
    deviceLabel: track.label || undefined,
    voiceIsolationSupported: Boolean(supported.voiceIsolation),
    explicitProcessing: p !== null,
  };
}

// Reads what the browser ACTUALLY applied to the live mic track and prints a
// REQUESTED / ACTUAL / MISMATCH table in the console. If the browser ignored a
// requested setting, asks daily-js once more to re-acquire the mic with the
// constraints (DailyMediaManager swaps the new track into the WebRTC sender).
// The server copy omits deviceId: it is a stable per-site device identifier.
export async function reportMicProcessing(
  client: PipecatClient,
  p: MicProcessing | null,
  { sendToServer }: { sendToServer: boolean },
): Promise<MicReport | null> {
  let track = client.tracks().local.audio;
  if (!track) {
    console.warn("[mic] no local audio track to verify");
    return null;
  }
  let report = readSettings(track, p);

  if (p && report.mismatch.length) {
    console.warn("[mic] browser did not apply requested processing, retrying once:", report.mismatch);
    await Daily.getCallInstance()?.updateInputSettings({ audio: { settings: constraintsFor(p) } });
    await new Promise((r) => setTimeout(r, 500));
    track = client.tracks().local.audio ?? track;
    report = readSettings(track, p);
  }

  console.info(
    `[mic] live track settings (${report.explicitProcessing ? "explicit request" : "browser defaults"}; ` +
      `device "${report.deviceLabel ?? "unlabelled"}"; voiceIsolation supported: ${report.voiceIsolationSupported})`,
  );
  console.table(
    report.rows.map((r) => ({ SETTING: r.setting, REQUESTED: r.requested, ACTUAL: r.actual, MISMATCH: r.mismatch ? "YES" : "no" })),
  );
  if (report.mismatch.length) console.warn("[mic] still not applied after retry:", report.mismatch);
  if (sendToServer) {
    client.sendClientMessage("mic_settings", {
      ...report,
      rows: report.rows.filter((r) => r.setting !== "deviceId"),
    });
  }
  return report;
}
