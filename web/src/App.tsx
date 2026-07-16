import { useCallback, useEffect, useRef, useState } from "react";
import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import { Database, ScrollText, Stethoscope, VolumeX } from "lucide-react";
import Transcript, { type TurnEntry } from "./Transcript";
import IntakeForm from "./IntakeForm";
import Records from "./Records";
import VoiceOrb, { type AudioLevels } from "./VoiceOrb";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import "./App.css";

// /start is the runner's canonical session-bootstrap endpoint (registers a
// session, returns ICE config); the SmallWebRTC transport then negotiates
// the actual WebRTC offer against /api/offer on its own. Passing /api/offer
// directly to connect() is deprecated and was producing a 422 (empty body)
// in testing — startBotAndConnect() against /start is the current API.
const START_ENDPOINT = import.meta.env.VITE_START_ENDPOINT ?? "http://localhost:7860/start";

type Status = "idle" | "connecting" | "connected" | "error";

export default function App() {
  const [view, setView] = useState<"intake" | "records">("intake");
  const [status, setStatus] = useState<Status>("idle");
  const [turns, setTurns] = useState<TurnEntry[]>([]);
  const [fields, setFields] = useState<Record<string, unknown>>({});
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [audioBlocked, setAudioBlocked] = useState(false);
  const clientRef = useRef<PipecatClient | null>(null);
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
    setStatus("connecting");
    setErrorMsg(null);
    setAudioBlocked(false);

    const client = new PipecatClient({
      transport: new SmallWebRTCTransport(),
      enableMic: true,
      callbacks: {
        onBotReady: () => setStatus("connected"),
        onDisconnected: () => {
          setStatus("idle");
          stopBotMeter();
          levelsRef.current.local = 0;
        },
        onLocalAudioLevel: (level) => {
          levelsRef.current.local = level;
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
          if (track.kind === "audio" && audioRef.current) {
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
          if (data.final) setTurns((prev) => [...prev, { role: "patient", text: data.text }]);
        },
        onBotTranscript: (data) => {
          setTurns((prev) => [...prev, { role: "bot", text: data.text }]);
        },
        onServerMessage: (data) => {
          // bot.py broadcasts {type: "field_update", key, value} after every save_field call
          if (data?.type === "field_update") {
            setFields((prev) => {
              const next = structuredClone(prev);
              const parts = String(data.key).split(".");
              let node: any = next;
              for (const part of parts.slice(0, -1)) node = node[part] ??= {};
              node[parts[parts.length - 1]] = data.value;
              return next;
            });
          }
          if (data?.type === "intake_complete") {
            setStatus("idle");
          }
        },
      },
    });

    clientRef.current = client;

    try {
      await client.startBotAndConnect({ endpoint: START_ENDPOINT });
    } catch (err) {
      console.error(err);
      setErrorMsg(err instanceof Error ? err.message : "Failed to connect");
      setStatus("error");
    }
  }, [startBotMeter, stopBotMeter]);

  const disconnect = useCallback(async () => {
    await clientRef.current?.disconnect();
    setStatus("idle");
    stopBotMeter();
  }, [stopBotMeter]);

  return (
    <div className="min-h-svh bg-background">
      <audio ref={audioRef} autoPlay />

      <header className="sticky top-0 z-10 border-b bg-background/80 backdrop-blur-sm">
        <div className="mx-auto flex h-14 max-w-5xl items-center gap-3 px-6">
          <Stethoscope className="size-5 shrink-0 text-muted-foreground" />
          <span className="font-medium">Hospital Voice Intake</span>
          <Badge variant="outline" className="font-normal text-muted-foreground">
            PoC
          </Badge>

          <span className="ml-auto flex items-center gap-3">
            {status === "connected" && (
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
        {errorMsg && (
          <p className="mb-4 rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
            {errorMsg}
          </p>
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
              status={status}
              levelsRef={levelsRef}
              onClick={status === "idle" || status === "error" ? connect : disconnect}
            />
            <main className="mt-8 grid items-start gap-6 md:grid-cols-2">
              <Transcript turns={turns} />
              <IntakeForm values={fields} />
            </main>
          </>
        ) : (
          <Records />
        )}
      </div>
    </div>
  );
}
