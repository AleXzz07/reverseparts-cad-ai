import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const scriptDirectory = resolve(fileURLToPath(new URL(".", import.meta.url)));
const projectRoot = resolve(scriptDirectory, "..");
const sourcePath = resolve(projectRoot, "frontend", "index.html");
const outputDirectory = resolve(projectRoot, "public");
const outputPath = resolve(outputDirectory, "index.html");
const apiBaseUrl = (process.env.API_BASE_URL || "").replace(/\/+$/, "");

const source = await readFile(sourcePath, "utf8");
const built = source.replace("__API_BASE_URL__", JSON.stringify(apiBaseUrl));

await rm(outputDirectory, { recursive: true, force: true });
await mkdir(outputDirectory, { recursive: true });
await writeFile(outputPath, built, "utf8");

console.log(`Frontend built with API_BASE_URL=${apiBaseUrl || "(relative requests)"}`);
