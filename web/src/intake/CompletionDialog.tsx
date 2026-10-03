import { Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import LanguagePicker from "./LanguagePicker";
import type { UiText } from "./languages";

// Shown only in the COMPLETED phase, which the page enters only when the
// server reports that Supabase confirmed the save. It can't be dismissed by
// clicking outside or pressing Escape: the only way on is the next patient.
// The message is in the finished session's language; the picker chooses the
// next patient's, since Start Next Patient connects straight away.
export default function CompletionDialog({
  open,
  patientName,
  onStartNext,
  busy,
  text,
  nextLanguage,
  onNextLanguage,
}: {
  open: boolean;
  patientName: string | null;
  onStartNext: () => void;
  busy: boolean;
  text: UiText;
  nextLanguage: string;
  onNextLanguage: (code: string) => void;
}) {
  return (
    <Dialog open={open}>
      <DialogContent
        className="justify-items-center text-center"
        onEscapeKeyDown={(e) => e.preventDefault()}
        onPointerDownOutside={(e) => e.preventDefault()}
        onInteractOutside={(e) => e.preventDefault()}
      >
        <span
          aria-hidden="true"
          className="flex size-12 items-center justify-center rounded-full bg-emerald-600/10 text-emerald-700 dark:text-emerald-400"
        >
          <Check className="size-6" />
        </span>
        <DialogTitle>{text.completeTitle}</DialogTitle>
        <DialogDescription className="flex flex-col gap-1">
          <span className="text-foreground">{text.thankYou(patientName)}</span>
          <span>{text.recorded}</span>
        </DialogDescription>
        <div className="mt-2 flex w-full flex-col gap-2">
          <span className="text-xs text-muted-foreground">Next patient's language</span>
          <LanguagePicker value={nextLanguage} onChange={onNextLanguage} disabled={busy} />
        </div>
        <Button className="mt-2 w-full" onClick={onStartNext} disabled={busy} autoFocus>
          Start Next Patient
        </Button>
      </DialogContent>
    </Dialog>
  );
}
