import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BugIcon,
  CheckCircle2Icon,
  CircleIcon,
  CopyIcon,
  LoaderCircleIcon,
  RefreshCwIcon,
  SparklesIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import {
  Message,
  MessageAction,
  MessageActions,
  MessageContent,
  MessageResponse,
} from "@/components/ai-elements/message";
import {
  PromptInput,
  PromptInputBody,
  PromptInputFooter,
  type PromptInputMessage,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools,
} from "@/components/ai-elements/prompt-input";
import {
  Source,
  Sources,
  SourcesContent,
  SourcesTrigger,
} from "@/components/ai-elements/sources";
import { Suggestion, Suggestions } from "@/components/ai-elements/suggestion";
import { Shimmer } from "@/components/ai-elements/shimmer";
import {
  Task,
  TaskContent,
  TaskItem,
  TaskTrigger,
} from "@/components/ai-elements/task";
import { Loading, StatusBadge } from "@/components/ui";
import { formatDate } from "@/lib/format";
import {
  deleteConversation,
  getConversation,
  getMessageTrace,
  listConversations,
  streamChat,
  type ChatMessage,
  type Citation,
} from "@/lib/api";

export function ChatPage() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [input, setInput] = useState("");
  const [debugEnabled, setDebugEnabled] = useState(false);
  const [draftMessages, setDraftMessages] = useState<ChatMessage[]>([]);
  const [warning, setWarning] = useState<string | null>(null);
  const [agentActivity, setAgentActivity] = useState<AgentActivity | null>(null);
  const [activeCitation, setActiveCitation] = useState<Citation | null>(null);
  const [traceMessageId, setTraceMessageId] = useState<string | null>(null);
  const [copiedMessageId, setCopiedMessageId] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const conversations = useQuery({ queryKey: ["conversations"], queryFn: ({ signal }) => listConversations(signal) });
  const conversation = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: ({ signal }) => getConversation(conversationId ?? "", signal),
    enabled: Boolean(conversationId),
  });
  const trace = useQuery({
    queryKey: ["message-trace", traceMessageId],
    queryFn: ({ signal }) => getMessageTrace(traceMessageId ?? "", signal),
    enabled: Boolean(traceMessageId),
  });
  const deletion = useMutation({
    mutationFn: deleteConversation,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      await navigate("/chat");
    },
  });
  const [isStreaming, setIsStreaming] = useState(false);
  const messages = useMemo(
    () => [...(conversation.data?.messages ?? []), ...draftMessages],
    [conversation.data, draftMessages],
  );

  useEffect(() => {
    setDraftMessages([]);
    setWarning(null);
    setAgentActivity(null);
  }, [conversationId]);

  async function send(text: string) {
    const question = text.trim();
    if (!question || isStreaming) return;
    setInput("");
    setWarning(null);
    setAgentActivity({ phase: "deciding", action: null });
    setIsStreaming(true);
    const temporaryUserId = `user-${Date.now()}`;
    const temporaryAssistantId = `assistant-${Date.now()}`;
    setDraftMessages([
      { id: temporaryUserId, role: "user", status: "completed", content: question, query_original: question, query_rewritten: null, created_at: new Date().toISOString(), citations: [] },
      { id: temporaryAssistantId, role: "assistant", status: "streaming", content: "", query_original: question, query_rewritten: null, created_at: new Date().toISOString(), citations: [] },
    ]);
    const abortController = new AbortController();
    controller.current = abortController;
    let resolvedConversationId = conversationId;
    try {
      await streamChat(
        { conversationId, text: question, debug: debugEnabled },
        (event) => {
          if (event.type === "data-conversation") {
            const nextId = event.data?.conversation_id;
            if (typeof nextId === "string") {
              resolvedConversationId = nextId;
              if (!conversationId) void navigate(`/chat/${nextId}`, { replace: true });
            }
          }
          if (event.type === "start" && event.messageId) {
            setDraftMessages((current) => current.map((message) => message.role === "assistant" ? { ...message, id: event.messageId as string } : message));
          }
          if (event.type === "text-delta" && event.delta) {
            setAgentActivity(null);
            setDraftMessages((current) => current.map((message) => message.role === "assistant" ? { ...message, content: message.content + event.delta } : message));
          }
          if (event.type === "data-agent-status") {
            const action = event.data?.action;
            if (action === "direct") {
              setAgentActivity({ phase: "composing", action });
            }
            if (action === "documents" || action === "relationships") {
              setAgentActivity({ phase: "retrieving", action });
            }
          }
          if (event.type === "data-tool-status") {
            const tool = event.data?.tool;
            const action = tool === "retrieve_enterprise_relationships" ? "relationships" : "documents";
            setAgentActivity((current) => ({
              ...current,
              action,
              phase: event.data?.status === "completed" ? "composing" : "retrieving",
            }));
          }
          if (event.type === "data-retrieval-status") {
            const sourceCount = event.data?.source_count;
            setAgentActivity((current) => ({
              ...current,
              action: current?.action ?? "documents",
              phase: "composing",
              sourceCount: typeof sourceCount === "number" ? sourceCount : undefined,
            }));
          }
          if (event.type === "data-source" && event.data) {
            const citation = event.data as unknown as Citation;
            setDraftMessages((current) => current.map((message) => message.role === "assistant" ? { ...message, citations: [...message.citations, citation] } : message));
          }
          if (event.type === "data-warning") {
            const message = event.data?.message;
            if (typeof message === "string") setWarning(message);
          }
          if (event.type === "error") setWarning(event.errorText ?? "回答生成失败，请稍后重试。");
        },
        abortController.signal,
      );
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        resolvedConversationId
          ? queryClient.invalidateQueries({ queryKey: ["conversation", resolvedConversationId] })
          : Promise.resolve(),
      ]);
      setDraftMessages([]);
    } catch (error) {
      if ((error as Error).name !== "AbortError") setWarning((error as Error).message);
    } finally {
      setIsStreaming(false);
      setAgentActivity(null);
      controller.current = null;
    }
  }

  function submit(message: PromptInputMessage) {
    void send(message.text);
  }

  async function copyMessage(message: ChatMessage) {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopiedMessageId(message.id);
    } catch {
      setWarning("浏览器未允许复制，请手动选择回答文本。");
    }
  }

  const lastQuestion = [...messages].reverse().find((message) => message.role === "user")?.content;
  const lastAssistantId = [...messages].reverse().find((message) => message.role === "assistant")?.id;
  return (
    <div className="chat-layout">
      <aside className="conversation-sidebar">
        <button className="primary-button new-chat" onClick={() => void navigate("/chat")}>＋ 新建对话</button>
        <span className="list-label">最近对话</span>
        {conversations.isPending ? <Loading /> : conversations.data?.length ? (
          <div className="conversation-list">
            {conversations.data.map((item) => <button key={item.id} className={item.id === conversationId ? "active" : ""} onClick={() => void navigate(`/chat/${item.id}`)}><strong>{item.title || "未命名对话"}</strong><span>{formatDate(item.updated_at)}</span></button>)}
          </div>
        ) : <p className="conversation-empty">还没有历史对话</p>}
      </aside>
      <section className="chat-main">
        <header className="chat-header">
          <div><span>Grounded chat</span><h1>{conversation.data?.title || "知识库问答"}</h1></div>
          <div className="chat-tools">
            <label className="switch"><input type="checkbox" checked={debugEnabled} onChange={(event) => setDebugEnabled(event.target.checked)} /><span />调试视图</label>
            {conversationId ? <button className="icon-button" aria-label="删除对话" onClick={() => deletion.mutate(conversationId)}>×</button> : null}
          </div>
        </header>
        <Conversation className="message-feed">
          <ConversationContent className="message-feed-content">
          {conversation.isPending && conversationId ? <Loading label="正在加载对话" /> : messages.length === 0 ? (
            <ConversationEmptyState className="chat-empty">
              <span className="chat-orb"><SparklesIcon aria-hidden="true" size={22} /></span>
              <h2>从可信资料中找到答案</h2>
              <p>回答会严格基于当前可检索文档，并提供可展开的来源引用。</p>
              <Suggestions className="suggestions">
                {["概括知识库中的核心主题", "有哪些重要实体及其关系？", "比较两份文档中的关键差异"].map((value) => (
                  <Suggestion key={value} onClick={(suggestion) => void send(suggestion)} suggestion={value}>
                    {value}<span>↗</span>
                  </Suggestion>
                ))}
              </Suggestions>
            </ConversationEmptyState>
          ) : messages.map((message) => (
            <Message className={`message message-${message.role}`} from={message.role} key={message.id}>
              <MessageContent className="message-body">
                {message.status === "streaming" && !message.content ? (
                  message.id === lastAssistantId && agentActivity ? (
                    <AgentActivityView activity={agentActivity} />
                  ) : (
                    <div className="typing"><span /><span /><span /></div>
                  )
                ) : message.role === "assistant" ? (
                  <MessageResponse mode={message.status === "streaming" ? "streaming" : "static"}>{message.content}</MessageResponse>
                ) : (
                  <p className="message-content">{message.content}</p>
                )}
                {message.role === "assistant" && message.content ? (
                  <MessageActions className="message-actions">
                    <MessageAction label="复制回答" onClick={() => void copyMessage(message)}>
                      <CopyIcon aria-hidden="true" size={13} />
                      <span>{copiedMessageId === message.id ? "已复制" : "复制回答"}</span>
                    </MessageAction>
                    {message.id === lastAssistantId && lastQuestion && !isStreaming ? (
                      <MessageAction label="重新生成" onClick={() => void send(lastQuestion)}>
                        <RefreshCwIcon aria-hidden="true" size={13} />
                        <span>重新生成</span>
                      </MessageAction>
                    ) : null}
                    {debugEnabled && !message.id.startsWith("assistant-") ? (
                      <MessageAction label="查看检索与模型 Trace" onClick={() => setTraceMessageId(message.id)}>
                        <BugIcon aria-hidden="true" size={13} />
                        <span>查看 Trace</span>
                      </MessageAction>
                    ) : null}
                  </MessageActions>
                ) : null}
                {message.citations.length ? (
                  <Sources>
                    <SourcesTrigger count={message.citations.length} />
                    <SourcesContent>
                      {message.citations.map((source, index) => (
                        <Source
                          key={`${source.node_id}-${index}`}
                          label={source.source_label ?? source.label ?? `S${index + 1}`}
                          onClick={() => setActiveCitation(source)}
                          title={source.document_name}
                        />
                      ))}
                    </SourcesContent>
                  </Sources>
                ) : null}
              </MessageContent>
            </Message>
          ))}
          {warning ? <div className="chat-warning">{warning}</div> : null}
          </ConversationContent>
          <ConversationScrollButton />
        </Conversation>
        <PromptInput className="composer" onSubmit={submit}>
          <PromptInputBody>
            <PromptInputTextarea value={input} onChange={(event) => setInput(event.target.value)} />
          </PromptInputBody>
          <PromptInputFooter>
            <PromptInputTools><span>Enter 发送 · Shift + Enter 换行</span></PromptInputTools>
            <PromptInputSubmit
              disabled={!isStreaming && !input.trim()}
              onStop={() => controller.current?.abort()}
              status={isStreaming ? "streaming" : "ready"}
            />
          </PromptInputFooter>
        </PromptInput>
      </section>
      {activeCitation ? <aside className="source-drawer"><header><div><span>Source</span><h2>来源详情</h2></div><button className="icon-button" onClick={() => setActiveCitation(null)}>×</button></header><StatusBadge value="ready" /><h3>{activeCitation.document_name}</h3><p className="source-path">{activeCitation.heading_path.join(" / ") || "文档正文"}</p><blockquote>{activeCitation.excerpt}</blockquote><dl><dt>位置</dt><dd>{activeCitation.location || locatorText(activeCitation.source_locators_json ?? activeCitation.source_locators)}</dd><dt>Node ID</dt><dd className="mono">{activeCitation.node_id}</dd></dl></aside> : null}
      {traceMessageId ? <aside className="source-drawer debug-drawer"><header><div><span>Admin debug</span><h2>回答 Trace</h2></div><button className="icon-button" onClick={() => setTraceMessageId(null)}>×</button></header>{trace.isPending ? <Loading /> : trace.isError ? <div className="inline-error">Trace 读取失败</div> : <pre>{JSON.stringify(trace.data, null, 2)}</pre>}</aside> : null}
    </div>
  );
}

