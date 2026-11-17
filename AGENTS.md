# Agents working on couchy-agent

Claude, Codex and Grok agents work this repository as part of the same tick
that covers [getcouchy/couchy](https://github.com/getcouchy/couchy). The
protocol is that repository's
[`AGENTS.md`](https://github.com/getcouchy/couchy/blob/main/AGENTS.md); read it
before picking up work here. It applies here unchanged:

- A `swarm/agent/<name>` label on an issue or PR means that agent is working
  on it right now: a lock, not ownership. Put yours on before you change
  anything for it and take it off when your tick ends. Once it is off, the
  work is anyone's to carry on.
- Put `swarm/pr` on every PR you open. That label is what makes a PR the
  agents' to review, fix and merge, whoever opened it.
- The queue is in Plane: this repository's work items are in project
  COUCHYUX with getcouchy/couchy's, and the tick takes them in the order
  getcouchy/swarm's `PROTOCOL.md` gives (in focus, in Todo, nobody on them, no
  open PR, no open blocker). A blocker is often getcouchy/couchy work.
- Any `swarm/pr` PR here that nobody is on is yours to review, unless you
  opened or pushed to it in this same session. End the review with
  `<!-- agent: <your name> verdict: ok -->` or `verdict: changes`; `ok` turns
  on auto-merge (squash).
- `swarm/review-only` on a PR asks for your review and nothing more: review
  it, but never push to it or turn its auto-merge on or off.
- `swarm/needs-human` is for a real fork and pages the owner; `swarm/hold` holds a
  PR for a wait an agent can end.

What differs here:

- The Agent desk and the heartbeats are in getcouchy/couchy. Nothing sweeps
  this repository, so a label left on here stays until someone removes it:
  take yours off every time.
- There is no changelog check and no simulator flow. Run `python3 -m unittest discover -s tests -t .` (README, "Test")
  before you push.
