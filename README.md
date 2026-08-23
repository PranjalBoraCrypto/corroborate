# Corroborate

**A multi-source primitive for GenLayer oracles.** One flaky source should cost you a vote,
not the whole transaction.

- **Contract on Testnet Bradbury:** [`0x59dD00eEaB89eaDbB1dF0836D64425C94DE9d7A3`](https://explorer-bradbury.genlayer.com/address/0x59dD00eEaB89eaDbB1dF0836D64425C94DE9d7A3)

---

## The failure this exists to prevent

A GenLayer contract that reads one web source and asks validators to agree on what it says has
a single point of failure that isn't in the contract at all: **the source**.

Every validator fetches it independently. If the page is assembled in the browser, or slow, or
rate-limits one node, validators receive materially different text — and then they honestly
disagree, the transaction goes undetermined, and nothing is written. The contract is correct.
The consensus mechanism is correct. The source broke it.

This is not hypothetical. It happened on Bradbury to the project this primitive was extracted
from, and the transaction is public: four leader rotations, then
[**decided Undetermined**](https://explorer-bradbury.genlayer.com/tx/0x14f9624d245fb5c776749265181b14e5221f3c1445dbabdf97c4f5c592fd011a).
The source was `docs.genlayer.com/developers/networks`, a client-rendered page.

## The mechanism

Judge several sources inside a **single** non-deterministic block and take the majority.

One flaky fetch no longer decides the answer, it only moves one vote. A validator that receives
a broken read from source A still lands on the same majority as everyone else if sources B and
C read cleanly. Redundancy reduces the variance of the final value, and it is variance in that
value — not disagreement about the truth — that causes deadlock.

Consensus is then checked on the majority alone:

```python
def validate(leader_result) -> bool:
    if not isinstance(leader_result, gl.vm.Return):
        return False
    return leader_result.calldata["verdict"] == read()["verdict"]

result = gl.vm.run_nondet_unsafe(read, validate)
```

Per-source verdicts are recorded for auditability but **deliberately excluded** from the
agreement test. Requiring those to match would reintroduce exactly the fragility this removes.

The majority is a deterministic error-correcting layer over a noisy channel: the leader and a
validator only diverge on the final verdict if two of three per-source judgements flip at once.

## The controlled result

Both halves ran on Bradbury on the same night, against the same problematic source.

| | Sources | Outcome |
| --- | --- | --- |
| Single-source oracle | `docs.genlayer.com/developers/networks` | **UNDETERMINED** — nothing written |
| Corroborate | the same page, plus two text endpoints | **SUPPORTS** — settled in 51s |

The claim, verbatim from `get_claim` on-chain:

```json
{
  "statement": "Polymarket is a prediction market.",
  "sources": [
    "https://raw.githubusercontent.com/PranjalBoraCrypto/settled/main/README.md",
    "https://en.wikipedia.org/api/rest_v1/page/summary/Polymarket",
    "https://docs.genlayer.com/developers/networks"
  ],
  "verdicts":  ["SUPPORTS", "SUPPORTS", "UNCLEAR"],
  "verdict":   "SUPPORTS",
  "agreement": 2,
  "decisive":  2,
  "note": "The source quotes Wikipedia: 'Polymarket is an American cryptocurrency-based prediction market'."
}
```

The third source is the one that deadlocked the single-source oracle. Here it returns `UNCLEAR`
and the claim settles regardless. That is the primitive working.

## The repository

`corroborate.py` is the contract. `README.md` is this file. That is deliberately all of it —
this is a primitive to be read, deployed and reused, not an application.

## Using it

```python
create_claim(claim_id, statement, criteria, sources)   # 2-5 distinct http sources
corroborate(claim_id)                                  # runs consensus, writes the majority
get_claim(claim_id)                                    # the record, including per-source votes
```

Outcomes are `SUPPORTS` / `REFUTES` / `UNCLEAR`, with `UNAVAILABLE` recorded per-source when a
fetch fails. A genuine split resolves to `UNCLEAR` rather than to whichever verdict happened to
be counted first — ties are broken deterministically, because any order-dependence here would
itself become a source of validator disagreement.

## Choosing sources — the part that decides whether this works

**Use endpoints that return text or JSON, not markup.**

`web.render` drives a real headless browser per call. Three renders plus three inference calls
in one block exceeds the node's wall-clock budget and the transaction dies as
`VALIDATORS_TIMEOUT` — verified the hard way. There is **no per-request timeout parameter** on
`render`, `get`, or `request` at SDK v0.2.16, so choosing the cheap fetch is the only lever
available. This contract therefore uses `gl.nondet.web.get`, which is a plain HTTP request and
roughly two orders of magnitude faster.

The consequence is that source *selection* becomes part of the security model:

| Good source | Why |
| --- | --- |
| `raw.githubusercontent.com/...` | plain text, small, identical bytes to every reader |
| `en.wikipedia.org/api/rest_v1/page/summary/X` | clean JSON, no markup |
| any documented JSON API | deterministic and cheap |

| Poor source | Why |
| --- | --- |
| a client-rendered docs site | validators receive different amounts of it |
| `en.wikipedia.org/wiki/X` | returns hundreds of KB of raw HTML through a plain GET |
| anything gating on User-Agent | the fetch identifies as `reqwest`, not a browser |

For an oracle, **source determinism matters as much as source authority.** A page that returns
identical bytes to every reader is usable; one assembled per-request is not, however
authoritative it looks.

## Implementation notes

- **Shaped so genvm-lint can prove it.** The non-deterministic functions are defined inline
  inside the public method rather than returned from a factory. genvm-lint establishes
  statically which code may run inside a consensus block by matching the qualified name of the
  function passed to `gl.vm.run_nondet_unsafe` against the scope where the `gl.nondet.*` calls
  were found; a closure defined in one scope and passed in another breaks that match, and the
  linter correctly refuses to certify it. Every `gl.*` call is spelled out literally for the
  same reason — the check matches the dotted name as text, so an aliased import is invisible to
  it. Verified with `genvm-lint 0.11.0`: `lint passed (3 checks)`.
- **Written against the SDK source, not the docs.** GenLayer's documentation contains errors
  that are load-bearing if copied: `gl.UserError` does not exist (it is `gl.vm.UserError`), and
  the web response field is `.status`, not `.status_code`. Every call here was checked against
  `genlayer-py-std` at tag `v0.2.16`.
- **Builtin exceptions are never raised.** They crash the WASM runtime with a generic exit code,
  discard the message, and break consensus. Every failure path raises `gl.vm.UserError` with a
  constant string — messages are compared for strict equality between leader and validator, so
  interpolating a varying value causes spurious consensus failures.
- **Storage never crosses into the non-deterministic block.** Storage objects are slot-bound
  views that cannot be serialised into the sub-VM; capturing one fails the block, and the
  symptom looks like a consensus problem rather than the storage bug it is. Every value is
  hoisted to plain Python before the closure is built.
- **Each fetch is individually guarded.** A dead source degrades to one `UNAVAILABLE` vote
  rather than throwing and taking the whole block down — which is the resilience being built.
- **`print` on the write path** costs nothing on-chain and lands in the execution log, so a
  claim that appears unmoved after an accepted transaction can be diagnosed immediately.

## Known limits

- Majority voting wants an odd source count. With two sources every disagreement is a tie that
  falls through to `UNCLEAR`. Three is the practical floor.
- Page text is truncated to 6,000 characters per source — a latency control as much as a gas
  one, since the whole block runs on every validator inside one wall-clock budget.
- No stake or weighting between sources. All sources count equally, which is wrong for the
  general case: a primary filing should outweigh a blog. Source weighting is the obvious next
  step.
- The block is serial. Fetching sources concurrently would cut latency further and allow more
  sources, but the SDK surface at v0.2.16 offers no concurrency primitive inside a nondet block.

---

Built for the GenLayer Foundation Portal, Builder → Intelligent Contracts.
Extracted from [Settled](https://github.com/PranjalBoraCrypto/settled), where the failure it
prevents was first observed.
