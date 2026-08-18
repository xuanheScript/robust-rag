import {
  memo,
  type ButtonHTMLAttributes,
  type ComponentProps,
  type HTMLAttributes,
  type ReactNode,
} from "react";
import { Streamdown } from "streamdown";

import { cn } from "@/lib/utils";

export type MessageProps = HTMLAttributes<HTMLDivElement> & {
  from: "user" | "assistant" | "system";
};

export function Message({ className, from, ...props }: MessageProps) {
  return (
    <div
      className={cn("ai-message", `ai-message-${from}`, className)}
      data-role={from}
      {...props}
    />
  );
}

export type MessageContentProps = HTMLAttributes<HTMLDivElement>;

export function MessageContent({ className, ...props }: MessageContentProps) {
  return <div className={cn("ai-message-content", className)} {...props} />;
}

export type MessageActionsProps = HTMLAttributes<HTMLDivElement>;

export function MessageActions({ className, ...props }: MessageActionsProps) {
  return <div className={cn("ai-message-actions", className)} {...props} />;
}

export type MessageActionProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string;
  tooltip?: string;
  children?: ReactNode;
};

export function MessageAction({ label, tooltip, className, children, ...props }: MessageActionProps) {
  return (
    <button
      aria-label={label}
      className={cn("ai-message-action", className)}
      title={tooltip ?? label}
      type="button"
      {...props}
    >
      {children}
    </button>
  );
}

export type MessageResponseProps = ComponentProps<typeof Streamdown>;

/**
 * Vite-local adaptation of AI Elements' MessageResponse source component.
 * Keeping it in the repository lets the product style and evolve the chat UI
 * without coupling application code to a framework-specific component loader.
 */
export const MessageResponse = memo(
  ({ className, ...props }: MessageResponseProps) => (
    <Streamdown className={cn("message-content", className)} {...props} />
  ),
  (previous, next) =>
    previous.children === next.children && previous.isAnimating === next.isAnimating,
);

MessageResponse.displayName = "MessageResponse";
