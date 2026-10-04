// Renders the cited PDF page with PDF.js and highlights the quoted passage with mark.js.
// Progressive enhancement: the claim and the quote are already on the page as text, so a
// failure here costs the picture, never the citation.

const root = document.getElementById("viewer");
const pageEl = document.getElementById("pdf-page");
const statusEl = document.getElementById("viewer-status");
const { pdfUrl, quote, workerSrc, workerSri } = root.dataset;
const pageNumber = Number(root.dataset.page);

const MAX_SCALE = 1.6; // wider than this a desktop window only shows a blurrier, bigger page
const MAX_CANVAS_PIXELS = 16e6; // iOS Safari refuses to draw larger canvases
const PREFIX_WORDS = 8;

const CHAR_CLASSES = [
  [/['‘’‛′`´]/, "['‘’‛′`´]"],
  [/["“”„‟″]/, '["“”„‟″]'],
  [/[-‐‑‒–—―−]/, "[-‐‑‒–—―−]"],
];

function setStatus(text, kind = "info") {
  statusEl.textContent = text;
  statusEl.dataset.kind = kind;
}

async function workerUrl() {
  // Browsers refuse cross-origin worker scripts. Fetching it ourselves with `integrity` gives the
  // worker the same SRI guarantee as the <script> tags; it then runs from a same-origin blob: URL.
  const res = await fetch(workerSrc, { integrity: workerSri, credentials: "omit" });
  if (!res.ok) throw new Error(`PDF.js worker: HTTP ${res.status}`);
  return URL.createObjectURL(new Blob([await res.arrayBuffer()], { type: "text/javascript" }));
}

async function openPage(pdfjsLib) {
  pdfjsLib.GlobalWorkerOptions.workerSrc = await workerUrl();
  const pdf = await pdfjsLib.getDocument({
    url: pdfUrl,
    isEvalSupported: false, // regulator filings are third-party PDFs: no eval'd font programs
    enableXfa: false,
    disableAutoFetch: true, // with range requests a phone downloads little beyond this page
    disableStream: true,
  }).promise;
  return pdf.getPage(pageNumber);
}

async function render(pdfjsLib, page) {
  const unscaled = page.getViewport({ scale: 1 });
  const scale = Math.min(pageEl.parentElement.clientWidth / unscaled.width, MAX_SCALE);
  const viewport = page.getViewport({ scale });
  const ratio = Math.min(
    window.devicePixelRatio || 1,
    Math.sqrt(MAX_CANVAS_PIXELS / (viewport.width * viewport.height)),
  );
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(viewport.width * ratio);
  canvas.height = Math.floor(viewport.height * ratio);
  canvas.style.width = `${Math.floor(viewport.width)}px`;
  canvas.style.height = `${Math.floor(viewport.height)}px`;
  const textLayer = document.createElement("div");
  textLayer.className = "textLayer";

  pageEl.style.setProperty("--scale-factor", String(scale));
  pageEl.style.width = canvas.style.width;
  pageEl.style.height = canvas.style.height;
  pageEl.replaceChildren(canvas, textLayer);

  await page.render({
    canvasContext: canvas.getContext("2d"),
    viewport,
    transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0],
  }).promise;
  await new pdfjsLib.TextLayer({
    textContentSource: page.streamTextContent(),
    container: textLayer,
    viewport,
  }).render();
  return textLayer;
}

function charPattern(ch) {
  for (const [test, cls] of CHAR_CLASSES) if (test.test(ch)) return cls;
  return ch.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
}

// Words joined by optional whitespace: PDF.js often emits no space at line ends, while the stored
// quote keeps PyMuPDF's line breaks (and any "con-\nstruction" hyphen, which matches as is).
function passageRegExp(text, maxWords = Infinity) {
  const words = text.normalize("NFKC").split(/\s+/).filter(Boolean).slice(0, maxWords);
  return new RegExp(words.map((w) => Array.from(w, charPattern).join("")).join("\\s*"), "gi");
}

function markWith(marker, method, pattern, options) {
  return new Promise((resolve) => {
    marker[method](pattern, { ...options, acrossElements: true, className: "cite-hl", done: resolve });
  });
}

// Most faithful first; the last attempt only finds the start, which still shows where to read.
async function highlight(textLayer) {
  const marker = new window.Mark(textLayer);
  const attempts = [
    ["markRegExp", passageRegExp(quote), {}, "full"],
    ["mark", quote, { separateWordSearch: false, accuracy: "complementary", ignoreJoiners: true }, "full"],
    ["markRegExp", passageRegExp(quote, PREFIX_WORDS), {}, "start"],
  ];
  for (const [method, pattern, options, extent] of attempts) {
    if ((await markWith(marker, method, pattern, options)) > 0) return extent;
  }
  return null;
}

function drawPassageBox() {
  const origin = pageEl.getBoundingClientRect();
  const rects = [...pageEl.querySelectorAll("mark.cite-hl")]
    .flatMap((m) => [...m.getClientRects()])
    .filter((r) => r.width > 0 && r.height > 0);
  if (!rects.length) return null;
  const pad = 4;
  const left = Math.min(...rects.map((r) => r.left)) - origin.left - pad;
  const top = Math.min(...rects.map((r) => r.top)) - origin.top - pad;
  const right = Math.max(...rects.map((r) => r.right)) - origin.left + pad;
  const bottom = Math.max(...rects.map((r) => r.bottom)) - origin.top + pad;
  const box = document.createElement("div");
  box.className = "passage-box";
  box.style.left = `${left}px`;
  box.style.top = `${top}px`;
  box.style.width = `${right - left}px`;
  box.style.height = `${bottom - top}px`;
  pageEl.append(box);
  return box;
}

function reportExtent(extent) {
  if (extent === "full") setStatus(`Passage highlighted on page ${pageNumber}.`);
  else if (extent === "start") setStatus(`Highlighted where the passage starts on page ${pageNumber}; the full quote is above.`);
  else setStatus(`Showing page ${pageNumber}. The passage couldn't be marked on the image; the quote above is from this page.`);
}

function fail(err) {
  console.error(err);
  setStatus("Couldn't display the page here. The quoted passage is above, and the PDF opens with the button.", "error");
}

async function show() {
  const { pdfjsLib, Mark } = window;
  if (!pdfjsLib || !Mark) throw new Error("viewer libraries did not load");
  const page = await openPage(pdfjsLib);
  let drawnWidth = 0;
  const draw = async (scroll) => {
    drawnWidth = pageEl.parentElement.clientWidth;
    const extent = await highlight(await render(pdfjsLib, page));
    const box = extent && drawPassageBox();
    if (box && scroll) box.scrollIntoView({ block: "center", behavior: "smooth" });
    reportExtent(extent);
  };
  await draw(true);

  // Phones rotate; re-render at the new width (serialised, so draws never interleave).
  let queue = Promise.resolve();
  let timer;
  window.addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (Math.abs(pageEl.parentElement.clientWidth - drawnWidth) < 24) return;
      queue = queue.then(() => draw(false)).catch(fail);
    }, 200);
  });
}

show().catch(fail);
