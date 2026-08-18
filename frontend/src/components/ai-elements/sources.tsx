import { BookOpenIcon, ChevronDownIcon } from "lucide-react";
import {
  type ButtonHTMLAttributes,
  type DetailsHTMLAttributes,
  type HTMLAttributes,
} from "react";

import { cn } from "@/lib/utils";

/** Product-adapted AI Elements Sources for internal RAG citations. */
export type SourcesProps = DetailsHTMLAttributes<HTMLDetailsElement>;

export function Sources({ className, ...props }: SourcesProps) {
  return <details className={cn("ai-sources", className)} {...props} />;
}

export type SourcesTriggerProps = HTMLAttributes<HTMLElement> & { count: number };

export function SourcesTrigger({ className, count, children, ...props }: SourcesTriggerProps) {
  return (
    <summary className={cn("ai-sources-trigger", className)} {...props}>
      {children ?? (
        <>
          <BookOpenIcon aria-hidden="true" size={14} />
          <span>{count} 个引用来源</span>
          <ChevronDownIcon aria-hidden="true" className="ai-sources-chevron" size={14} />
        </>
      )}
    </summary>
  );
}

export type SourcesContentProps = HTMLAttributes<HTMLDivElement>;

export function SourcesContent({ className, ...props }: SourcesContentProps) {
  return <div className={cn("ai-sources-content", className)} {...props} />;
}

export type SourceProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string;
  title: string;
};

export function Source({ className, label, title, children, ...props }: SourceProps) {
  return (
    <button className={cn("ai-source", className)} type="button" {...props}>
      {children ?? (
        <>
          <span>{label}</span>
          <strong>{title}</strong>
        </>
      )}
    </button>
  );
}
