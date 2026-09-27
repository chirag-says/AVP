import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { SCENARIO } from "./scenario";

// Development test sessions only (open the page with ?scenario=bystander, etc.;
// the server must run with VOICE_DEBUG_SCENARIO set or the marks are ignored).
//
// Nothing in the voice pipeline knows WHO is speaking, so the tester supplies
// that: hold P while the patient talks, hold B while the other person talks.
// Only the key-down/key-up timing is sent; the server scores what the
// pipeline did against these marks (server/voice/scenario.py).


type Role = "patient" | "bystander";
const KEYS: Record<string, Role> = { p: "patient", b: "bystander" };

export default function ScenarioMarkers({
  enabled,
  send,
}: {
  enabled: boolean;
  send: (type: string, data: unknown) => void;
}) {
  const [held, setHeld] = useState<Record<Role, boolean>>({ patient: false, bystander: false });
  const heldRef = useRef(held);

  const mark = (role: Role, down: boolean) => {
    if (!enabled || heldRef.current[role] === down) return;
    heldRef.current = { ...heldRef.current, [role]: down };
    setHeld(heldRef.current);
    send("scenario_marker", { role, down });
  };

  useEffect(() => {
    const onKey = (down: boolean) => (e: KeyboardEvent) => {
      const role = KEYS[e.key.toLowerCase()];
      if (role && !e.repeat) mark(role, down);
    };
    const keydown = onKey(true);
    const keyup = onKey(false);
    window.addEventListener("keydown", keydown);
    window.addEventListener("keyup", keyup);
    return () => {
      window.removeEventListener("keydown", keydown);
      window.removeEventListener("keyup", keyup);
    };
  });

  const holdButton = (role: Role, label: string) => (
    <Button
      variant={held[role] ? "default" : "outline"}
      size="sm"
      disabled={!enabled}
      onPointerDown={() => mark(role, true)}
      onPointerUp={() => mark(role, false)}
      onPointerLeave={() => mark(role, false)}
    >
      {label}
    </Button>
  );

  return (
    <div className="mx-auto mt-4 max-w-md rounded-lg border border-dashed p-3 text-center text-xs text-muted-foreground">
      <p className="mb-2">
        Test scenario <strong>{SCENARIO}</strong>: hold <kbd>P</kbd> while the patient speaks, <kbd>B</kbd> while the
        other person speaks. Results appear in the server&apos;s voice_telemetry line.
      </p>
      <div className="flex justify-center gap-2">
        {holdButton("patient", "Patient speaking (P)")}
        {holdButton("bystander", "Other person speaking (B)")}
      </div>
    </div>
  );
}
