const MAX_SOURCE_CHARS = 200_000;
const MAX_MATH_CHARS = 10_000;
const MATH_DELIMITERS = Object.freeze([
  Object.freeze({ open: "$$", close: "$$", display: true }),
  Object.freeze({ open: "\\(", close: "\\)", display: false }),
  Object.freeze({ open: "$", close: "$", display: false }),
]);

function isEscaped(source, index) {
  let slashes = 0;
  for (let cursor = index - 1; cursor >= 0 && source[cursor] === "\\"; cursor--) {
    slashes += 1;
  }
  return slashes % 2 === 1;
}

function nextDelimiter(source, start) {
  for (let index = start; index < source.length; index++) {
    const delimiter = MATH_DELIMITERS.find(({ open }) =>
      source.startsWith(open, index)
    );
    if (delimiter && !isEscaped(source, index)) {
      return { index, ...delimiter };
    }
  }
  return null;
}

function closingDelimiter(source, start, delimiter) {
  for (let index = start; index < source.length; index++) {
    if (
      source.startsWith(delimiter.close, index) &&
      !isEscaped(source, index)
    ) return index;
  }
  return -1;
}

function appendText(segments, value) {
  if (!value) return;
  const previous = segments.at(-1);
  if (previous?.kind === "text") previous.value += value;
  else segments.push({ kind: "text", value });
}

export function splitMathSegments(source) {
  if (typeof source !== "string") throw new TypeError("Math text must be a string");
  if (source.length > MAX_SOURCE_CHARS) return [{ kind: "text", value: source }];

  const segments = [];
  let cursor = 0;
  while (cursor < source.length) {
    const opener = nextDelimiter(source, cursor);
    if (!opener) {
      appendText(segments, source.slice(cursor));
      break;
    }
    appendText(segments, source.slice(cursor, opener.index));
    const contentStart = opener.index + opener.open.length;
    const close = closingDelimiter(source, contentStart, opener);
    if (close < 0) {
      appendText(segments, source.slice(opener.index));
      break;
    }
    const end = close + opener.close.length;
    const value = source.slice(contentStart, close);
    segments.push({
      kind: "math",
      value,
      raw: source.slice(opener.index, end),
      display: opener.display,
    });
    cursor = end;
  }
  if (source.length === 0) return [{ kind: "text", value: "" }];
  return segments;
}

function renderMathSegments(
  container,
  source,
  katex,
  documentImpl,
  inlineOnly,
) {
  if (!container?.replaceChildren || !documentImpl?.createElement) {
    throw new TypeError("A DOM container and document are required");
  }
  if (!katex || typeof katex.render !== "function") {
    throw new TypeError("A KaTeX renderer is required");
  }
  container.replaceChildren();
  for (const segment of splitMathSegments(source)) {
    if (segment.kind === "text") {
      container.append(documentImpl.createTextNode(segment.value));
      continue;
    }
    const displayMode = segment.display && !inlineOnly;
    const output = documentImpl.createElement(displayMode ? "div" : "span");
    if (segment.value.length > MAX_MATH_CHARS) {
      output.textContent = segment.raw;
      container.append(output);
      continue;
    }
    try {
      katex.render(segment.value, output, {
        displayMode,
        throwOnError: true,
        trust: false,
        strict: "error",
        maxExpand: 1000,
        maxSize: 20,
        macros: Object.freeze({}),
      });
    } catch {
      output.textContent = segment.raw;
    }
    container.append(output);
  }
}

export function renderMathText(
  container,
  source,
  katex,
  documentImpl = globalThis.document,
) {
  renderMathSegments(container, source, katex, documentImpl, false);
}

export function renderInlineMathText(
  container,
  source,
  katex,
  documentImpl = globalThis.document,
) {
  renderMathSegments(container, source, katex, documentImpl, true);
}

export const MATH_RENDER_LIMITS = Object.freeze({
  sourceCharacters: MAX_SOURCE_CHARS,
  segmentCharacters: MAX_MATH_CHARS,
  maxExpand: 1000,
  maxSize: 20,
});
