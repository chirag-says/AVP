import { useEffect, useRef, useState } from "react";
import SiriOrb from "./SiriOrb";

export type AudioLevels = { local: number; remote: number };
export type OrbStatus = "idle" | "connecting" | "connected" | "error";

type Speaker = "idle" | "bot" | "user";

// Only c1..c3 (the conic gradient bands) change per speaker. `bg` is the orb's
// negative space — the inset shadow and the dot mask — so it tracks the page
// background, not the mood.
const BG = "oklch(97% 0.01 264.695)";

const PALETTES: Record<Speaker, { bg: string; c1: string; c2: string; c3: string }> = {
  // Desaturated: present, but clearly not listening to anything.
  idle: { bg: BG, c1: "oklch(72% 0.04 280)", c2: "oklch(76% 0.03 240)", c3: "oklch(74% 0.04 300)" },
  // smoothui's defaults — the pink/cyan/violet everyone reads as "Siri".
  bot: { bg: BG, c1: "oklch(75% 0.15 350)", c2: "oklch(80% 0.12 200)", c3: "oklch(78% 0.14 280)" },
  // Green: the patient has the floor.
  user: { bg: BG, c1: "oklch(78% 0.15 155)", c2: "oklch(82% 0.12 190)", c3: "oklch(75% 0.14 130)" },
};

// Idle rotation is a slow drift; speech spins it up.
const DURATION: Record<Speaker, number> = { idle: 20, bot: 7, user: 9 };

const SPEECH_THRESHOLD = 0.02;
// Hold the speaker colour briefly after level drops, or natural pauses between
// words strobe the orb between palettes.
const SPEAKER_HOLD_MS = 500;
const ATTACK = 0.35;
const RELEASE = 0.08;

const prefersReducedMotion = () =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

export default function VoiceOrb({
  status,
  levelsRef,
  onClick,
}: {
  status: OrbStatus;
  levelsRef: React.RefObject<AudioLevels>;
  onClick: () => void;
}) {
  const [speaker, setSpeaker] = useState<Speaker>("idle");
  const shellRef = useRef<HTMLSpanElement | null>(null);
  const connected = status === "connected";

  useEffect(() => {
    if (!connected) {
      setSpeaker("idle");
      shellRef.current?.style.setProperty("--level", "0");
      return;
    }

    // Audio levels arrive ~10x/sec and drive a per-frame animation. Routing
    // them through useState would re-render App — and with it the transcript
    // and the whole intake form — on every tick. So the loop owns the value
    // and writes one CSS variable; React only hears about speaker changes.
    let raf = 0;
    let level = 0;
    let lastSpeech = 0;
    let current: Speaker = "idle";
    const reduced = prefersReducedMotion();

    const tick = () => {
      const { local, remote } = levelsRef.current;
      const target = Math.min(1, Math.max(local, remote));

      // Rise fast so the orb feels instant on speech onset; fall slow so it
      // settles instead of buzzing.
      level += (target - level) * (target > level ? ATTACK : RELEASE);

      const now = performance.now();
      let next = current;
      if (remote > SPEECH_THRESHOLD) {
        next = "bot";
        lastSpeech = now;
      } else if (local > SPEECH_THRESHOLD) {
        next = "user";
        lastSpeech = now;
      } else if (now - lastSpeech > SPEAKER_HOLD_MS) {
        next = "idle";
      }

      if (next !== current) {
        current = next;
        setSpeaker(next);
      }

      // The component pauses its own rotation under prefers-reduced-motion;
      // level-driven pulsing is motion too, so it stops here as well.
      shellRef.current?.style.setProperty("--level", reduced ? "0" : level.toFixed(3));
      raf = requestAnimationFrame(tick);
    };

    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [connected, levelsRef]);

  const label =
    status === "connected"
      ? "End conversation"
      : status === "connecting"
        ? "Connecting to intake bot"
        : "Start conversation";

  return (
    <div className="orb-stage">
      <button
        type="button"
        className={`orb-button ${speaker}`}
        onClick={onClick}
        disabled={status === "connecting"}
        aria-label={label}
      >
        <span className="orb-shell" ref={shellRef}>
          <span className="orb-glow" aria-hidden="true" />
          <SiriOrb
            className="orb-visual"
            size="220px"
            colors={PALETTES[speaker]}
            animationDuration={connected ? DURATION[speaker] : DURATION.idle}
          />
        </span>
      </button>
      <p className="min-h-[1.2em] text-sm text-muted-foreground">
        {status === "connecting"
          ? "Connecting…"
          : status === "connected"
            ? speaker === "bot"
              ? "Speaking…"
              : speaker === "user"
                ? "Listening…"
                : "Go ahead"
            : status === "error"
              ? "Tap to try again"
              : "Tap to start"}
      </p>
    </div>
  );
}
