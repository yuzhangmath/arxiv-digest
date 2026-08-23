import assert from "node:assert/strict";
import test from "node:test";

import {
  LibraryController,
  renderLibraryView,
} from "../../src/arxiv_digest/web/static/library_view.mjs";
import {
  FakeDocument,
  FakeNode,
  descendants,
  findButton,
} from "./dom_test_helper.mjs";

test("library search and pagination are delegated to the local API", async () => {
  const calls = [];
  const api = {
    async json(key, path, options) {
      calls.push({ key, path, options });
      return { entries: [], limit: 20, offset: 20, next_offset: null };
    },
  };
  const controller = new LibraryController(api);

  const page = await controller.search("  graph & groups  ", 20);

  assert.deepEqual(page, {
    entries: [],
    limit: 20,
    offset: 20,
    next_offset: null,
  });
  assert.deepEqual(calls, [
    {
      key: "library-search",
      path: "/api/v1/library?q=graph+%26+groups&offset=20",
      options: undefined,
    },
  ]);
});

test("remove and PDF retry use only stored paper and server job identifiers", async () => {
  const calls = [];
  const api = {
    async json(key, path, options) {
      calls.push({ key, path, options });
      if (path === "/api/v1/library/pdf") return { job_id: "download_job_123" };
      if (path.includes("/downloads/")) return { status: "failed", error_code: "offline" };
      return {};
    },
  };
  const controller = new LibraryController(api);

  await controller.remove("2608.01234");
  const job = await controller.download("2608.01234", 3);
  assert.deepEqual(await controller.downloadStatus(job.job_id), {
    status: "failed",
    error_code: "offline",
  });
  await controller.retryDownload(job.job_id);

  assert.equal(calls[0].path, "/api/v1/library/remove");
  assert.deepEqual(JSON.parse(calls[0].options.body), { arxiv_id: "2608.01234" });
  assert.equal(calls[1].path, "/api/v1/library/pdf");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    arxiv_id: "2608.01234",
    version: 3,
    save_first: false,
  });
  assert.equal(calls[2].path, "/api/v1/downloads/download_job_123");
  assert.equal(calls[3].path, "/api/v1/library/pdf");
  assert.deepEqual(JSON.parse(calls[3].options.body), {
    arxiv_id: "2608.01234",
    version: 3,
    save_first: false,
  });
  assert.equal(calls.every((call) => !call.path.includes("..")), true);
});

test("library separates tombstoned paper status from local PDF presence", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderLibraryView(
    new FakeDocument(),
    root,
    {
      query: "geometry",
      limit: 20,
      offset: 20,
      previous_offset: 0,
      next_offset: 40,
      entries: [
        {
          metadata: {
            arxiv_id: "2608.01234",
            title: "A <script>literal</script> title",
            authors: ["Ada Example"],
          },
          saved_version: 1,
          latest_version: 3,
          paper_available: false,
          local_pdf_versions: [1],
          new_version_available: true,
          download: { job_id: "download_job_123", status: "failed" },
        },
      ],
    },
    {
      search: (query) => calls.push(["search", query]),
      page: (offset) => calls.push(["page", offset]),
      remove: (...args) => calls.push(["remove", ...args]),
      retryPdf: (...args) => calls.push(["retry", ...args]),
    },
  );

  assert.match(root.textContent, /Saved v1/);
  assert.match(root.textContent, /Latest v3/);
  assert.match(root.textContent, /Paper unavailable from arXiv/);
  assert.match(root.textContent, /Local PDF available: v1/);
  assert.doesNotMatch(root.textContent, /New version available/);
  assert.equal(descendants(root, "script").length, 0);

  const input = descendants(root, "input")[0];
  input.value = "new query";
  findButton(root, "Search").click();
  findButton(root, "Previous page").click();
  findButton(root, "Next page").click();
  findButton(root, "Remove").click();
  assert.equal(descendants(root, "button").some((node) => node.textContent === "Retry PDF"), false);
  assert.equal(descendants(root, "button").some((node) => node.textContent === "Download PDF"), false);
  assert.deepEqual(calls, [
    ["search", "new query"],
    ["page", 0],
    ["page", 40],
    ["remove", "2608.01234"],
  ]);
});

test("library offers a PDF action only when the latest available version is not local", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderLibraryView(
    new FakeDocument(),
    root,
    {
      entries: [{
        metadata: { arxiv_id: "2608.01234", title: "Available", authors: [] },
        saved_version: 1,
        latest_version: 3,
        paper_available: true,
        local_pdf_versions: [1],
      }],
    },
    { downloadPdf: (...args) => calls.push(args) },
  );

  assert.match(root.textContent, /Local PDF available: v1/);
  findButton(root, "Download v3 PDF").click();
  assert.deepEqual(calls, [["2608.01234", 3]]);
});

test("library does not offer a duplicate download when the latest PDF is local", () => {
  const root = new FakeNode("main");
  renderLibraryView(new FakeDocument(), root, {
    entries: [{
      metadata: { arxiv_id: "2608.01234", title: "Already local", authors: [] },
      saved_version: 1,
      latest_version: 3,
      paper_available: true,
      local_pdf_versions: [1, 3],
    }],
  });

  assert.match(root.textContent, /Local PDF available: v1, v3/);
  assert.equal(
    descendants(root, "button").some((node) => node.textContent.includes("Download")),
    false,
  );
});

test("completed download status immediately updates the rendered local presence", () => {
  const root = new FakeNode("main");
  renderLibraryView(new FakeDocument(), root, {
    entries: [{
      metadata: { arxiv_id: "2608.01234", title: "Just downloaded", authors: [] },
      saved_version: 1,
      latest_version: 3,
      paper_available: true,
      local_pdf_versions: [],
      download: {
        job_id: "download_job_123",
        status: "completed",
        value: { version: 3 },
      },
    }],
  });

  assert.match(root.textContent, /Local PDF available: v3/);
  assert.doesNotMatch(root.textContent, /No local PDF/);
});
