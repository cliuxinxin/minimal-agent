"use client";

import "@assistant-ui/react-markdown/styles/dot.css";

import {
  type CodeHeaderProps,
  MarkdownTextPrimitive,
  type MarkdownTextPrimitiveProps,
  memoizeMarkdownComponents,
  useIsMarkdownCodeBlock,
} from "@assistant-ui/react-markdown";
import remarkGfm from "remark-gfm";
import { type FC, memo, useMemo, useRef } from "react";
import { useAuiState, useMessagePartText, type TextMessagePartProps } from "@assistant-ui/react";
import remend from "remend";
import { SyntaxHighlighter } from "./syntax-highlighter";
import { CheckIcon, CopyIcon } from "lucide-react";

import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button";
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard";
import { cn } from "@/lib/utils";
import { sourceReference, isSourceIdentifier, type Source } from "@/workbench-data";

type MarkdownTextProps = Partial<TextMessagePartProps> & {
  components?: Parameters<typeof memoizeMarkdownComponents>[0];
  componentsByLanguage?: MarkdownTextPrimitiveProps["componentsByLanguage"];
};

const useShallowStable = <T extends Record<string, unknown> | undefined>(
  value: T,
): T => {
  const ref = useRef(value);
  if (value !== ref.current) {
    const prev = ref.current;
    const stable =
      value !== undefined &&
      prev !== undefined &&
      Object.keys(prev).length === Object.keys(value).length &&
      Object.keys(value).every((key) => prev[key] === value[key]);
    if (!stable) ref.current = value;
  }
  return ref.current;
};

const MarkdownTextImpl: FC<MarkdownTextProps> = ({
  components,
  componentsByLanguage,
}) => {
  const part = useMessagePartText();
  const streaming = part.status.type === "running";
  const stableComponents = useShallowStable(components);
  const stableComponentsByLanguage = useShallowStable(componentsByLanguage);
  const markdownComponents = useMemo(() => {
    if (!stableComponents) return defaultComponents;
    return {
      ...defaultComponents,
      ...memoizeMarkdownComponents(stableComponents),
    };
  }, [stableComponents]);

  return (
    <MarkdownTextPrimitive
      remarkPlugins={[remarkGfm]}
      className="aui-md markdown"
      smooth={false}
      preprocess={streaming ? repairPartialMarkdown : undefined}
      urlTransform={safeMarkdownUrl}
      components={markdownComponents}
      componentsByLanguage={stableComponentsByLanguage}
      defer
    />
  );
};

const repairPartialMarkdown = (text: string) => remend(text, { katex: false, inlineKatex: false });
const safeMarkdownUrl = (url: string, key: string) => key !== "src" && /^https?:\/\//i.test(url) ? url : "";

export const MarkdownText = memo(MarkdownTextImpl);

const CodeHeader: FC<CodeHeaderProps> = ({ language, code }) => {
  const { isCopied, copyToClipboard } = useCopyToClipboard();
  const onCopy = () => {
    if (!code || isCopied) return;
    copyToClipboard(code);
  };

  return (
    <div className="aui-code-header-root border-border/50 bg-muted/50 mt-3 flex items-center justify-between rounded-t-xl border border-b-0 px-3.5 py-1.5 text-xs">
      <span className="aui-code-header-language text-muted-foreground font-medium lowercase">
        {language}
      </span>
      <TooltipIconButton tooltip="复制代码" onClick={onCopy} className="copy-code">
        {!isCopied && (
          <CopyIcon className="animate-in zoom-in-75 fade-in duration-150" />
        )}
        {isCopied && (
          <CheckIcon className="animate-in zoom-in-50 fade-in duration-200 ease-out" />
        )}
      </TooltipIconButton>
    </div>
  );
};

function SourceLink({ className, href, children, ...props }: React.ComponentProps<"a">) {
  const sources = useAuiState((s) => s.message.metadata.custom.sources) as Source[] | undefined;
  const label = typeof children === "string" ? children : Array.isArray(children) && children.every((child) => typeof child === "string") ? children.join("") : "";
  const reference = sourceReference(label, href, sources || []);
  const unknown = !reference && isSourceIdentifier(label);
  const tooltip = reference ? `${reference.number}. ${reference.source.title} · ${new URL(reference.source.url).hostname}`
    : unknown ? "这条引用未匹配到本次实际来源，请核对原文。" : undefined;
  return href ? <a {...props} className={cn("aui-md-a", reference ? "source-citation" : unknown ? "source-citation unverified" : "text-primary underline underline-offset-2", className)}
    href={href} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer"
    title={tooltip || props.title} aria-label={tooltip || props["aria-label"]} data-source-number={reference?.number}
    data-source-id={reference?.source.id}>{reference ? `[${reference.number}]` : unknown ? "[来源待核对]" : children}</a> : <span>{children}</span>;
}

