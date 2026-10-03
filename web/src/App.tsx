import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import { Database, LoaderCircle, ScrollText, Stethoscope, TriangleAlert, VolumeX } from "lucide-react";
import Transcript from "./Transcript";
import IntakeForm from "./IntakeForm";
import Records from "./Records";
import VoiceOrb, { type AudioLevels } from "./VoiceOrb";
import { micProcessingFromEnv, prepareMicProcessing, reportMicProcessing } from "./audio/micProcessing";
import ScenarioMarkers from "./audio/ScenarioMarkers";
import { SCENARIO } from "./audio/scenario";
import CompletionDialog from "./intake/CompletionDialog";
import LanguagePicker from "./intake/LanguagePicker";
import { DEFAULT_LANGUAGE, languageFor } from "./intake/languages";
import { initialIntakeState, intakeReducer, isLocked, type Phase } from "./intake/intakeState";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import "./App.css";

// /start is the runner's canonical session-bootstrap endpoint (registers a
// session, returns ICE config); the SmallWebRTC transport then negotiates
// the actual WebRTC offer against /api/offer on its own. Passing /api/offer
// directly to connect() is deprecated and was producing a 422 (empty body)
// in testing — startBotAndConnect() against /start is the current API.
const START_ENDPOINT = import.meta.env.VITE_START_ENDPOINT ?? "http://localhost:7860/start";

type OrbStatus = "idle" | "connecting" | "connected" | "error";

const ORB_STATUS: Record<Phase, OrbStatus> = {
  IDLE: "idle",
  CONNECTING: "connecting",
  STARTING_NEXT_PATIENT: "connecting",
  IN_PROGRESS: "connected",
  FINALIZING: "connected",
  SAVING: "connected",
  SAVE_FAILED: "connected",
  COMPLETED: "connected",
  ERROR: "error",
};

