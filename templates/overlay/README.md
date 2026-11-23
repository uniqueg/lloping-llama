# Overlay template

A starting point for your own **overlay**: the private layer of files that turns the generic
lloping-llama kit into your setup. Copy this folder into a new **private** repository, then fill in
the files. Every file says what goes into it.

```text
overlay/
├── CLAUDE.md              entry point; imported from ~/.claude/CLAUDE.md
├── profiles.yaml          per-repository profiles: who decides, who merges
├── me/                    you: preferences, machine, people you work with
├── orgs/<org>/            one folder per organization: conventions, repository policy
├── projects/<project>/    one folder per project: state, decisions, runbooks, drafts, reports
├── todos/                 your to-do list (source of your to-do board)
└── archive/               finished material that is kept, not deleted
```

Layering: **kit → me → org → project**. A more specific layer wins over a more general one: a
project rule overrides an org convention, which overrides your personal defaults, which override the
kit's defaults.

## Set up

1. Create a private repository (e.g. `<you>/agent-overlay`) and copy this folder's contents into it.
2. Rename `orgs/example-org/` and `projects/example-project/` to your org and project names; copy
   them for more orgs and projects.
3. Fill in `me/` (run a collector in `me/collect-machine/` for `me/machine.md`), `profiles.yaml` and
   the org and project files.
4. Import the overlay in your user memory, `~/.claude/CLAUDE.md`:

   ```text
   @~/path/to/agent-overlay/CLAUDE.md
   ```

5. Never commit secrets: no tokens, passwords, kubeconfigs or `.env` files. The overlay holds
   decisions and context, not credentials.

See the Overlay guide in the lloping-llama docs for details.
