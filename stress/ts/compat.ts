/**
 * TypeScript MCP SDK compatibility check.
 *
 * Connects to the databricks-ai-steward MCP server via either stdio or
 * streamable-http, runs initialize / list_tools / list_catalogs / health,
 * and emits a single JSON object on stdout describing the result. The
 * Python wrapper (stress/probe_typescript_compat.py) parses that JSON.
 *
 * Usage:
 *   tsx compat.ts stdio                     # spawns python -m mcp_server.server
 *   tsx compat.ts http <url>                # connects to existing http server
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

type Verdict = {
  transport: string;
  ok: boolean;
  steps: Array<{ name: string; ok: boolean; detail?: string }>;
};

async function withClient<T>(
  transport: any,
  fn: (client: Client) => Promise<T>,
): Promise<T> {
  const client = new Client({ name: "ts-compat-probe", version: "0" });
  await client.connect(transport);
  try {
    return await fn(client);
  } finally {
    await client.close();
  }
}

function parseToolText(content: unknown): unknown {
  // tools/call returns { content: [{ type: "text", text: "..." }], ... }
  if (
    Array.isArray(content) &&
    content.length > 0 &&
    typeof (content[0] as any).text === "string"
  ) {
    return JSON.parse((content[0] as any).text);
  }
  return content;
}

async function runChecks(client: Client, transport: string): Promise<Verdict> {
  const verdict: Verdict = { transport, ok: true, steps: [] };

  // 1. tools/list
  try {
    const { tools } = await client.listTools();
    const names = tools.map((t) => t.name).sort();
    const ok =
      names.includes("list_catalogs") && names.includes("health");
    verdict.steps.push({ name: "tools/list", ok, detail: names.join(",") });
    if (!ok) verdict.ok = false;
  } catch (e: any) {
    verdict.steps.push({ name: "tools/list", ok: false, detail: String(e) });
    verdict.ok = false;
  }

  // 2. tools/call list_catalogs (live workspace; verify shape, not names)
  try {
    const r = await client.callTool({ name: "list_catalogs", arguments: {} });
    const payload = parseToolText(r.content);
    const catalogs = (payload as any)?.catalogs;
    const ok =
      Array.isArray(catalogs) &&
      catalogs.length > 0 &&
      catalogs.every((c: any) => c && typeof c === "object" && typeof c.name === "string");
    verdict.steps.push({
      name: "tools/call list_catalogs",
      ok,
      detail: ok ? `${catalogs.length} catalog(s)` : `bad shape: ${JSON.stringify(payload).slice(0, 100)}`,
    });
    if (!ok) verdict.ok = false;
  } catch (e: any) {
    verdict.steps.push({
      name: "tools/call list_catalogs",
      ok: false,
      detail: String(e),
    });
    verdict.ok = false;
  }

  // 3. tools/call health
  try {
    const r = await client.callTool({ name: "health", arguments: {} });
    const payload = parseToolText(r.content) as any;
    const ok = payload.ready === true && payload.status === "ok";
    verdict.steps.push({
      name: "tools/call health",
      ok,
      detail: `ready=${payload.ready} status=${payload.status}`,
    });
    if (!ok) verdict.ok = false;
  } catch (e: any) {
    verdict.steps.push({
      name: "tools/call health",
      ok: false,
      detail: String(e),
    });
    verdict.ok = false;
  }

  return verdict;
}

async function main(): Promise<void> {
  const mode = process.argv[2];
  let verdict: Verdict;

  if (mode === "stdio") {
    const transport = new StdioClientTransport({
      command: process.env.PYTHON ?? "python",
      args: ["-m", "mcp_server.server"],
      env: process.env as Record<string, string>,
    });
    verdict = await withClient(transport, (c) => runChecks(c, "stdio"));
  } else if (mode === "http") {
    const url = process.argv[3];
    if (!url) throw new Error("http mode requires a URL");
    const transport = new StreamableHTTPClientTransport(new URL(url));
    verdict = await withClient(transport, (c) =>
      runChecks(c, "streamable-http"),
    );
  } else {
    throw new Error(`unknown mode: ${mode}; expected 'stdio' or 'http'`);
  }

  console.log(JSON.stringify(verdict));
  process.exit(verdict.ok ? 0 : 1);
}

main().catch((e) => {
  console.error(e);
  process.exit(2);
});
