# Agents working on couchy-agent

Claude, Codex and Grok agents work this repository under the same protocol as
[getcouchy/couchy](https://github.com/getcouchy/couchy). The protocol and the
tools that run it live in **[getcouchy/swarm](https://github.com/getcouchy/swarm)**:
read its [`PROTOCOL.md`](https://github.com/getcouchy/swarm/blob/main/PROTOCOL.md)
before picking up work here. This file adds only what is this repository's own.

## Getting the protocol and the tools

getcouchy/swarm is public, so your sandbox can clone it without being given
access. At the start of every tick, beside your checkout of this repository:

```bash
git clone --depth 1 https://github.com/getcouchy/swarm ../swarm   # or: git -C ../swarm pull --ff-only
```

Then read `../swarm/PROTOCOL.md`. With a token, run the tools from this checkout
with this repository as home: `GITHUB_REPOSITORY=getcouchy/couchy-agent
AGENT_REPOS=getcouchy/couchy-agent AGENT_NAME=<your name> node ../swarm/agent.mjs next`.

## Home repository

A tick whose prompt names this repository has it as its home: the Agent desk
(the open `swarm/desk` issue here) and its heartbeat are here, and
`.github/workflows/agent-sweep.yml` sweeps it every hour, freeing lapsed
claims and keeping that desk current. A tick of getcouchy/couchy covers this
repository too when its sandbox can reach it, and heartbeats on couchy's desk.

Issues here are often blocked by issues in getcouchy/couchy ("Blocked by
getcouchy/couchy#1203"). A tick that can't read couchy counts such a blocker as
open and passes the issue by; it is couchy's ticks that can take it.

## This repository's rules

- There is no changelog check and no simulator flow. Run
  `python3 -m unittest discover -s tests -t .` (README, "Test") before you push.
- `reviewed` is not a required check here, so auto-merge goes on with an `ok`
  verdict (the protocol's "Merging"), not when a PR opens.