export default function App() {
  const [view, setView] = useState<"intake" | "records">("intake");
  // All patient-specific UI state (phase, transcript, fields, name, session)
  // lives in one reducer so the next patient can replace it in one step.
  const [intake, dispatch] = useReducer(intakeReducer, undefined, () => initialIntakeState());
  const [audioBlocked, setAudioBlocked] = useState(false);
  // The language the next session starts in (not patient data: it stays for
  // the next patient until changed), and the one the current session used.
  // The ref lets connect() read the latest choice without being re-created.
  const [language, setLanguageState] = useState(DEFAULT_LANGUAGE);
  const languageRef = useRef(DEFAULT_LANGUAGE);
  const setLanguage = useCallback((code: string) => {
    languageRef.current = code;
    setLanguageState(code);
  }, []);
  const [sessionLanguage, setSessionLanguage] = useState(DEFAULT_LANGUAGE);
  const clientRef = useRef<PipecatClient | null>(null);
  // Generation of the live connection; callbacks from older ones are ignored.
  const genRef = useRef(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // Drives the orb's pulse. Deliberately a ref, not state: these update ~20x/sec
  // and VoiceOrb reads them from an animation loop, so pushing them through
  // React would re-render the transcript and intake form on every tick for no
  // visible gain.
  const levelsRef = useRef<AudioLevels>({ local: 0, remote: 0 });
  const botMeterRef = useRef<{ ctx: AudioContext; timer: number } | null>(null);

  const stopBotMeter = useCallback(() => {
    if (!botMeterRef.current) return;
    clearInterval(botMeterRef.current.timer);
    void botMeterRef.current.ctx.close();
    botMeterRef.current = null;
    levelsRef.current.remote = 0;
  }, []);

  // The bot's speaking level has to be measured here rather than taken from the
  // client's onRemoteAudioLevel callback, which never fires on this transport:
  // SmallWebRTCTransport builds `new DailyMediaManager(false, false, ...)`, and
  // that first `false` is enablePlayer — without a _wavStreamPlayer the manager
  // never starts the interval that emits remote levels. (onLocalAudioLevel is
  // unaffected; Daily's own observer drives it.) So tap the same remote track
  // we bind for playback and measure it directly.
  const startBotMeter = useCallback(
    (stream: MediaStream) => {
      stopBotMeter();
      const ctx = new AudioContext();
      void ctx.resume();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      ctx.createMediaStreamSource(stream).connect(analyser);

      const samples = new Uint8Array(analyser.fftSize);
      // 20Hz — matches the cadence of Daily's local observer, and VoiceOrb's
      // rAF smoothing interpolates between samples, so a faster poll would
      // burn CPU without looking any different.
      const timer = window.setInterval(() => {
        analyser.getByteTimeDomainData(samples);
        let sum = 0;
        for (const sample of samples) {
          const centered = (sample - 128) / 128;
          sum += centered * centered;
        }
        // RMS of speech sits around 0.05–0.2; the gain lifts that into roughly
        // the same 0..1 shape Daily reports for the mic, so the orb reacts
        // comparably to both voices.
        levelsRef.current.remote = Math.min(1, Math.sqrt(sum / samples.length) * 4);
      }, 50);

      botMeterRef.current = { ctx, timer };
    },
    [stopBotMeter],
  );

  useEffect(() => stopBotMeter, [stopBotMeter]);

  const connect = useCallback(async () => {
    const gen = ++genRef.current;
    const code = languageRef.current;
    dispatch({ type: "CONNECT_START", newGen: gen });
    setSessionLanguage(code);
    setAudioBlocked(false);

    const micProcessing = micProcessingFromEnv();
    try {
      await prepareMicProcessing(micProcessing);
    } catch (err) {
      // Never block a session over this: the browser defaults still apply.
      console.warn("[mic] could not apply mic processing settings", err);
    }

    const client = new PipecatClient({
      transport: new SmallWebRTCTransport(),
      enableMic: true,
      callbacks: {
        onBotReady: () => {
          dispatch({ type: "CONNECTED", gen });
          if (SCENARIO) client.sendClientMessage("scenario_name", { name: SCENARIO });
          reportMicProcessing(client, micProcessing, { sendToServer: true }).catch((err) =>
            console.warn("[mic] could not read mic settings", err),
          );
        },
        onDisconnected: () => {
          dispatch({ type: "DISCONNECTED", gen });
          if (genRef.current === gen) {
            stopBotMeter();
            levelsRef.current.local = 0;
          }
        },
        onLocalAudioLevel: (level) => {
          if (genRef.current === gen) levelsRef.current.local = level;
        },
        onTrackStarted: (track) => {
          // Neither client-js nor small-webrtc-transport auto-plays incoming
          // audio — the app owns binding the bot's remote track to a real
          // <audio> element. Without this, TTS audio is generated and sent
          // over WebRTC but never actually reaches the speakers.
          //
          // SmallWebRTCTransport wires this directly to the browser's native
          // RTCPeerConnection "track" event, which only ever fires for
          // *remote* tracks (confirmed by reading the transport's source) —
          // unlike Daily's transport, it never fires for our own local mic,
          // so no participant.local check is needed or even possible here
          // (this transport doesn't pass a participant argument at all).
          if (track.kind === "audio" && audioRef.current && genRef.current === gen) {
            const stream = new MediaStream([track]);
            audioRef.current.srcObject = stream;
            startBotMeter(stream);
            // The `autoPlay` attribute alone isn't reliable here — the track
            // arrives asynchronously well after the click that started the
            // connection, and Chrome's autoplay policy can silently block
            // that. Play explicitly and surface a recovery button if blocked
            // instead of failing silently (which is what happened before).
            audioRef.current.play().catch(() => setAudioBlocked(true));
          }
        },
        onUserTranscript: (data) => {
          if (data.final) dispatch({ type: "TURN", gen, role: "patient", text: data.text });
        },
        onBotTranscript: (data) => {
          dispatch({ type: "TURN", gen, role: "bot", text: data.text });
        },
        onServerMessage: (data) => {
          switch (data?.type) {
            case "field_update":
              dispatch({ type: "FIELD_UPDATE", gen, key: String(data.key), value: data.value, status: data.status });
              break;
            case "intake_finalizing":
              dispatch({ type: "FINALIZING", gen });
              break;
            case "intake_saving":
              dispatch({ type: "SAVING", gen });
              break;
            case "intake_save_failed":
              dispatch({ type: "SAVE_FAILED", gen, message: String(data.message ?? "The information could not be saved.") });
              break;
            case "intake_complete":
              // Sent only after Supabase confirmed the save. The patient is done:
              // stop listening, the bot's closing line still plays.
              client.enableMic(false);
              dispatch({ type: "COMPLETE", gen, sessionId: data.session_id ?? null, patientName: data.patient_name ?? null });
              break;
          }
        },
      },
    });

    clientRef.current = client;
    // Dev builds only (compiled out of production): lets the end-to-end test
    // (server/tests/e2e_intake_browser.py) answer as the patient via sendText.
    if (import.meta.env.DEV) (window as unknown as { __intakeClient?: PipecatClient }).__intakeClient = client;

    try {
      // The runner hands "body" to the bot (runner_args.body); unknown codes become English there.
      await client.startBotAndConnect({ endpoint: START_ENDPOINT, requestData: { body: { language: code } } });
    } catch (err) {
      console.error(err);
      dispatch({ type: "CONNECT_FAILED", gen, message: err instanceof Error ? err.message : "Failed to connect" });
    }
  }, [startBotMeter, stopBotMeter]);

  const disconnect = useCallback(async () => {
    await clientRef.current?.disconnect();
    stopBotMeter();
  }, [stopBotMeter]);

  const retrySave = useCallback(() => {
    clientRef.current?.sendClientMessage("retry_save", {});
  }, []);

  // Start Next Patient: close the finished session, wipe every patient-specific
  // value in one reducer step, then open a brand-new server session (new
  // IntakeSession, engine, LLM context and database row). No page reload.
  const startNextPatient = useCallback(async () => {
    const old = clientRef.current;
    clientRef.current = null;
    const gen = ++genRef.current;
    dispatch({ type: "START_NEXT_PATIENT", newGen: gen });
    stopBotMeter();
    levelsRef.current = { local: 0, remote: 0 };
    if (audioRef.current) audioRef.current.srcObject = null;
    try {
      await old?.disconnect();
    } catch (err) {
      console.warn("previous session already closed", err);
    }
    await connect();
  }, [connect, stopBotMeter]);

  const { phase } = intake;
  const locked = isLocked(phase);
  const idle = phase === "IDLE" || phase === "ERROR";
  // Patient-facing text: the picked language before a session, the session's during and after it.
  const ui = languageFor(idle ? language : sessionLanguage).ui;

  return (
    <div className="min-h-svh bg-background">
      <audio ref={audioRef} autoPlay />

      <header className="sticky top-0 z-10 border-b bg-background/80 backdrop-blur-sm">
        <div className="mx-auto flex h-14 max-w-5xl items-center gap-3 px-6">
          <Stethoscope className="size-5 shrink-0 text-muted-foreground" />
          <span className="font-medium">AVP Hospital Voice Intake</span>
          <Badge variant="outline" className="font-normal text-muted-foreground">
            PoC
          </Badge>

          <span className="ml-auto flex items-center gap-3">
            {phase === "IN_PROGRESS" && (
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <span className="size-1.5 animate-pulse rounded-full bg-emerald-500" />
                Live
              </span>
            )}
            <Button variant="outline" size="sm" onClick={() => setView(view === "intake" ? "records" : "intake")}>
              {view === "intake" ? "Reception records" : "Back to intake"}
            </Button>
            {/* Separate pages (their own HTML entries), so real navigations, not the view toggle. */}
            <Button asChild variant="outline" size="sm">
              <a href="/consultation.html">
                <ScrollText /> Consultation scribe
              </a>
            </Button>
            <Button asChild variant="outline" size="sm">
              <a href="/export.html">
                <Database /> Export
              </a>
            </Button>
          </span>
        </div>
      </header>

      <div className="mx-auto max-w-5xl px-6 py-8">
        {phase === "ERROR" && intake.message && (
          <p className="mb-4 rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
            {intake.message}
          </p>
        )}
        {(phase === "FINALIZING" || phase === "SAVING") && (
          <p role="status" className="mb-4 flex items-center justify-center gap-2 text-sm text-muted-foreground">
            <LoaderCircle className="size-4 animate-spin" /> {ui.saving}
          </p>
        )}
        {phase === "SAVE_FAILED" && (
          <div
            role="alert"
            className="mb-4 flex flex-wrap items-center justify-center gap-3 rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-2.5 text-sm text-destructive"
          >
            <TriangleAlert className="size-4" />
            {intake.message}
            <Button variant="outline" size="sm" onClick={retrySave}>
              Retry saving
            </Button>
          </div>
        )}
        {audioBlocked && (
          <div className="mb-4 flex items-center justify-center">
            <Button
              variant="outline"
              size="sm"
              onClick={() => audioRef.current?.play().then(() => setAudioBlocked(false))}
            >
              <VolumeX /> Click to enable bot audio
            </Button>
          </div>
        )}

        {view === "intake" ? (
          <>
            <VoiceOrb
              status={ORB_STATUS[phase]}
              levelsRef={levelsRef}
              // While saving or completed the session belongs to the save; the
              // orb can't end it or start a new one (use Start Next Patient).
              onClick={idle ? connect : locked ? () => {} : disconnect}
              text={ui}
            />
            {idle && (
              <div className="mt-3">
                <LanguagePicker value={language} onChange={setLanguage} />
              </div>
            )}
            <p className="mt-2 text-center text-xs text-muted-foreground">
              For best recognition, use a headset or keep the microphone close to the patient. A laptop
              microphone also picks up people talking nearby.
            </p>
            {SCENARIO && (
              <ScenarioMarkers
                enabled={phase === "IN_PROGRESS"}
                send={(type, data) => clientRef.current?.sendClientMessage(type, data)}
              />
            )}
            <main className="mt-8 grid items-start gap-6 md:grid-cols-2">
              <Transcript turns={intake.turns} />
              <IntakeForm values={intake.fields} status={intake.fieldStatus} saved={phase === "COMPLETED"} />
            </main>
          </>
        ) : (
          <Records />
        )}
      </div>

      <CompletionDialog
        open={phase === "COMPLETED"}
        patientName={intake.patientName}
        onStartNext={startNextPatient}
        busy={phase !== "COMPLETED"}
        text={languageFor(sessionLanguage).ui}
        nextLanguage={language}
        onNextLanguage={setLanguage}
      />
    </div>
  );
}
