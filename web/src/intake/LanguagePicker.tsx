import { cn } from "@/lib/utils";
import { LANGUAGES } from "./languages";

// The language the next session starts in. The bot still follows the patient
// if they switch languages mid-conversation.
export default function LanguagePicker({
  value,
  onChange,
  disabled = false,
}: {
  value: string;
  onChange: (code: string) => void;
  disabled?: boolean;
}) {
  return (
    <div role="radiogroup" aria-label="Conversation language" className="flex flex-wrap justify-center gap-2">
      {LANGUAGES.map((lang) => {
        const selected = lang.code === value;
        return (
          <button
            key={lang.code}
            type="button"
            role="radio"
            aria-checked={selected}
            lang={lang.code}
            disabled={disabled}
            onClick={() => onChange(lang.code)}
            className={cn(
              "rounded-full border px-3.5 py-1.5 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50",
              selected
                ? "border-foreground bg-foreground text-background"
                : "border-border bg-background text-foreground hover:bg-muted",
            )}
          >
            {lang.label}
          </button>
        );
      })}
    </div>
  );
}
