import { useId, useState } from 'react';
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
  reviewFirst = false,
  note,
  onUseWholeQuestion,
  onRestoreSuggestions,
}: {
  parts: string[];
  confirmed: boolean;
  disabled: boolean;
  onChange: (parts: string[]) => void;
  onConfirm: (confirmed: boolean) => void;
  reviewFirst?: boolean;
  note?: string;
  onUseWholeQuestion?: () => void;
  onRestoreSuggestions?: () => void;
}) {
  const help = useId();
  const error = questionPartsError(parts);
  const [editing, setEditing] = useState(false);
  const showEditor = !reviewFirst || editing || !!error;
  return (
    <fieldset disabled={disabled} className="space-y-3" aria-describedby={help}>
      <legend className="text-sm font-medium">
        {reviewFirst ? 'What the answer should cover' : 'Question parts'}
      </legend>
      <p id={help} className="text-xs text-muted-foreground">
        {reviewFirst
          ? note
          : 'Edit what each part should answer. Use up to six parts, with 500 characters each.'}{' '}
        Confirming the parts does not approve an answer or guarantee complete coverage.
      </p>
      {!showEditor && (
        <ol className="list-decimal space-y-2 pl-5 text-sm">
          {parts.map((part, index) => (
            <li className="whitespace-pre-wrap break-words" key={index}>
              {part}
            </li>
          ))}
        </ol>
      )}
      {showEditor &&
        parts.map((part, index) => (
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
      {showEditor && (
        <button
          type="button"
          className={button}
          disabled={disabled || parts.length === 6}
          onClick={() => onChange([...parts, ''])}
        >
          Add question part
        </button>
      )}
      {reviewFirst && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className={button}
            disabled={showEditor && !!error}
            onClick={() => setEditing(!editing)}
          >
            {showEditor ? 'Done editing' : 'Edit question parts'}
          </button>
          {parts.length > 1 && onUseWholeQuestion && (
            <button
              type="button"
              className={button}
              onClick={() => {
                onUseWholeQuestion();
                setEditing(false);
              }}
            >
              Keep as one question
            </button>
          )}
          {onRestoreSuggestions && (
            <button
              type="button"
              className={button}
              onClick={() => {
                onRestoreSuggestions();
                setEditing(false);
              }}
            >
              Restore suggestions
            </button>
          )}
        </div>
      )}
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
