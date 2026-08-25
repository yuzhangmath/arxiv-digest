import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, readdir } from "node:fs/promises";
import { dirname, join, relative } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  renderMathText,
  splitMathSegments,
} from "../../src/arxiv_digest/web/static/math_view.mjs";
import { FakeDocument, FakeNode } from "./dom_test_helper.mjs";

test("mixed prose stays text while inline and display delimiters become math", () => {
  assert.deepEqual(
    splitMathSegments("Energy is $E=mc^2$ here. $$x+y$$ Done."),
    [
      { kind: "text", value: "Energy is " },
      { kind: "math", value: "E=mc^2", raw: "$E=mc^2$", display: false },
      { kind: "text", value: " here. " },
      { kind: "math", value: "x+y", raw: "$$x+y$$", display: true },
      { kind: "text", value: " Done." },
    ],
  );

  const document = new FakeDocument();
  const root = new FakeNode("div");
  const calls = [];
  renderMathText(root, "Energy is $E=mc^2$ in this model.", {
    render(value, output, options) {
      calls.push({ value, output, options });
      output.textContent = `rendered:${value}`;
    },
  }, document);

  assert.equal(root.children[0].nodeType, 3);
  assert.equal(root.children[0].textContent, "Energy is ");
  assert.equal(root.children[1].tagName, "SPAN");
  assert.equal(root.children[2].nodeType, 3);
  assert.equal(root.textContent, "Energy is rendered:E=mc^2 in this model.");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].value, "E=mc^2");
  assert.deepEqual(calls[0].options, {
    displayMode: false,
    throwOnError: true,
    trust: false,
    strict: "error",
    maxExpand: 1000,
    maxSize: 20,
    macros: {},
  });
  assert.equal(Object.isFrozen(calls[0].options.macros), true);
});

test("adjacent inline expressions are parsed independently", () => {
  assert.deepEqual(splitMathSegments("$x$$y$"), [
    { kind: "math", value: "x", raw: "$x$", display: false },
    { kind: "math", value: "y", raw: "$y$", display: false },
  ]);
});

test("parenthesized TeX delimiters become inline math", () => {
  assert.deepEqual(
    splitMathSegments(String.raw`Value \(x^2 + y^2\) here.`),
    [
      { kind: "text", value: "Value " },
      {
        kind: "math",
        value: "x^2 + y^2",
        raw: String.raw`\(x^2 + y^2\)`,
        display: false,
      },
      { kind: "text", value: " here." },
    ],
  );
});

test("the KaTeX manifest has the exact version and sorted hashes for every vendored byte", async () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const vendor = join(
    here,
    "../../src/arxiv_digest/web/static/vendor/katex",
  );
  const manifest = JSON.parse(
    await readFile(join(vendor, "katex-manifest.json"), "utf8"),
  );
  assert.equal(manifest.name, "KaTeX");
  assert.equal(manifest.version, "0.18.0");
  assert.equal(manifest.license, "MIT");
  const paths = manifest.files.map((entry) => entry.path);
  assert.deepEqual(paths, [...paths].sort());
  assert.equal(paths.includes("LICENSE"), true);
  assert.equal(paths.includes("katex.min.css"), true);
  assert.equal(paths.includes("katex.min.js"), true);
  assert.equal(paths.includes("auto-render.min.js"), true);
  assert.equal(paths.some((path) => path.startsWith("fonts/") && path.endsWith(".woff2")), true);
  assert.equal(paths.includes("README.md"), false);

  async function filesBelow(directory) {
    const found = [];
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) found.push(...await filesBelow(path));
      else if (entry.name !== "katex-manifest.json") found.push(relative(vendor, path));
    }
    return found.sort();
  }
  assert.deepEqual(paths, await filesBelow(vendor));
  for (const entry of manifest.files) {
    const digest = createHash("sha256")
      .update(await readFile(join(vendor, entry.path)))
      .digest("hex");
    assert.match(entry.sha256, /^[0-9a-f]{64}$/);
    assert.equal(entry.sha256, digest, entry.path);
  }
});

test("markup, unknown commands, unmatched delimiters, and oversized math fall back literally", () => {
  const document = new FakeDocument();
  const root = new FakeNode("div");
  let renderCalls = 0;
  renderMathText(
    root,
    "<a href='https://example.test'>paper</a> $\\unknown{x}$ unmatched $tail",
    {
      render() {
        renderCalls += 1;
        throw new Error("unknown command");
      },
    },
    document,
  );
  assert.equal(renderCalls, 1);
  assert.equal(root.querySelectorAll("a").length, 0);
  assert.equal(root.querySelectorAll("img").length, 0);
  assert.equal(root.querySelectorAll("script").length, 0);
  assert.equal(
    root.textContent,
    "<a href='https://example.test'>paper</a> $\\unknown{x}$ unmatched $tail",
  );

  const oversized = new FakeNode("div");
  renderMathText(
    oversized,
    `$${"x".repeat(10_001)}$`,
    {
      render() {
        throw new Error("oversized input must not reach KaTeX");
      },
    },
    document,
  );
  assert.equal(oversized.textContent, `$${"x".repeat(10_001)}$`);
});
