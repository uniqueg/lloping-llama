# Workflows

Workflow scripts (`.js` files for Claude Code's Workflow tool) go into this folder; Claude Code
discovers them when the plugin is enabled and runs them as `/lloping-llama:<name>`. There are none
yet.

A workflow fans work out to several agents in a fixed structure (e.g. a pre-release review with one
reviewer per area and an adversarial check of their findings). Like skills, workflows stay generic
and read project specifics from your overlay.