const defaultComponents = memoizeMarkdownComponents({
  h1: ({ className, ...props }) => (
    <h1
      className={cn(
        "aui-md-h1 mt-5 mb-2 scroll-m-20 text-xl font-semibold first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  h2: ({ className, ...props }) => (
    <h2
      className={cn(
        "aui-md-h2 mt-5 mb-2 scroll-m-20 text-lg font-semibold first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  h3: ({ className, ...props }) => (
    <h3
      className={cn(
        "aui-md-h3 mt-4 mb-1.5 scroll-m-20 text-base font-semibold first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  h4: ({ className, ...props }) => (
    <h4
      className={cn(
        "aui-md-h4 mt-3.5 mb-1 scroll-m-20 text-base font-medium first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  h5: ({ className, ...props }) => (
    <h5
      className={cn(
        "aui-md-h5 mt-3 mb-1 text-sm font-semibold first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  h6: ({ className, ...props }) => (
    <h6
      className={cn(
        "aui-md-h6 mt-3 mb-1 text-sm font-medium first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  p: ({ className, ...props }) => (
    <p
      className={cn(
        "aui-md-p my-3 leading-relaxed first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  a: SourceLink,
  img: ({ alt }) => <span className="image-reference">[图片：{alt || "图片"}]</span>,
  blockquote: ({ className, ...props }) => (
    <blockquote
      className={cn(
        "aui-md-blockquote border-muted-foreground/30 text-muted-foreground my-3 border-s-2 ps-4",
        className,
      )}
      {...props}
    />
  ),
  ul: ({ className, ...props }) => (
    <ul
      className={cn(
        "aui-md-ul marker:text-muted-foreground my-3 ms-5 list-disc [&>li]:mt-1",
        className,
      )}
      {...props}
    />
  ),
  ol: ({ className, ...props }) => (
    <ol
      className={cn(
        "aui-md-ol marker:text-muted-foreground my-3 ms-5 list-decimal [&>li]:mt-1",
        className,
      )}
      {...props}
    />
  ),
  hr: ({ className, ...props }) => (
    <hr
      className={cn("aui-md-hr border-muted-foreground/20 my-3", className)}
      {...props}
    />
  ),
  table: ({ className, ...props }) => (
    <div className="aui-md-table-wrapper my-3 overflow-x-auto">
      <table
        className={cn(
          "aui-md-table w-full border-separate border-spacing-0",
          className,
        )}
        {...props}
      />
    </div>
  ),
  th: ({ className, ...props }) => (
    <th
      className={cn(
        "aui-md-th bg-muted px-3 py-1.5 text-start font-medium first:rounded-ss-lg last:rounded-se-lg [[align=center]]:text-center [[align=right]]:text-right",
        className,
      )}
      {...props}
    />
  ),
  td: ({ className, ...props }) => (
    <td
      className={cn(
        "aui-md-td border-muted-foreground/20 border-s border-b px-3 py-1.5 text-start last:border-e [[align=center]]:text-center [[align=right]]:text-right",
        className,
      )}
      {...props}
    />
  ),
  tr: ({ className, ...props }) => (
    <tr
      className={cn(
        "aui-md-tr m-0 border-b p-0 first:border-t [&:last-child>td:first-child]:rounded-es-lg [&:last-child>td:last-child]:rounded-ee-lg",
        className,
      )}
      {...props}
    />
  ),
  li: ({ className, ...props }) => (
    <li className={cn("aui-md-li leading-relaxed", className)} {...props} />
  ),
  strong: ({ className, ...props }) => (
    <strong
      className={cn("aui-md-strong font-semibold", className)}
      {...props}
    />
  ),
  sup: ({ className, ...props }) => (
    <sup
      className={cn("aui-md-sup [&>a]:text-xs [&>a]:no-underline", className)}
      {...props}
    />
  ),
  pre: ({ className, ...props }) => (
    <pre
      className={cn(
        "aui-md-pre border-border/50 bg-muted/30 overflow-x-auto rounded-t-none rounded-b-xl border border-t-0 p-3.5 text-[13px] leading-relaxed",
        className,
      )}
      {...props}
    />
  ),
  code: function Code({ className, ...props }) {
    const isCodeBlock = useIsMarkdownCodeBlock();
    return (
      <code
        className={cn(
          !isCodeBlock &&
            "aui-md-inline-code bg-muted rounded-md px-1.5 py-0.5 font-mono text-[0.85em]",
          className,
        )}
        {...props}
      />
    );
  },
  CodeHeader,
  SyntaxHighlighter,
});
