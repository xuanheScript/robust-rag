import { ArrowDownIcon } from "lucide-react";
import { type ComponentProps, type ReactNode, useCallback } from "react";
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom";

import { cn } from "@/lib/utils";

/** Vite-local adaptation of AI Elements' Conversation component. */
export type ConversationProps = ComponentProps<typeof StickToBottom>;

export function Conversation({ className, ...props }: ConversationProps) {
  return (
    <StickToBottom
      className={cn("ai-conversation", className)}
      initial="smooth"
      resize="smooth"
      role="log"
      {...props}
    />
  );
}

export type ConversationContentProps = ComponentProps<typeof StickToBottom.Content>;

export function ConversationContent({ className, ...props }: ConversationContentProps) {
  return <StickToBottom.Content className={cn("ai-conversation-content", className)} {...props} />;
}

export type ConversationEmptyStateProps = ComponentProps<"div"> & {
  title?: string;
  description?: string;
  icon?: ReactNode;
};

export function ConversationEmptyState({
  className,
  title = "还没有消息",
  description = "在下方输入问题开始对话",
  icon,
  children,
  ...props
}: ConversationEmptyStateProps) {
  return (
    <div className={cn("ai-conversation-empty", className)} {...props}>
      {children ?? (
        <>
          {icon ? <div className="ai-conversation-empty-icon">{icon}</div> : null}
          <div>
            <h3>{title}</h3>
            {description ? <p>{description}</p> : null}
          </div>
        </>
      )}
    </div>
  );
}

export type ConversationScrollButtonProps = ComponentProps<"button">;

export function ConversationScrollButton({ className, ...props }: ConversationScrollButtonProps) {
  const { isAtBottom, scrollToBottom } = useStickToBottomContext();
  const handleScrollToBottom = useCallback(() => {
    void scrollToBottom();
  }, [scrollToBottom]);

  if (isAtBottom) return null;

  return (
    <button
      aria-label="回到底部"
      className={cn("ai-conversation-scroll-button", className)}
      onClick={handleScrollToBottom}
      type="button"
      {...props}
    >
      <ArrowDownIcon aria-hidden="true" size={16} />
    </button>
  );
}
