#!/usr/bin/env node
/** Render the real terminal UI with disclosed synthetic data. No live runtime. */
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import ts from "typescript";
import { renderUsage } from "../dist/usage-render.js";

if (!process.argv[2]) throw new Error("Usage: node scripts/preview-usage-dashboard.mjs <output-directory>");
const output = resolve(process.argv[2]);
await mkdir(output, { recursive: true });
const source = (await readFile(new URL("../test/usage-fixture.ts", import.meta.url), "utf8"))
  .replace('"../src/usage-data.js"', JSON.stringify(new URL("../dist/usage-data.js", import.meta.url).href));
const module = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { usageFixture } = await import(`data:text/javascript;base64,${Buffer.from(module).toString("base64")}`);
const escape = value => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");

function ansiHtml(ansi, theme) {
  const base = theme === "dark" ? ["#f3ede1", "#1a1714"] : ["#221b15", "#f6f3ed"];
  let [fg, bg] = base, bold = false, result = "";
  for (const part of ansi.split(/(\x1b\[[\d;]+m)/)) {
    if (!part.startsWith("\x1b")) {
      result += `<span style="color:${fg};background:${bg};font-weight:${bold ? 700 : 400}">${escape(part)}</span>`;
      continue;
    }
    const codes = part.slice(2, -1).split(";").map(Number);
    for (let i = 0; i < codes.length; i++) {
      const code = codes[i];
      if (code === 0) { [fg, bg] = base; bold = false; }
      if (code === 1) bold = true;
      if (code === 22) bold = false;
      if ((code === 38 || code === 48) && codes[i + 1] === 2) {
        const rgb = `rgb(${codes.slice(i + 2, i + 5).join(",")})`;
        if (code === 38) fg = rgb; else bg = rgb;
        i += 4;
      }
    }
  }
  return result;
}

const previews = [
  ["overview-dark-130", "overview", "dark", 130, 38],
  ["overview-light-130", "overview", "light", 130, 38],
  ["overview-dark-80", "overview", "dark", 80, 24],
  ["overview-dark-x", "overview", "dark", 100, 36],
  ["models-light-110", "models", "light", 110, 32],
  ["machines-dark-130", "machines", "dark", 130, 38],
];
for (const [name, view, theme, width, height] of previews) {
  const ansi = renderUsage({ view, window: 7, direction: "consumed", theme, paused: false, offset: 0,
    report: usageFixture(), error: false, refreshing: false }, { width, height, color: true, unicode: true, interactive: false });
  const light = theme === "light";
  const markup = `<!doctype html><html lang="en"><meta charset="utf-8"><title>Potluck CLI preview — sample data</title>
<style>*{box-sizing:border-box}body{margin:0;padding:24px;background:${light ? "#ede6da" : "#15120f"};color:${light ? "#635440" : "#b4a999"};font:12px Menlo,monospace}
.frame{display:inline-block;border:1px solid ${light ? "#cfc4b4" : "#493f36"};border-radius:12px;overflow:hidden}
.bar{padding:14px 20px;background:${light ? "#e8e0d3" : "#241f1a"};letter-spacing:.04em}
pre{font:14px/1.4 Menlo,monospace;margin:0;padding:16px;background:${light ? "#f6f3ed" : "#1a1714"}}</style>
<div class="frame"><div class="bar">POTLUCK / CLI USAGE &nbsp; · &nbsp; SAMPLE DATA — VISUAL PREVIEW</div><pre>${ansiHtml(ansi, theme)}</pre></div></html>`;
  await writeFile(resolve(output, `${name}.ansi`), ansi);
  await writeFile(resolve(output, `${name}.html`), markup);
}
console.log(`Wrote ${previews.length} ANSI/HTML previews to ${output}. Capture .frame for PNG; retain the sample-data banner.`);
