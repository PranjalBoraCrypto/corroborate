# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

# Corroborate — a multi-source primitive for GenLayer oracles.
#
# THE PROBLEM THIS EXISTS TO SOLVE
#
# A GenLayer contract that reads one web source and asks validators to agree on
# what it says has a hidden single point of failure: the source itself. Every
# validator fetches it independently. If the page is assembled in the browser,
# or slow, or rate-limits one node, validators receive materially different text
# — and then they honestly disagree, the transaction goes undetermined, and
# nothing is written. The contract is fine. The consensus is fine. The source
# broke the whole thing.
#
# This is not hypothetical. It happened on Testnet Bradbury to the project this
# primitive was extracted from, and the failure is on-chain:
#   explorer-bradbury.genlayer.com/tx/0x14f9624d245fb5c776749265181b14e5221f3c1445dbabdf97c4f5c592fd011a
#
# THE FIX
#
# Judge several sources inside a single non-deterministic block and take the
# majority. One flaky fetch no longer decides the answer, it only moves one vote.
# A validator that receives a broken read from source A still lands on the same
# majority as everyone else if sources B and C read cleanly. Redundancy reduces
# the variance of the final value, and it is variance in that value — not
# disagreement about the truth — that causes deadlock.
#
# Consensus is then checked on the majority alone. Per-source verdicts are
# recorded for auditability but deliberately excluded from the agreement test,
# because requiring those to match would reintroduce exactly the fragility this
# is built to remove.
#
# A NOTE ON SHAPE
#
# The non-deterministic functions are defined inline inside the public method
# rather than produced by a factory. That is not a style preference: genvm-lint
# proves statically which code can run inside a consensus block, by matching the
# qualified name of the function passed to gl.vm.run_nondet_unsafe against the
# scope where the gl.nondet.* calls were found. A closure returned from a factory
# is defined in one scope and passed in another, the two names never match, and
# the linter correctly reports that it cannot prove those calls are reachable
# from a consensus block. Every gl.* call is also spelled out literally, since
# the check matches the dotted name as text and an aliased import is invisible
# to it.

from dataclasses import dataclass

from genlayer import *

SUPPORTS = "SUPPORTS"
REFUTES = "REFUTES"
UNCLEAR = "UNCLEAR"
UNAVAILABLE = "UNAVAILABLE"          # the source could not be read at all
_DECISIVE = (SUPPORTS, REFUTES)
_VERDICTS = (SUPPORTS, REFUTES, UNCLEAR)

MIN_SOURCES = 2
MAX_SOURCES = 5

# Everything the leader returns is written on-chain, and every validator pays to
# produce it. Budgets are tight on purpose, and the page cap is also a latency
# control: the whole block runs on every validator inside one wall-clock budget.
_MAX_PAGE_CHARS = 6000
_MAX_STATEMENT = 400
_MAX_CRITERIA = 800
_MAX_NOTE = 300


@allow_storage
@dataclass
class Claim:
    id: str
    statement: str
    criteria: str
    sources: DynArray[str]
    verdicts: DynArray[str]          # one per source, positionally aligned
    verdict: str                     # the corroborated majority
    note: str
    agreement: u256                  # how many sources backed the majority
    decisive: u256                   # how many sources returned a usable verdict
    resolved: bool
    creator: str
    created_at: str
    resolved_at: str


def _prompt(statement: str, criteria: str, page: str) -> str:
    """Compose the per-source classification prompt.

    A module-level helper called from inside the non-deterministic block, which
    the linter traces through the call graph without difficulty — it is closures
    crossing a function boundary that break the trace, not ordinary calls.
    """
    return f"""You are checking a single source against a statement. Use ONLY the source text below.

STATEMENT:
{statement}

WHAT WOULD SETTLE IT:
{criteria}

SOURCE TEXT:
---
{page}
---

Rules:
- SUPPORTS only if the source text states facts that clearly satisfy the criteria.
- REFUTES only if the source text states facts that clearly contradict them.
- UNCLEAR if the source is silent, ambiguous, or does not address the statement.
- Use no knowledge beyond the source text. Do not infer or speculate.
- A source that simply does not mention the subject is UNCLEAR, never REFUTES.

Reply with a JSON object with exactly these keys:
  "verdict" - one of "SUPPORTS", "REFUTES", "UNCLEAR"
  "note"    - one sentence, at most 25 words, naming the decisive line
"""


def _tally(verdicts: list) -> tuple:
    """Reduce per-source verdicts to a majority.

    Deterministic by construction, because this runs independently on every
    validator and any order-dependence here would itself become a source of
    disagreement. Ties resolve to UNCLEAR rather than to whichever verdict
    happened to be counted first.
    """
    counts = {}
    decisive = 0
    for v in verdicts:
        if v in _DECISIVE:
            counts[v] = counts.get(v, 0) + 1
            decisive += 1

    if not counts:
        return (UNCLEAR, 0, decisive)

    top = max(counts.values())
    winners = sorted([k for k, n in counts.items() if n == top])
    if len(winners) != 1:
        return (UNCLEAR, 0, decisive)      # a genuine split is not a majority
    return (winners[0], top, decisive)


