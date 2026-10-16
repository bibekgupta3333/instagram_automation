# MCP servers for the operator console (`.mcp.json`)

The repo-root `.mcp.json` follows docs 16 §5 and is for **Claude Code as an operator console**.
The pipeline never uses it: pipeline stages call plain Python clients (docs 16 §1, "Pattern").
Every secret is an env-var placeholder (`${VAR}`). Don't put real keys in the file.

| Server | Purpose | Notes |
|---|---|---|
| `buffer` | Buffer's official MCP (`https://mcp.buffer.com/mcp`), Bearer `${BUFFER_READONLY_KEY}` | **Read-only console.** Buffer keys are account-wide with no scoping, and `create_post` can publish immediately (docs 16 §6). Use a *separate* key from the Publisher's `BUFFER_API_KEY`. MCP calls share the 100 req/15 min API bucket |
| `files` | `@modelcontextprotocol/server-filesystem` | Scoped to `./output` only |
| `fetch` | `mcp-server-fetch` | For fact-checking. Treat fetched text as untrusted data. Block `169.254.169.254` and private ranges, and keep a domain allowlist in the Fact-Checker |
| `brave-search` | `@brave/brave-search-mcp-server`, `${BRAVE_API_KEY}` | Web search for fact-checking and trend research |
| `aws-docs` | `awslabs.aws-documentation-mcp-server` | Optional. Public AWS docs; no credentials needed. Don't give it an AWS profile |
| `youtube-readonly` | yutu (`yutu mcp`) | Optional and **disabled**. When enabled, it must use a read-only OAuth client (`youtube.readonly`, `yt-analytics.readonly`). Uploads go only through `studio/publish/youtube.py` |

## Deny Buffer write tools (required)

Buffer has no read-only key, so **the deny list is the control** (docs 16 §5). Agents must never
call `create_post`, `edit_post`, `delete_post`, `execute_mutation` or `create_idea`. Add this to
`.claude/settings.json` (owned outside this layer):

```json
{
  "permissions": {
    "deny": [
      "mcp__buffer__create_post", "mcp__buffer__edit_post", "mcp__buffer__delete_post",
      "mcp__buffer__execute_mutation", "mcp__buffer__create_idea",
      "mcp__youtube-readonly__*"
    ]
  },
  "disabledMcpjsonServers": ["youtube-readonly"]
}
```

Then allow specific read tools one at a time. Tool names are from docs 16 §5 and are
**unverified**: list the real names with `/mcp` once the server is connected, and update the
deny list to match.

## Enabling and disabling servers

`.mcp.json` has no standard "disabled" flag. The `"disabled": true` on `youtube-readonly` is
only advisory. The real switch is `disabledMcpjsonServers` / `enabledMcpjsonServers` in
`.claude/settings.json`, or approving or declining the server when Claude Code prompts.

## Security checklist (docs 16 §6)

- No LLM has publish rights. The Publisher's key lives only in the Publisher's environment
  (Keychain or 1Password), never in `.mcp.json`.
- Pin versions once they're stable. `@latest` / unpinned `uvx` is fine for a first look only.
  TODO: pin `mcp-server-fetch`, `@modelcontextprotocol/server-filesystem`,
  `@brave/brave-search-mcp-server` and `awslabs.aws-documentation-mcp-server`.
- Prefer stdio servers. Review tool descriptions. Don't mix untrusted servers into the console
  session.
- yutu env var names (`YUTU_CREDENTIAL`, `YUTU_CACHE_TOKEN`) are **unverified**. Check the
  yutu README.
