import React, { useState, useRef, useEffect, Component } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Send, Bot, Loader2, BrainCircuit, ChevronDown, ChevronRight, X, Sparkles } from 'lucide-react';

class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }
  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }
  render() {
    if (this.state.hasError) {
      return <div className="text-rose-500 text-xs p-2">Markdown Error: {this.state.error.message}</div>;
    }
    return this.props.children;
  }
}

export default function ChatPanel({ isOpen, onClose }) {
  const [messages, setMessages] = useState([
    {
      role: 'model',
      content: "Hi! I am Botsensai AI. Ask me about active holdings, our +12.55% $FIGHT runner, or our 4-tier moonbag ladder targets.",
      thoughts: '',
      suggestions: [
        "What is the current PnL of our $FIGHT position?",
        "When will the next 1.40x take-profit rung trigger?",
        "How much SOL buffer remains above the 0.010 SOL floor?"
      ]
    }
  ]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [expandedThoughts, setExpandedThoughts] = useState({});
  const bottomRef = useRef(null);
  const textareaRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isLoading]);

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      const scrollHeight = textareaRef.current.scrollHeight;
      textareaRef.current.style.height = Math.min(scrollHeight, 112) + 'px';
      textareaRef.current.style.overflowY = scrollHeight > 112 ? 'auto' : 'hidden';
    }
  }, [input]);

  const toggleThought = (idx) => {
    setExpandedThoughts(prev => ({ ...prev, [idx]: !prev[idx] }));
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend(e);
    }
  };

  const handleSend = async (e, overrideText = null) => {
    e?.preventDefault();
    const textToSend = overrideText || input;
    if (!textToSend.trim() || isLoading) return;

    if (!overrideText) setInput('');
    setIsLoading(true);

    const currentMessages = [...messages, { role: 'user', content: textToSend.trim() }];
    const nextIdx = currentMessages.length;
    let thinking = '';
    let reply = '';
    let suggestions = [];

    setMessages([...currentMessages, { role: 'model', content: '', thoughts: '', suggestions: [] }]);

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: textToSend.trim(),
          history: currentMessages.slice(0, -1)
        })
      });

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        setIsLoading(false);
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const dataStr = line.replace('data: ', '').trim();
            if (dataStr === '[DONE]') break;
            if (dataStr) {
              try {
                const parsed = JSON.parse(dataStr);
                if (parsed.type === 'THOUGHT') {
                  thinking += parsed.content + '\n';
                } else if (parsed.type === 'SUGGESTION') {
                  suggestions.push(parsed.content);
                } else {
                  reply += parsed.content;
                }

                setMessages(prev => {
                  const updated = [...prev];
                  updated[nextIdx] = {
                    role: 'model',
                    content: reply,
                    thoughts: thinking,
                    suggestions: suggestions
                  };
                  return updated;
                });
              } catch (e) {
                console.error('SSE JSON parse error:', dataStr);
              }
            }
          }
        }
      }
    } catch (error) {
      console.error(error);
      setMessages([...currentMessages, { role: 'model', content: `⚠️ Error communicating with Gemini API.` }]);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className={`fixed top-0 right-0 h-full w-[420px] bg-white dark:bg-[#0c0c0f] border-l border-zinc-200 dark:border-zinc-800 shadow-2xl flex flex-col z-50 transition-transform duration-300 ${isOpen ? 'translate-x-0' : 'translate-x-full'}`}>
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-zinc-200 dark:border-zinc-800">
        <div className="flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-blue-500" />
          <div className="font-semibold text-sm text-zinc-950 dark:text-zinc-50">Gemini Trading Intelligence</div>
        </div>
        <button onClick={onClose} className="p-1 rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-200">
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-4">
        {messages.map((m, i) => (
          <div key={i} className={`flex gap-3 ${m.role === 'user' ? 'flex-row-reverse' : 'flex-row'}`}>
            <div className="flex-shrink-0 w-7 h-7 rounded-full bg-zinc-100 dark:bg-zinc-800 flex items-center justify-center text-xs font-semibold">
              {m.role === 'user' ? 'U' : <Bot className="w-3.5 h-3.5 text-blue-500" />}
            </div>

            <div className="max-w-[85%] flex flex-col gap-2">
              {/* 1. Thoughts Box */}
              {m.role === 'model' && m.thoughts && m.thoughts.trim().length > 0 && (
                <div className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm">
                  <button
                    onClick={() => toggleThought(i)}
                    className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-200 hover:bg-zinc-100 dark:hover:bg-zinc-800/60"
                  >
                    {expandedThoughts[i] ? <ChevronDown className="w-3.5 h-3.5 flex-shrink-0" /> : <ChevronRight className="w-3.5 h-3.5 flex-shrink-0" />}
                    <BrainCircuit className="w-3.5 h-3.5 text-blue-500 flex-shrink-0" />
                    <span className="truncate text-left text-[11px]">
                      {m.content && m.content.length > 0 ? "View reasoning process" : m.thoughts.split('\n').filter(l => l.trim()).pop() || "Thinking..."}
                    </span>
                  </button>
                  {expandedThoughts[i] && (
                    <div className="px-3 pb-3 pt-2 border-t border-zinc-200 dark:border-zinc-800 max-h-[180px] overflow-y-auto font-mono text-[11px] text-zinc-500 dark:text-zinc-400 flex flex-col gap-1.5">
                      {m.thoughts.split('\n').filter(l => l.trim()).map((line, idx) => (
                        <div key={idx} className="flex gap-2 items-start">
                          <span className="text-zinc-400">›</span>
                          <span className="break-words">{line}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* 2. Loading State */}
              {m.role === 'model' && !m.content && !expandedThoughts[i] && (
                <div className="flex items-center gap-2 text-xs text-zinc-500 italic p-1">
                  <Loader2 className="w-3 h-3 animate-spin text-blue-500" /> Analyzing...
                </div>
              )}

              {/* 3. Final Content */}
              {m.content && (
                <div className={`p-3 rounded-2xl text-xs leading-relaxed ${m.role === 'user' ? 'bg-blue-600 text-white rounded-tr-none' : 'bg-zinc-100 dark:bg-zinc-900 text-zinc-900 dark:text-zinc-100 rounded-tl-none border border-zinc-200 dark:border-zinc-800 shadow-sm'}`}>
                  {m.role === 'model' ? (
                    <ErrorBoundary>
                      <div className="[&>p]:mb-2 [&>p:last-child]:mb-0 [&>ul]:list-disc [&>ul]:ml-4 [&>h3]:font-bold [&>h3]:text-zinc-900 dark:[&>h3]:text-zinc-100 [&>h3]:mb-1 [&>h3]:mt-2 [&_code]:bg-black/10 dark:[&_code]:bg-white/10 [&_code]:px-1 [&_code]:rounded font-sans">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {m.content}
                        </ReactMarkdown>
                      </div>
                    </ErrorBoundary>
                  ) : (
                    <div className="whitespace-pre-wrap">{m.content}</div>
                  )}
                </div>
              )}

              {/* 4. Suggestions */}
              {m.role === 'model' && m.suggestions && m.suggestions.length > 0 && (
                <div className="flex flex-col gap-1 mt-1">
                  {m.suggestions.slice(0, 3).map((s, idx) => (
                    <button
                      key={idx}
                      onClick={() => handleSend(null, s)}
                      disabled={isLoading}
                      className="text-left text-[11px] bg-blue-50 dark:bg-blue-950/40 hover:bg-blue-100 dark:hover:bg-blue-900/60 text-blue-700 dark:text-blue-300 p-2 rounded-lg border border-blue-200 dark:border-blue-800/40 transition-colors"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      {/* Input Form */}
      <form onSubmit={handleSend} className="p-4 border-t border-zinc-200 dark:border-zinc-800 flex items-end bg-zinc-50 dark:bg-[#09090b] relative">
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={isLoading}
          placeholder="Ask Gemini about live trades or ladder targets..."
          rows={1}
          className="w-full bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-xl pl-4 pr-12 py-3 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500 text-zinc-900 dark:text-zinc-100 placeholder-zinc-400 resize-none"
        />
        <button
          type="submit"
          disabled={!input.trim() || isLoading}
          className="absolute right-6 bottom-6 p-2 bg-blue-600 hover:bg-blue-700 rounded-full text-white transition-colors disabled:opacity-40"
        >
          <Send className="w-3.5 h-3.5" />
        </button>
      </form>
    </div>
  );
}
