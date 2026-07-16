import { useEffect, useRef } from "react";
import { MessagesSquare } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";

export interface TurnEntry {
  role: "patient" | "bot";
  text: string;
}

export default function Transcript({ turns }: { turns: TurnEntry[] }) {
  const endRef = useRef<HTMLDivElement | null>(null);

  // Pin to the newest turn. Anchored on a sentinel element rather than the
  // viewport's scrollTop because ScrollArea owns that node internally.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  return (
    <Card className="h-full">
      <CardHeader className="border-b">
        <CardTitle className="text-base">Conversation</CardTitle>
      </CardHeader>

      <CardContent className="p-0">
        <ScrollArea className="h-[520px]">
          <div className="flex flex-col gap-3 px-4 pb-4">
            {turns.length === 0 ? (
              <div className="flex flex-col items-center gap-2 py-20 text-center text-muted-foreground">
                <MessagesSquare className="size-8 opacity-40" />
                <p className="text-sm">Tap the orb and start talking.</p>
              </div>
            ) : (
              turns.map((turn, i) => (
                <div
                  key={i}
                  className={`flex flex-col gap-1 ${
                    turn.role === "patient" ? "items-end" : "items-start"
                  }`}
                >
                  <span className="px-1 text-xs text-muted-foreground">
                    {turn.role === "bot" ? "Assistant" : "You"}
                  </span>
                  <p
                    className={`max-w-[85%] rounded-2xl px-3.5 py-2 text-sm leading-relaxed ${
                      turn.role === "patient"
                        ? "rounded-br-sm bg-primary text-primary-foreground"
                        : "rounded-bl-sm bg-muted text-foreground"
                    }`}
                  >
                    {turn.text}
                  </p>
                </div>
              ))
            )}
            <div ref={endRef} />
          </div>
        </ScrollArea>
      </CardContent>
    </Card>
  );
}
