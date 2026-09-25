const fs = require("fs");
const path = require("path");
const D = require("docx");
const { Document, Packer, Paragraph, TextRun, ImageRun, Table, TableRow, TableCell, AlignmentType,
  WidthType, BorderStyle, ShadingType, Footer, PageNumber, TabStopType } = D;
const { B, REFS, ABSTRACT, KEYWORDS } = require("./content.js");

const FIG = "/home/claude/lab/figures";
const EQ = "/home/claude/paper/eq";
const F = "Times New Roman";
const BODY = 22; // 11 pt

// ---- citations: number by first appearance ----
const order = [];
const scan = (s) => (s.match(/\[@(\w+)\]/g) || []).forEach((m) => {
  const k = m.slice(2, -1);
  if (!REFS[k]) throw new Error("missing ref " + k);
  if (!order.includes(k)) order.push(k);
});
for (const b of B) {
  if (b.x) scan(b.x);
  if (b.cap) scan(b.cap);
  if (b.note) scan(b.note);
  if (b.items) b.items.forEach(scan);
  if (b.rows) b.rows.forEach((r) => r.forEach(scan));
}
const num = (k) => order.indexOf(k) + 1;
const cite = (s) => s.replace(/(\[@\w+\])+/g, (grp) => {
  const ns = grp.match(/@(\w+)/g).map((m) => num(m.slice(1)));
  return ns.map((n) => `[${n}]`).join(", ");
});

// ---- inline markup ----
function runs(text, o = {}) {
  const size = o.size || BODY;
  const out = [];
  const parts = cite(text).split(/(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g);
  for (const p of parts) {
    if (!p) continue;
    if (p.startsWith("`")) out.push(new TextRun({ text: p.slice(1, -1), font: "Courier New", size: size - 2 }));
    else if (p.startsWith("**")) out.push(new TextRun({ text: p.slice(2, -2), font: F, size, bold: true, italics: o.italics }));
    else if (p.startsWith("*")) out.push(new TextRun({ text: p.slice(1, -1), font: F, size, italics: true, bold: o.bold }));
    else out.push(new TextRun({ text: p, font: F, size, bold: o.bold, italics: o.italics }));
  }
  return out;
}

function pngSize(file) {
  const b = fs.readFileSync(file);
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20), data: b };
}
function image(file, widthIn) {
  const s = pngSize(file);
  const wpx = Math.round(widthIn * 96);
  return new ImageRun({ type: "png", data: s.data, transformation: { width: wpx, height: Math.round(wpx * s.h / s.w) } });
}
function imageH(file, heightIn) {
  const s = pngSize(file);
  const hpx = Math.round(heightIn * 96);
  return new ImageRun({ type: "png", data: s.data, transformation: { width: Math.round(hpx * s.w / s.h), height: hpx } });
}

const sp = { line: 276, before: 0, after: 120 };
const children = [];
const P = (o) => children.push(new Paragraph(o));

// ---- title block ----
P({ alignment: AlignmentType.CENTER, spacing: { after: 60 }, children: [new TextRun({ text: "Preprint. Not peer reviewed.", font: F, size: 18, italics: true, color: "555555" })] });
P({ alignment: AlignmentType.CENTER, spacing: { before: 120, after: 240 }, children: [new TextRun({ text: "Silent Log Loss in Kubernetes: Mechanisms, Detection, and a Sequence-Marker Solution", font: F, size: 34, bold: true })] });
P({ alignment: AlignmentType.CENTER, spacing: { after: 40 }, children: [new TextRun({ text: "Prasad MK", font: F, size: 24 })] });
P({ alignment: AlignmentType.CENTER, spacing: { after: 40 }, children: [new TextRun({ text: "Independent Researcher, Rocklin, California, United States", font: F, size: 20, italics: true })] });
P({ alignment: AlignmentType.CENTER, spacing: { after: 40 }, children: [new TextRun({ text: "prasad.rocklin@gmail.com  |  ORCID: 0009-0004-9496-6339", font: F, size: 20 })] });
P({ alignment: AlignmentType.CENTER, spacing: { after: 280 }, children: [new TextRun({ text: "September 2026", font: F, size: 20 })] });
const rule = { style: BorderStyle.SINGLE, size: 6, color: "000000" };
P({ border: { top: rule }, spacing: { before: 0, after: 80 }, children: [new TextRun({ text: "Abstract", font: F, size: BODY, bold: true })] });
P({ alignment: AlignmentType.JUSTIFIED, spacing: sp, children: runs(ABSTRACT, { size: 21 }) });
P({ border: { bottom: rule }, alignment: AlignmentType.LEFT, spacing: { after: 280 }, children: [new TextRun({ text: "Keywords: ", font: F, size: 21, bold: true }), new TextRun({ text: KEYWORDS, font: F, size: 21 })] });

// ---- body ----
const cellBorder = { style: BorderStyle.SINGLE, size: 4, color: "808080" };
const borders = { top: cellBorder, bottom: cellBorder, left: cellBorder, right: cellBorder };
const DXA = (inch) => Math.round(inch * 1440);

