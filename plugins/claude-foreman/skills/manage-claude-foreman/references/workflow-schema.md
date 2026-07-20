# Workflow schema

Propose a JSON object with a description and a non-empty `tasks` array:

```json
{
  "description": "Implement and independently verify a complex change",
  "tasks": [
    {
      "key": "implement",
      "prompt": "Implement ${feature}. Run the relevant tests.",
      "provider": "codex",
      "model": "gpt-5.6-terra",
      "effort": "high",
      "priority": 10,
      "max_turns": 80,
      "depends_on": []
    },
    {
      "key": "verify",
      "prompt": "Review the accepted implementation for ${feature}, find defects, and add or improve tests.",
      "provider": "claude",
      "model": "opus",
      "effort": "high",
      "priority": 5,
      "max_turns": 60,
      "depends_on": ["implement"]
    }
  ]
}
```

Rules:

- Give every task a unique `key` and non-empty `prompt`.
- `provider` may be `claude` or `codex`. A `gpt-5.6-*` model infers `codex`; omitting both retains the Claude Sonnet default.
- Codex models are `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna`. Do not use `ultra` with Luna.
- Reference dependencies by task key. Dependencies must exist in the same workflow.
- A phase may depend on at most one earlier phase, and a phase may have at most one child. This keeps each shared worktree's history linear. Use separate root chains for parallel work.
- Use `${name}` placeholders only for values supplied to `workflow_run.inputs`.
- Keep phases independently reviewable. A dependent task runs only after Codex accepts its parent and then continues in the same isolated worktree.
- Do not embed secrets, raw credentials, sandbox bypasses, force pushes, merges, or deployments.
- Prefer repository scripts over long inline shell programs.
- Proposing creates an immutable version. Review activates that exact version and supersedes the previous active version.
