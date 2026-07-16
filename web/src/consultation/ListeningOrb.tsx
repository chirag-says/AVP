import { useEffect, useRef } from "react";
import SiriOrb from "../SiriOrb";

const prefersReducedMotion = () =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// Calm, clinical palette — deliberately not the chatbot's Siri pink/green.
const IDLE = { c1: "oklch(74% 0.05 220)", c2: "oklch(78% 0.04 200)", c3: "oklch(72% 0.05 240)" };
const LIVE = { c1: "oklch(70% 0.13 210)", c2: "oklch(76% 0.11 190)", c3: "oklch(68% 0.13 235)" };

/**
 * The orb reads mic level from a ref and writes one CSS variable (--level) per
 * animation frame, so loud speech visibly pushes the rings and core without
 * re-rendering React on every audio tick. Same pattern the chatbot's VoiceOrb
 * uses, pared down to a single (local) level.
 */
export default function ListeningOrb({
  listening,
  levelRef,
}: {
  listening: boolean;
  levelRef: React.RefObject<number>;
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!listening) {
      wrapRef.current?.style.setProperty("--level", "0");
      return;
    }
    if (prefersReducedMotion()) return;

    let raf = 0;
    let smoothed = 0;
    const tick = () => {
      const target = Math.min(1, Math.max(0, levelRef.current));
      // Rise fast, fall slow — instant on speech onset, no buzzing on release.
      smoothed += (target - smoothed) * (target > smoothed ? 0.35 : 0.08);
      wrapRef.current?.style.setProperty("--level", smoothed.toFixed(3));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [listening, levelRef]);

  return (
    <div ref={wrapRef} className="orb-wrap">
      <span className="orb-ring" aria-hidden />
      <span className="orb-ring" aria-hidden />
      <span className="orb-ring" aria-hidden />
      <div className="orb-core">
        <SiriOrb
          size="180px"
          colors={listening ? LIVE : IDLE}
          animationDuration={listening ? 8 : 18}
        />
      </div>
    </div>
  );
}
