import { type ButtonHTMLAttributes, type HTMLAttributes, useCallback } from "react";

import { cn } from "@/lib/utils";

export type SuggestionsProps = HTMLAttributes<HTMLDivElement>;

export function Suggestions({ className, ...props }: SuggestionsProps) {
  return <div className={cn("ai-suggestions", className)} {...props} />;
}

export type SuggestionProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onClick"> & {
  suggestion: string;
  onClick?: (suggestion: string) => void;
};

export function Suggestion({ suggestion, onClick, className, children, ...props }: SuggestionProps) {
  const handleClick = useCallback(() => onClick?.(suggestion), [onClick, suggestion]);
  return (
    <button
      className={cn("ai-suggestion", className)}
      onClick={handleClick}
      type="button"
      {...props}
    >
      {children ?? suggestion}
    </button>
  );
}
