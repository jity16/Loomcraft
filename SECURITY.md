# Security

## Reporting a vulnerability

Open a [private security advisory](https://github.com/jity16/Loomcraft/security/advisories/new)
rather than a public issue. Please include a reproduction and the version or
commit you tested.

## The threat model

LoomCraft's central assumption is that **the model is untrusted input**. A plan
is a proposal, not an instruction; the broker re-validates everything against
server-owned state before anything runs. The properties that follow from that:

- A `capability` or `workflow` step's status is written only by its execution
  tool, so `succeeded` always corresponds to a real run.
- A step runs only when its dependencies are satisfied.
- Source references are resolved and integrity-checked against files the session
  actually owns; a path never reaches a runner unverified.
- `requires_approval` parks a node *before* its runner is invoked.
- Errors returned to a model carry a stable code and a bounded message, and
  never echo the rejected input.
- Artifact metadata that leaves the process is scrubbed of host filesystem
  paths, command lines and environment.

## What LoomCraft does not do

These are the host's responsibility, and the library will not pretend otherwise:

- **Authentication, authorisation and tenancy.** `SessionStore` isolates
  sessions from each other, not users from each other.
- **Sandboxing runners.** A registered runner is your code and runs with your
  process's privileges. Run untrusted work in a container or VM.
- **Rate limiting and quota.** `BrokerLimits` bounds one turn's tool calls; it
  is not a billing or abuse control.
- **Encryption at rest.** Session directories are created `0700` and files
  `0600`, which is a default, not a guarantee about your disk.

## Supported versions

Pre-1.0: fixes land on `main` and in the next release. There are no backports.
