import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const scriptDirectory = resolve(fileURLToPath(new URL(".", import.meta.url)));
const projectRoot = resolve(scriptDirectory, "..");
const sourcePath = resolve(projectRoot, "frontend", "index.html");
const outputDirectory = resolve(projectRoot, "public");
const outputPath = resolve(outputDirectory, "index.html");
const configOutputPath = resolve(outputDirectory, "app-config.js");
const apiBaseUrl = (process.env.API_BASE_URL || "").replace(/\/+$/, "");

const source = await readFile(sourcePath, "utf8");
const appConfig = `window.REVERSEPARTS_API_BASE_URL = ${JSON.stringify(apiBaseUrl)};\n`;

await rm(outputDirectory, { recursive: true, force: true });
await mkdir(outputDirectory, { recursive: true });
await writeFile(outputPath, source, "utf8");
await writeFile(configOutputPath, appConfig, "utf8");

console.log(`Frontend built with API_BASE_URL=${apiBaseUrl || "(relative requests)"}`);
