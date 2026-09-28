import { Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

// Shown only in the COMPLETED phase, which the page enters only when the
// server reports that Supabase confirmed the save. It can't be dismissed by
// clicking outside or pressing Escape: the only way on is the next patient.
export default function CompletionDialog({
  open,
  patientName,
  onStartNext,
  busy,
}: {
  open: boolean;
  patientName: string | null;
  onStartNext: () => void;
  busy: boolean;
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
        <DialogTitle>Intake Complete</DialogTitle>
        <DialogDescription className="flex flex-col gap-1">
          <span className="text-foreground">{patientName ? `Thank you, ${patientName}.` : "Thank you."}</span>
          <span>Your information has been successfully recorded.</span>
        </DialogDescription>
        <Button className="mt-2 w-full" onClick={onStartNext} disabled={busy} autoFocus>
          Start Next Patient
        </Button>
      </DialogContent>
    </Dialog>
  );
}
