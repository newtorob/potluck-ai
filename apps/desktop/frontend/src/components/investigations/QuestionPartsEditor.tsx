import { useId } from 'react';
import { investigationButton as button, investigationField as field } from './controls';

export function questionPartsError(parts: string[]): string {
  if (parts.length < 1 || parts.length > 6) return 'Use one to six question parts.';
  if (parts.some((part) => !part.trim())) return 'Fill in every question part.';
  if (parts.some((part) => part.length > 500)) return 'Use at most 500 characters per part.';
  const normalized = parts.map((part) => part.trim().replace(/\s+/g, ' ').toLowerCase());
  return new Set(normalized).size === parts.length ? '' : 'Use distinct question parts.';
}

export function QuestionPartsEditor({
  parts,
  confirmed,
  disabled,
  onChange,
  onConfirm,
}: {
  parts: string[];
  confirmed: boolean;
  disabled: boolean;
  onChange: (parts: string[]) => void;
  onConfirm: (confirmed: boolean) => void;
}) {
  const help = useId();
  const error = questionPartsError(parts);
  return (
    <fieldset disabled={disabled} className="space-y-3" aria-describedby={help}>
      <legend className="text-sm font-medium">Question parts</legend>
      <p id={help} className="text-xs text-muted-foreground">
        Edit what each part should answer. Use up to six parts, with 500 characters each. Confirming
        the parts does not approve an answer or guarantee complete coverage.
      </p>
      {parts.map((part, index) => (
        <div key={index} className="space-y-2">
          <label className="block text-sm">
            Part {index + 1}
            <textarea
              className={`${field} resize-none`}
              rows={2}
              maxLength={500}
              value={part}
              aria-invalid={!part.trim() || undefined}
              onChange={(event) =>
                onChange(parts.map((text, i) => (i === index ? event.target.value : text)))
              }
            />
          </label>
          <button
            type="button"
            className={button}
            disabled={disabled || parts.length === 1}
            aria-label={`Remove part ${index + 1}`}
            onClick={() => onChange(parts.filter((_, i) => i !== index))}
          >
            Remove part
          </button>
        </div>
      ))}
      <button
        type="button"
        className={button}
        disabled={disabled || parts.length === 6}
        onClick={() => onChange([...parts, ''])}
      >
        Add question part
      </button>
      <p className="min-h-5 text-xs text-muted-foreground" role="status">
        {error}
      </p>
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          checked={confirmed}
          disabled={disabled || !!error}
          onChange={(event) => onConfirm(event.target.checked)}
        />
        I reviewed these question parts.
      </label>
    </fieldset>
  );
}
