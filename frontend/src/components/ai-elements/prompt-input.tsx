import {
  CornerDownLeftIcon,
  LoaderCircleIcon,
  SquareIcon,
  XIcon,
} from "lucide-react";
import {
  type ButtonHTMLAttributes,
  type FormEvent,
  type FormHTMLAttributes,
  type HTMLAttributes,
  type KeyboardEventHandler,
  type TextareaHTMLAttributes,
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { cn } from "@/lib/utils";

export type PromptInputMessage = {
  text: string;
};

export type PromptInputProps = Omit<FormHTMLAttributes<HTMLFormElement>, "onSubmit"> & {
  onSubmit: (message: PromptInputMessage, event: FormEvent<HTMLFormElement>) => void | Promise<void>;
};

export function PromptInput({ className, onSubmit, ...props }: PromptInputProps) {
  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const value = new FormData(event.currentTarget).get("message");
      void onSubmit({ text: typeof value === "string" ? value : "" }, event);
    },
    [onSubmit],
  );

  return <form className={cn("ai-prompt-input", className)} onSubmit={handleSubmit} {...props} />;
}

export type PromptInputBodyProps = HTMLAttributes<HTMLDivElement>;

export function PromptInputBody({ className, ...props }: PromptInputBodyProps) {
  return <div className={cn("ai-prompt-input-body", className)} {...props} />;
}

export type PromptInputTextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement>;

export function PromptInputTextarea({
  className,
  onChange,
  onKeyDown,
  value,
  placeholder = "询问知识库中的内容…",
  ...props
}: PromptInputTextareaProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [isComposing, setIsComposing] = useState(false);

  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 140)}px`;
  }, [value]);

  const handleKeyDown: KeyboardEventHandler<HTMLTextAreaElement> = useCallback(
    (event) => {
      onKeyDown?.(event);
      if (event.defaultPrevented || event.key !== "Enter" || event.shiftKey) return;
      if (isComposing || event.nativeEvent.isComposing) return;
      event.preventDefault();
      const submit = event.currentTarget.form?.querySelector<HTMLButtonElement>(
        'button[type="submit"]',
      );
      if (!submit?.disabled) event.currentTarget.form?.requestSubmit();
    },
    [isComposing, onKeyDown],
  );

  return (
    <textarea
      className={cn("ai-prompt-input-textarea", className)}
      name="message"
      onChange={onChange}
      onCompositionEnd={() => setIsComposing(false)}
      onCompositionStart={() => setIsComposing(true)}
      onKeyDown={handleKeyDown}
      placeholder={placeholder}
      ref={textareaRef}
      rows={1}
      value={value}
      {...props}
    />
  );
}

export type PromptInputFooterProps = HTMLAttributes<HTMLDivElement>;

export function PromptInputFooter({ className, ...props }: PromptInputFooterProps) {
  return <div className={cn("ai-prompt-input-footer", className)} {...props} />;
}

export type PromptInputToolsProps = HTMLAttributes<HTMLDivElement>;

export function PromptInputTools({ className, ...props }: PromptInputToolsProps) {
  return <div className={cn("ai-prompt-input-tools", className)} {...props} />;
}

export type PromptInputStatus = "ready" | "submitted" | "streaming" | "error";

export type PromptInputSubmitProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  status?: PromptInputStatus;
  onStop?: () => void;
};

export function PromptInputSubmit({
  className,
  status = "ready",
  onStop,
  onClick,
  children,
  ...props
}: PromptInputSubmitProps) {
  const isGenerating = status === "submitted" || status === "streaming";
  const icon =
    status === "submitted" ? (
      <LoaderCircleIcon className="ai-spin" size={16} />
    ) : status === "streaming" ? (
      <SquareIcon size={14} />
    ) : status === "error" ? (
      <XIcon size={16} />
    ) : (
      <CornerDownLeftIcon size={16} />
    );

  return (
    <button
      aria-label={isGenerating ? "停止生成" : "发送消息"}
      className={cn("ai-prompt-input-submit", className)}
      onClick={(event) => {
        if (isGenerating && onStop) {
          event.preventDefault();
          onStop();
          return;
        }
        onClick?.(event);
      }}
      type={isGenerating && onStop ? "button" : "submit"}
      {...props}
    >
      {children ?? icon}
    </button>
  );
}
