// Uses assistant-ui's official PrismLight adapter with bundled common languages.
// Nothing is loaded from a CDN while a model response is rendered.
import PrismLight from "react-syntax-highlighter/dist/esm/prism-light";
import { makeMakeSyntaxHighlighter } from "./make-syntax-highlighter";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";
import coldarkCold from "react-syntax-highlighter/dist/esm/styles/prism/coldark-cold";

for (const [name, syntax] of Object.entries({ python, py: python, js: tsx, javascript: tsx, jsx: tsx,
  ts: tsx, typescript: tsx, tsx, bash, sh: bash, shell: bash, json, css, sql, html: markup, xml: markup })) {
  PrismLight.registerLanguage(name, syntax);
}

export const SyntaxHighlighter = makeMakeSyntaxHighlighter(PrismLight)({
  style: coldarkCold,
  customStyle: { background: "transparent", margin: 0, width: "100%", padding: 0 },
});