class Corroborate(gl.Contract):
    claims: TreeMap[str, Claim]
    ids: DynArray[str]

    def __init__(self):
        pass

    # ---------------------------------------------------------------- writes

    @gl.public.write
    def create_claim(self, claim_id: str, statement: str, criteria: str, sources: list) -> None:
        claim_id = claim_id.strip()
        if not claim_id:
            raise gl.vm.UserError("claim id is required")
        if claim_id in self.claims:
            raise gl.vm.UserError("claim id already exists")
        if not statement.strip():
            raise gl.vm.UserError("statement is required")
        if not criteria.strip():
            raise gl.vm.UserError("criteria are required")

        clean = []
        for s in sources:
            u = str(s).strip()
            if u and u.startswith("http") and u not in clean:
                clean.append(u)
        if len(clean) < MIN_SOURCES:
            raise gl.vm.UserError("at least two distinct http sources are required")
        if len(clean) > MAX_SOURCES:
            raise gl.vm.UserError("at most five sources are allowed")

        # get_or_insert_default hands back a live, zero-initialised view. The
        # DynArray fields cannot be constructed in memory, so the record is
        # created in storage and filled in place; a plain list assigns straight
        # into a DynArray field.
        claim = self.claims.get_or_insert_default(claim_id)
        claim.id = claim_id
        claim.statement = statement[:_MAX_STATEMENT]
        claim.criteria = criteria[:_MAX_CRITERIA]
        claim.sources = clean
        claim.verdict = UNCLEAR
        claim.note = ""
        claim.agreement = 0
        claim.decisive = 0
        claim.resolved = False
        claim.creator = gl.message.sender_address.as_hex
        claim.created_at = gl.message_raw["datetime"]
        claim.resolved_at = ""
        self.ids.append(claim_id)

    @gl.public.write
    def corroborate(self, claim_id: str) -> None:
        if claim_id not in self.claims:
            raise gl.vm.UserError("no such claim")

        claim = self.claims[claim_id]
        if claim.resolved:
            raise gl.vm.UserError("claim already corroborated")

        # Hoist to plain Python before defining the closures. Storage objects are
        # slot-bound views that cannot be serialised into the sub-VM a
        # non-deterministic block runs in; capturing one here would fail the
        # block, and the symptom would look like a consensus problem rather than
        # the storage bug it is.
        statement = str(claim.statement)
        criteria = str(claim.criteria)
        urls = list(claim.sources)

        def read() -> dict:
            verdicts = []
            notes = []

            for url in urls:
                # A plain HTTP GET, deliberately not gl.nondet.web.render.
                # render() drives a real headless browser per call and takes
                # seconds; several of those plus several inference calls exceeds
                # the node's wall-clock budget and the transaction dies as
                # VALIDATORS_TIMEOUT. There is no per-request timeout to pass, so
                # choosing the cheap fetch is the only lever available. Sources
                # should therefore be endpoints returning text or JSON, not markup.
                #
                # One dead source must degrade to a single lost vote, not take the
                # whole block down with it. This is the resilience being built.
                try:
                    res = gl.nondet.web.get(url)
                    if res.status >= 400 or res.body is None:
                        verdicts.append(UNAVAILABLE)
                        notes.append("")
                        continue
                    page = res.body.decode("utf-8", errors="replace")[:_MAX_PAGE_CHARS]
                except Exception:
                    verdicts.append(UNAVAILABLE)
                    notes.append("")
                    continue

                try:
                    raw = gl.nondet.exec_prompt(
                        _prompt(statement, criteria, page), response_format="json"
                    )
                    v = str(raw.get("verdict", "")).strip().upper()
                    if v not in _VERDICTS:
                        v = UNCLEAR
                    verdicts.append(v)
                    notes.append(str(raw.get("note", ""))[:_MAX_NOTE])
                except Exception:
                    verdicts.append(UNCLEAR)
                    notes.append("")

            majority, agreement, decisive = _tally(verdicts)

            note = ""
            for i in range(len(verdicts)):
                if verdicts[i] == majority and notes[i]:
                    note = notes[i]
                    break

            return {
                "verdict": majority,
                "verdicts": verdicts,
                "agreement": agreement,
                "decisive": decisive,
                "note": note,
            }

        def validate(leader_result) -> bool:
            """Agreement is tested on the majority alone.

            Comparing the per-source list would defeat the entire point: one
            source flipping on one node would deadlock the transaction, which is
            the failure this primitive removes. The majority is a three-value
            enum, so equality is exact, deterministic, cheaper than a judge
            model, and cannot be argued into 'close enough'.
            """
            if not isinstance(leader_result, gl.vm.Return):
                return False
            return leader_result.calldata["verdict"] == read()["verdict"]

        result = gl.vm.run_nondet_unsafe(read, validate)

        claim.verdict = result["verdict"]
        claim.verdicts = result["verdicts"]
        claim.agreement = result["agreement"]
        claim.decisive = result["decisive"]
        claim.note = result["note"]
        claim.resolved_at = gl.message_raw["datetime"]
        claim.resolved = True

        # Free on-chain, lands in the execution log. If a claim ever appears
        # unmoved after an accepted transaction, this says immediately whether
        # the write landed or the transaction went undetermined.
        print(f"corroborated {claim_id} -> {self.claims[claim_id].verdict}")

    # ----------------------------------------------------------------- views

    @gl.public.view
    def get_claim(self, claim_id: str) -> Claim:
        if claim_id not in self.claims:
            raise gl.vm.UserError("no such claim")
        return self.claims[claim_id]

    @gl.public.view
    def get_claims(self) -> dict:
        return {k: v for k, v in self.claims.items()}

    @gl.public.view
    def get_ids(self) -> list:
        return [str(i) for i in self.ids]
