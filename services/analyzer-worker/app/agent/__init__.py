"""minus20-agent: the deliberate, on-demand half of ADR-0005.

The analyzer-worker is the reflex: one bounded LLM call per alert. This
package is the agent an engineer talks to in chat when the reflex's answer is
not enough. It runs a small tool-calling loop over a closed set of read-only
tools (the existing collectors, the incident history, and a memory of past
incidents), and it never holds a tool that can change anything.

Same image as the worker, different entrypoint: `python -m app.agent`.
"""
