---
id: out-of-scope
title: Out of scope
is_source_of_truth: true
derives_from: [design]
status: current
authority: >
  Canonical list of what tart-taste-test deliberately does not do. Work that
  belongs to the product but is not in V1 is in deferred-work.md instead.
---

# Out of scope

Each entry says what is excluded, why, and what change to the product boundary
would cause it to be reconsidered.

### OS1. Hosts other than macOS on Apple silicon

- **Excluded:** Linux, Windows, and Intel Mac hosts; hypervisors other than
  [Tart](glossary.md#tart).
- **Why:** The project is a wrapper around Tart, which runs only on Apple
  silicon Macs.
- **Reconsider if:** the product is redefined as a general "test in a VM"
  service instead of a Tart wrapper.

### OS2. Reviewing or judging the candidate

- **Excluded:** Any assessment of whether the [candidate](glossary.md#candidate)
  is correct, safe, or malicious. The tool runs commands and reports what
  happened.
- **Why:** Its job is to give a reviewer a test result without running the code
  on the [host](glossary.md#host). A passing run does not mean the code is safe
  to merge; the candidate may have changed its own tests.
- **Reconsider if:** the product takes on review itself.

### OS3. Defending against a hypervisor escape

- **Excluded:** Protection against code that breaks out of the virtual machine
  through a flaw in Apple's virtualization framework or in Tart.
- **Why:** The VM boundary is the trust anchor. Hardening below it is the
  platform's responsibility.
- **Reconsider if:** the threat model grows to include attackers who hold such a
  flaw.

### OS4. A network service or remote brokers

- **Excluded:** Listening on a port, serving requests from other machines, or
  authenticating callers.
- **Why:** The [client](glossary.md#client) and [broker](glossary.md#broker) are
  on one machine under one user, and a file [spool](glossary.md#spool) needs no
  authentication scheme (see
  [decision D3](decisions.md#d3-requests-travel-through-a-file-spool-in-the-checkout)).
- **Reconsider if:** test runs must be requested from a different machine than
  the one that runs the VMs.

### OS5. Replacing continuous integration

- **Excluded:** Running on every push, reporting to pull requests, build
  matrices, artifact publishing.
- **Why:** The tool answers one local question, before review: do the tests pass
  on this candidate? CI remains the gate of record.
- **Reconsider if:** the product is asked to become the gate of record.

### OS6. Supplying secrets to the virtual machine

- **Excluded:** Passing credentials, tokens, or the operator's SSH agent into a
  VM.
- **Why:** The VM runs untrusted code. Anything given to it must be assumed
  disclosed.
- **Reconsider if:** a class of tests cannot run without a credential and a
  disposable, narrowly scoped credential can be issued per run.

### OS7. Installing Tart, Softnet, or VM images

- **Excluded:** Installing or upgrading Tart or [Softnet](glossary.md#softnet),
  and choosing or building images.
- **Why:** These need the operator's package manager, and Softnet needs
  administrator rights. An installer that escalates privilege is itself a risk.
- **Reconsider if:** operators ask for a single bootstrap command and accept a
  privileged step.

### OS8. Graphical and interactive tests

- **Excluded:** Tests that need a display, a logged-in desktop session, or a
  terminal attached to standard input.
- **Why:** VMs are started without graphics and commands run without a
  terminal, which keeps runs reproducible and unattended.
- **Reconsider if:** UI test suites become a target workload.
