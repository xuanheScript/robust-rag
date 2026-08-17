import { memo, type ComponentProps } from "react";
import { Streamdown } from "streamdown";

export type MessageResponseProps = ComponentProps<typeof Streamdown>;

/**
 * Vite-local adaptation of AI Elements' MessageResponse source component.
 * Keeping it in the repository lets the product style and evolve the chat UI
 * without coupling application code to a framework-specific component loader.
 */
export const MessageResponse = memo(({ className = "", ...props }: MessageResponseProps) => (
  <Streamdown className={`message-content ${className}`.trim()} {...props} />
));

MessageResponse.displayName = "MessageResponse";