for (const b of B) {
  if (b.t === "h1") P({ keepNext: true, spacing: { before: 280, after: 120 }, children: [new TextRun({ text: b.x, font: F, size: 24, bold: true })] });
  else if (b.t === "h2") P({ keepNext: true, spacing: { before: 180, after: 80 }, children: [new TextRun({ text: b.x, font: F, size: BODY, bold: true, italics: true })] });
  else if (b.t === "p") P({ alignment: AlignmentType.JUSTIFIED, spacing: sp, children: runs(b.x) });
  else if (b.t === "list") b.items.forEach((it, i) => P({ alignment: AlignmentType.JUSTIFIED, spacing: { line: 276, after: 80 }, indent: { left: 540, hanging: 360 }, children: [new TextRun({ text: `(${i + 1})\t`, font: F, size: BODY }), ...runs(it)], tabStops: [{ type: TabStopType.LEFT, position: 540 }] }));
  else if (b.t === "fig") {
    P({ alignment: AlignmentType.CENTER, keepNext: true, spacing: { before: 120, after: 60 }, children: [image(path.join(FIG, b.file), b.w)] });
    P({ alignment: AlignmentType.JUSTIFIED, spacing: { line: 240, after: 200 }, children: runs(b.cap, { size: 19 }) });
  } else if (b.t === "eq") {
    P({ spacing: { before: 100, after: 140 }, tabStops: [{ type: TabStopType.CENTER, position: 4680 }, { type: TabStopType.RIGHT, position: 9360 }],
      children: [new TextRun({ text: "\t", font: F, size: BODY }), imageH(path.join(EQ, b.file), b.h), new TextRun({ text: `\t(${b.n})`, font: F, size: BODY })] });
  } else if (b.t === "table") {
    P({ keepNext: true, spacing: { before: 160, after: 80 }, children: runs(b.cap, { size: 19, bold: true }) });
    const tw = b.widths.reduce((a, c) => a + c, 0);
    const widths = b.widths.map((w) => DXA(w * 6.5 / tw));
    const mk = (cells, head) => new TableRow({ tableHeader: head, cantSplit: true, children: cells.map((c, i) => new TableCell({
      width: { size: widths[i], type: WidthType.DXA }, borders,
      shading: head ? { type: ShadingType.CLEAR, fill: "E2E8F0", color: "auto" } : undefined,
      margins: { top: 40, bottom: 40, left: 70, right: 70 },
      children: [new Paragraph({ spacing: { line: 240 }, children: runs(c, { size: 17, bold: head }) })] })) });
    children.push(new Table({ width: { size: widths.reduce((a, c) => a + c, 0), type: WidthType.DXA }, columnWidths: widths,
      rows: [mk(b.head, true), ...b.rows.map((r) => mk(r, false))] }));
    P({ alignment: AlignmentType.JUSTIFIED, spacing: { before: 60, after: 200, line: 240 }, children: b.note ? runs(b.note, { size: 17, italics: true }) : [] });
  } else if (b.t === "algo") {
    const w = DXA(6.5);
    const thick = { style: BorderStyle.SINGLE, size: 8, color: "000000" };
    const none = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
    children.push(new Table({ width: { size: w, type: WidthType.DXA }, columnWidths: [w], rows: [new TableRow({ children: [new TableCell({
      width: { size: w, type: WidthType.DXA }, borders: { top: thick, bottom: thick, left: none, right: none },
      margins: { top: 80, bottom: 80, left: 100, right: 100 },
      children: [new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text: b.title, font: F, size: 19, bold: true })] }),
        ...b.lines.map((l) => new Paragraph({ spacing: { line: 240 }, children: [new TextRun({ text: l, font: "Courier New", size: 16 })] }))] })] })] }));
    P({ spacing: { after: 160 }, children: [] });
  }
}

// ---- references ----
P({ keepNext: true, spacing: { before: 280, after: 120 }, children: [new TextRun({ text: "References", font: F, size: 24, bold: true })] });
order.forEach((k, i) => P({ alignment: AlignmentType.LEFT, spacing: { line: 240, after: 60 }, indent: { left: 500, hanging: 500 },
  tabStops: [{ type: TabStopType.LEFT, position: 500 }], children: [new TextRun({ text: `[${i + 1}]\t` + REFS[k], font: F, size: 18 })] }));
const unused = Object.keys(REFS).filter((k) => !order.includes(k));
if (unused.length) console.log("UNUSED REFS:", unused);

const doc = new Document({
  styles: { default: { document: { run: { font: F, size: BODY } } } },
  sections: [{
    properties: { page: { size: { width: 12240, height: 15840 }, margin: { top: 1440, bottom: 1440, left: 1440, right: 1440 } } },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [
      new TextRun({ text: "Prasad MK, Silent Log Loss in Kubernetes (preprint, 2026)    ", font: F, size: 16, color: "666666" }),
      new TextRun({ children: [PageNumber.CURRENT], font: F, size: 16, color: "666666" })] })] }) },
    children,
  }],
});
Packer.toBuffer(doc).then((buf) => { fs.writeFileSync("/home/claude/paper/silent_log_loss_kubernetes.docx", buf); console.log("built, refs:", order.length); });