type AgentActivityAction = "direct" | "documents" | "relationships";
type AgentActivityPhase = "deciding" | "retrieving" | "composing";

interface AgentActivity {
  phase: AgentActivityPhase;
  action: AgentActivityAction | null;
  sourceCount?: number;
}

type AgentActivityStepState = "complete" | "active" | "pending";

function AgentActivityView({ activity }: { activity: AgentActivity }) {
  const retrievingLabel = activity.action === "relationships"
    ? "检索企业关系"
    : activity.action === "documents"
      ? "检索企业资料"
      : "按需查找企业资料";
  const composingLabel = activity.sourceCount === undefined
    ? "整理资料并生成回答"
    : `整理 ${activity.sourceCount} 条资料并生成回答`;
  const steps: Array<{ label: string; state: AgentActivityStepState }> = [
    {
      label: "理解问题",
      state: activity.phase === "deciding" ? "active" : "complete",
    },
    {
      label: activity.action === "direct" ? "无需检索企业资料" : retrievingLabel,
      state: activity.phase === "deciding"
        ? "pending"
        : activity.phase === "retrieving"
          ? "active"
          : "complete",
    },
    {
      label: composingLabel,
      state: activity.phase === "composing" ? "active" : "pending",
    },
  ];
  const title = activity.phase === "deciding"
    ? "正在理解问题"
    : activity.phase === "retrieving"
      ? "正在查找相关资料"
      : "正在整理回答";

  return (
    <Task className="chat-activity">
      <TaskTrigger title={title} />
      <TaskContent>
        {steps.map((step) => (
          <TaskItem className={`chat-activity-item is-${step.state}`} key={step.label}>
            {step.state === "complete" ? (
              <CheckCircle2Icon aria-hidden="true" size={14} />
            ) : step.state === "active" ? (
              <LoaderCircleIcon aria-hidden="true" className="ai-spin" size={14} />
            ) : (
              <CircleIcon aria-hidden="true" size={14} />
            )}
            {step.state === "active" ? (
              <Shimmer as="span">{step.label}</Shimmer>
            ) : (
              <span>{step.label}</span>
            )}
          </TaskItem>
        ))}
      </TaskContent>
    </Task>
  );
}

function locatorText(values?: Array<Record<string, unknown>>) {
  if (!values?.length) return "未提供具体位置";
  const first = values[0];
  if (typeof first.page_number === "number") return `第 ${first.page_number} 页`;
  if (typeof first.slide_number === "number") return `第 ${first.slide_number} 张幻灯片`;
  if (typeof first.sheet_name === "string") {
    const range = typeof first.cell_range === "string" ? ` ${first.cell_range}` : "";
    return `${first.sheet_name}${range}`;
  }
  return "文档正文";
}
