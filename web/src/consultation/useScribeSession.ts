import { useCallback, useRef, useState } from "react";
import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import type { Segment } from "./api";

// The scribe's session-bootstrap endpoint — the runner's canonical /start,
// same as the chatbot uses, but on the scribe's port. startBotAndConnect()
// registers the session and the transport negotiates WebRTC from there.
const START_ENDPOINT =
  import.meta.env.VITE_SCRIBE_START_ENDPOINT ?? "http://localhost:7861/start";

export type ScribeStatus = "idle" | "connecting" | "listening" | "error";

export interface ScribeSession {
  status: ScribeStatus;
  segments: Segment[];
  errorMsg: string | null;
  levelRef: React.RefObject<number>;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  reset: () => void;
}

export function useScribeSession(): ScribeSession {
  const [status, setStatus] = useState<ScribeStatus>("idle");
  const [segments, setSegments] = useState<Segment[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const clientRef = useRef<PipecatClient | null>(null);
  const startedAtRef = useRef<number>(0);
  // Mic level drives the orb from an animation loop; a ref keeps those ~20/sec
  // updates from re-rendering the growing transcript on every tick.
  const levelRef = useRef<number>(0);

  const start = useCallback(async () => {
    setStatus("connecting");
    setErrorMsg(null);
    setSegments([]);

    const client = new PipecatClient({
      transport: new SmallWebRTCTransport(),
      enableMic: true,
      callbacks: {
        // The listen-only worker calls set_bot_ready() on client-ready, so
        // onBotReady still fires even though this bot never speaks.
        onBotReady: () => {
          startedAtRef.current = performance.now();
          setStatus("listening");
        },
        onDisconnected: () => {
          levelRef.current = 0;
        },
        onLocalAudioLevel: (level) => {
          levelRef.current = level;
        },
        onUserTranscript: (data) => {
          // Only finals become durable segments; interims would thrash the list.
          if (!data.final) return;
          const text = data.text?.trim();
          if (!text) return;
          const ts = startedAtRef.current
            ? (performance.now() - startedAtRef.current) / 1000
            : undefined;
          setSegments((prev) => [...prev, { text, ts }]);
        },
      },
    });

    clientRef.current = client;

    try {
      await client.startBotAndConnect({ endpoint: START_ENDPOINT });
    } catch (err) {
      console.error(err);
      setErrorMsg(
        err instanceof Error ? err.message : "Could not connect to the scribe service.",
      );
      setStatus("error");
    }
  }, []);

  const stop = useCallback(async () => {
    await clientRef.current?.disconnect();
    clientRef.current = null;
    levelRef.current = 0;
    // Drop out of "listening" immediately. Without this the page can fall back
    // to the live-recording screen (orb + running timer) if summarizing then
    // fails — looking like it restarted the mic when it hasn't.
    setStatus("idle");
  }, []);

  const reset = useCallback(() => {
    setSegments([]);
    setErrorMsg(null);
    setStatus("idle");
  }, []);

  return { status, segments, errorMsg, levelRef, start, stop, reset };
}
