"""Reconcile the funnel's settlement lines against the chain.

Reads `settled` and `settlement-unknown` lines, keys each by
(network, asset, payer, nonce) - the authorization's identity, which EIP-3009
scopes per token contract - and asks the token contract whether that nonce
has been consumed. The state machine, from parthod0x/satroot#2:

    settled            consumed, Transfer pays us        -> confirmed
    settled            consumed, Transfer pays elsewhere -> investigate
    settled            not consumed                      -> investigate
    unknown            consumed, Transfer pays us        -> confirmed (the answer was
                                                            lost, not the payment)
    any                consumed, Transfer pays a known   -> routed: paid into the
                       receiver of our facilitator          facilitator's receiver,
                                                            payout to us pending
    unknown            consumed, the window searched completely and no Transfer
                       from the payer in it              -> not-paid, terminal: the
                                                            authorization we served
                                                            was never paid; the slot
                                                            was burned by another
    unknown            consumed, a payer Transfer in-window to a `to` we do
                       not recognise                     -> unrecognised-receiver: a
                                                            configuration gap, not a
                                                            statement about the buyer
    unknown            consumed, window clamped (validAfter absent) or the log
                       read incomplete                   -> undetermined: no verdict
    unknown            not consumed, now <  validBefore  -> pending (may still land)
    unknown            not consumed, now >= validBefore  -> closed, not charged
    any                RPC failed                        -> unavailable: not judged

"Consumed" and "consumed by the authorization we served" are different
predicates. authorizationState[from][nonce] says only that the slot was
burned; it carries neither `to` nor `value`. The executed Transfer does,
and `to`/`value` are signed but are not part of the slot's identity - so a
payer can sign A (to us, value X) and A' (to themselves, value 1) on one
nonce and settle A'. Reading the burned slot as "charged" would call X of
served work paid. The Transfer selected is the one whose `from` is the
payer, never the first in the receipt: routed settlement can carry several,
and the first would reintroduce the relayer-vs-buyer confusion one layer up.

The scan window for an unknown line is the authorization's own
(validAfter, latest validBefore): both ends come from the signature, not
from this host's clock. The line's own time is a late sample, not a lower
bound - the transfer being hunted is by construction older than the line
that reported it, and any log delay would have cut the window exactly where
the evidence lives. A validAfter of zero means "since genesis" and is
clamped to a bounded lookback.

Facilitators that batch settle transfer into their own receiver and pay out
later, so a perfectly good payment can show a Transfer whose `to` is the
facilitator's contract. Those receivers are configuration, per facilitator
and per asset (SATLEDGER_X402_FACILITATOR_RECEIVERS, comma-separated, or
--receivers=), and a Transfer to one of them is "routed", not "not-paid".

Evidence is bound to the nonce. The tx that consumes an EIP-3009 nonce emits
AuthorizationUsed(authorizer, nonce) - or AuthorizationCanceled - beside its
Transfer, so the reconciler does not hunt the payer's transfers and guess
which invoice one belongs to: it finds the one tx that consumed THIS nonce
and reads that tx's transfers. One transfer therefore satisfies one row,
across runs, with no claimed-log set to share; a cancellation is a definite
"not paid"; and a facilitator-reported hash whose receipt is still null is
"pending" until a grace period passes, after which a hash that never mined
is its own fact, distinct from one that mined and moved nothing.

"Not paid" is reserved for a verdict: the consuming tx was found and its
transfer does not pay us, or the nonce was cancelled.

Within the consuming tx, a Transfer belongs to the authorization it follows:
FiatTokenV2 emits AuthorizationUsed(authorizer, nonce) and then the Transfer
it caused (logIndex k and k+1 on live Base receipts). That is a fact about
the token's body, not about the log stream, so the matcher does not assume
adjacency in the stream: it walks the logs after the AuthorizationUsed,
ignores logs from other addresses (a hook or a proxy may emit between the
two), and takes the contiguous run of the asset's own Transfers from the
payer, stopping at the first asset log that is anything else - the next
authorization event, a transfer by someone else, or a plain transfer that
merely sits later in the same call. Only that run is attributed to the
nonce; surplus can only come from it. Payer transfers found further along,
before the next authorization, are unattributable: if the adjacent run
already satisfies the amount they are noted and ignored, and if it does not,
the row is undetermined rather than summed into a confirmation. As a
structural guard, every (txHash, logIndex) a row rests on is claimed across
the run; two rows resting on one log are both undetermined, with the
collision named.

A plain ERC-20 transfer to payTo consumes no nonce and never satisfies an
invoice: nothing was served against it and nothing can bind it to one. Such
transfers are reported once, at the payer/payee level, as their own row
("unsolicited-transfer": money that landed outside this ledger, to refund
or apply by hand); the invoice rows that have only this evidence stay
undetermined, since an identity-less transfer must not be attached to N
invoices it may have nothing to do with. A payer Transfer to a `to` nobody
configured is "unrecognised-receiver" - the empty receiver set is a stated
choice, and a stated choice must not be able to manufacture negatives. A
consumed authorization whose paired Transfers to payTo (or to a receiver)
sum to less than the amount, with nothing later in the run to make it up,
is "underpaid": the authorization was used and it paid short, with the
shortfall on the row. It is distinct from "undetermined", which is not a
verdict at all - but it is terminal only when its evidence is final (below):
read near the head it is "short in what has been read", which a later block
can still change.

"undetermined" means: not provable from the evidence read. It is not a
pending state with a re-check hook; every run recomputes every row from the
whole log and the chain, so nothing is ever "not re-read" - but the only
thing that can change such a row is depth (a reorg, or a window that was
read near the head). Each row therefore carries `final`: True when every
piece of evidence it rests on - the window searched for the consuming
event, the consuming tx's receipt, and the cross-check search - is at or
below the node's `finalized` head: on an L2 that is the last block whose
batch sits in a finalized L1 block (on Base about 20 minutes behind the
tip), so no reorg of either chain can change it. The weakest piece decides.
A provider that does not report a finalized head makes nothing final.
Two integers ordering correctly is not the claim, so "final" also asserts
that the chain reports the evidence block's own hash at that height (the
receipt's blockHash, or the canonical-checked hash of its logs). Finalized
heights from two providers at the same moment legitimately differ by the
batch and L1 lag; only a hash difference at one height is a contradiction.

Whether an endpoint's `finalized` tag can be trusted at all is a fact about
the endpoint, so it is judged once per endpoint per run, before any row,
and never charged to rows. It is judged on what the endpoint itself says,
with no clock and no history in the verdict: the `latest`, `safe` and
`finalized` headers it serves, which must be strictly ordered,
finalized < safe < latest. Any equality or inversion is a smell, named:
`finalized` at or above `safe` (the tag answered as safe, or somewhere in
the band between safe and latest - the endpoint's own `safe` is therefore
the default floor, with no per-network constant), `safe` at or above
`latest` (one of the two tags is not doing its job). Without a `safe` tag
the band cannot be checked and the line says so; `finalized` within
SMELL_MAX_DEPTH blocks of `latest` is still a smell (answered as latest).
A floor in blocks can be configured per network for chains whose `safe`
is itself late (SATLEDGER_FINALITY_FLOOR).

A provider that clamps `finalized` to just below an honest `safe` passes
every invariant one endpoint can state about itself. That is closed by
comparing endpoints, not constants (SATLEDGER_FINALITY_SAMPLE, per network,
peer URLs separated by "|"). Honest peers disagree on the finalized height,
because finality advances in jumps and they read at different moments, so
height alone is never the verdict. Three things are:
  - the primary's finalized height is above every peer's `latest`: a block
    no independent endpoint has even produced - fabricated;
  - the primary is ahead of every peer's finalized height, and after one
    re-read CROSS_RETRY_SECONDS later it is above the midpoint of the
    freshest peer's own finalized and safe - nearer that peer's safe than
    its finalized: a clamp. "Freshest" is by `finalized` alone: ranking by
    finalized + safe would favour a peer behind on finalized and current
    on safe, which is the inflated gap again. The line prints the lead over
    the whole field and the depth into that ladder as separate numbers. Each midpoint comes from one peer's own
    ladder, so a subtraction never pairs one peer's `safe` with another's
    `finalized` (that pairing can go negative, and then any lead fires).
    A peer whose `safe` is at or below another peer's `finalized` is stale -
    the chain has finalized past it - and sizes nothing; with no usable
    ladder the lead is "not judged".
    Being ahead is not the evidence. On
    31 Oct 2026, sampled every 5 s, mainnet.base.org reached a new finalized
    height about 290 s before blastapi did, blastapi staying one step (186
    blocks) behind throughout, and publicnode and drpc alternated between
    the two heights from one URL - load-balanced backends, so one endpoint's `finalized`
    can go backwards between reads. An honest lead is about one finality
    step; a clamp just below `safe` leads by nearly the whole gap
    (~520-630 blocks on Base). The gap is read from the peers each time, so
    no chain constant is needed;
  - the peers that have finalized that height report a different hash
    there, by majority: a fork or a fabricated block.
All three rank heights, and ranking heights rests on an assumption that is
the chain's, not this code's: the safety half of finality - no chain
finalizes two blocks at one height. A peer on a minority fork after an
incident, or one overstating how far it has finalized, advertises a higher
`finalized` and wins any comparison of numbers. So hashes are compared
first, across every claim, the primary's included: claims at one height
must carry one hash, and each lower claim must be the block the highest
claimant's chain reports at that height (one call per lower height: if
all lie on that one chain, all lie on one chain together). A contradiction
is "undetermined", printing the height and both hashes, and no height
verdict is given beside it - a minority that disagrees included, since a
vote between two finalized blocks at one height settles nothing. Only a
block above every peer's tip ("fabricated") is still reported first, as
it rests on no hash. A height the asked endpoint does not answer is an
absence and is not compared.
The highest claim is the anchor every lower one is checked against, so
its own (height, hash) would otherwise meet no second source: a peer
inventing a finalized hash one step ahead, below another peer's tip,
answers the lower heights from its real chain and passes every
comparison. The anchor is therefore witnessed first - by a second claim
at its height when there is one (no call), else by an eligible endpoint
reporting the same block there (usually one call). Eligible means its
own `safe` head has reached the anchor's height: its `finalized` cannot
have (the anchor is the highest finalized claim by construction), and
below `safe` the block is derived from L1 data, so a lagging sequencer
head cannot pose as a contradiction. A different hash is a contradiction;
"no endpoint's safe head had reached N" and "K eligible endpoints did not
answer" are stated apart. The witness is one source: its identity is on
every height-bearing line, in the result (`witness`) and in the history
record, and each contradiction names the role of each side (anchor,
witness, finalized claim, anchor witnessed or unwitnessed), so a later
audit can tell one unverified source from two independent ones. Re-reading the anchor from its own
endpoint was rejected: that is self-consistency, which a liar with a
consistent local view passes, not corroboration. Attribution is
asymmetric and the line does not resolve it: "X at b vs Y on a's chain"
can be manufactured against an honest b by an anchor answering the lower
height wrongly. Once the anchor is witnessed the fault more likely lies
with the lower claim, but the verdict stays "undetermined" either way.
A peer one step behind lands below the primary with the same hash: drift,
not a fault. A peer whose `finalized` is lower on the re-read than on the
first read is unstable - one URL in front of backends at different heights
- and is dropped from that verdict by name: its answer is a sample of a
backend, not of the chain. A peer that answered the first read and not the
re-read is named too, as "no answer", so a quorum never shrinks silently. At least two stable peers must answer the
reading the verdict rests on, the re-read included, or the line says "no
quorum". Every lead verdict prints each peer's (finalized, safe, latest), so
one outlier and a whole stale set do not read alike. A clamp that claims
less than half the gap early is indistinguishable, in one run, from the
freshest peer lagging one step; so is an honest primary whose freshest
peer lags two steps, which reads as a clamp - loud, and the per-peer
triples show whether the whole set is stale (one provider's cache under
several names). The midpoint is the threshold rather than a third of the
gap: Base's gap rides the finality sawtooth (observed 31 Oct: finalized
519-705 blocks behind latest, safe 45-60 behind, so ~470-650), and a
third of the low end is ~155, which calls the measured honest lead of 186
a clamp. Half of it is ~235: the honest margin is real but not wide.
What this cannot see: a majority of the sample agreeing on the same wrong
answer, peers that are one backend under several names (two public Base
endpoints return identical answers to the hash, which is a hint of a
shared upstream), or a deployment whose only source is one provider. A median over the endpoint's own runs was tried and
rejected: an endpoint that stays clamped becomes its own baseline and
clears itself. A chain whose honest `finalized` equals `latest` or `safe`
reads as a smell - a loud false positive that prints both heads, which is
the right direction for the last line of defence. The three tags can be
answered by different backends behind one load balancer; that is the
trade taken. A smelly endpoint is one line naming it, its depths and the
rows not judged; its rows are not judged, and the run cannot be clean.
CONFIRMATIONS (64 blocks, ~2 minutes on Base) is a depth fact and is used
only for "may an event still appear", never to call a row terminal. A window that had to be
clamped, or a log read that was not complete, is "undetermined" too.
Every row records the window actually searched, so a later disagreement can
be settled by re-reading the search rather than only its conclusion.

Within one run several receivers can appear: transferWithAuthorization to
payTo followed by a plain transfer to someone else in the same multicall is
two contiguous Transfers from the payer. The amount check is therefore per
`to`: the Transfers to payTo (or to one receiver) must cover the amount by
themselves, surplus is measured inside that group, and every other group is
printed on the row as its own fact and never summed into the verdict.

Log evidence is identified by (network, blockNumber, blockHash, logIndex).
Across a reorg the same height can carry two hashes; such a pair is two
pieces of evidence, not one claim twice. If the height is by now at or
below the finalized head, the chain has answered: rows resting on the canonical
hash stand, and rows resting on the orphan are undetermined and not final
(their evidence is gone; the next run reads the canonical one). Near the
head neither can be told apart, so every row resting on either is
undetermined.

Providers cap eth_getLogs answers; a capped page is not an empty page and
looks exactly like a short run. No threshold decides this. Every block
that contributed a log is read again with eth_getBlockReceipts, by hash -
bounded by the block, so no log cap applies - and the filter's logs in it
must include every log the page returned; logs the page missed are taken
from the receipts. After each page that returned logs, the blocks after the
last one it reached are read again as their own range (a capped answer
stops at some block, and everything after that block is what it dropped),
until a tail comes back empty. The ranges are disjoint, so nothing is summed
twice. This relies on providers returning logs in (block, logIndex) order,
which is checked as *strict* increase - a log repeated at the same pair is
a duplicate that would inflate a sum into a surplus, so it is IncompleteRead,
inside a page and across the whole walk. Logs a provider returns outside the
range asked for are dropped from that page (the page that owns their block
returns them), and every remaining dedupe is on (blockHash, logIndex), never
on blockNumber, which would merge two branches.

Every range asked for is at most PAGE_BLOCKS blocks, and the last one ends
exactly at the window's end, so an empty answer means "these blocks were
read and held none" for a range we bounded, not a provider's promise about
a range it may have cut by span.

RPC refusals are classified by JSON-RPC error.code (and HTTP status), never
by message text: 429, -32005 and -32011 are a quota - retried, and if it
persists the read is incomplete; -32601 and -32004 are a method the
provider does not serve - "unsupported-rpc"; -32603 and transport failures
are transient - IncompleteRead. -32000 is geth's catch-all (header not
found, execution reverted, limit exceeded, not supported), so it is read
through a short allowlist: a timeout/try-again marker is transient, a
rate-limit marker is a quota, and anything else under -32000 - a permanent
"header not found" from a pruned endpoint among them - is "rpc-refused",
the bucket that names a config fix. Any other code is "rpc-refused",
an explicit unknown bucket that prints the code and message and exits 2,
so a provider that rewords or invents a refusal is never read as evidence.
eth_getBlockReceipts is probed per RPC; a positive answer holds for the
run, a negative one is probed again after RPC_REPROBE_SECONDS, so a quota
seen at t=0 cannot stamp the whole run. After the probe succeeds, a block
whose receipts come back null is orphaned or pruned - a per-block gap that
heals on the next pass, IncompleteRead.

The paging path is not left to wide windows. For every settled line the
consuming event is also searched for in the blocks around the reported
tx's receipt (CROSS_BLOCKS either side), and the row records `cross_check`:
"agrees" (the search, keyed on the authorizer and the nonce - the fact the
row is about, not the tx hash the receipt was read by - found exactly one
canonical consumer and it is the receipt's tx), "incomplete" (it could not
finish; the row is not final), or "disagrees" (it found no consumer, a
different one, or two) - a reader defect that exits 2, because the same
reader emits every negative verdict. The hash it found is on the row.

A run reports how many rows it actually measured. A row is unmeasured when
the chain was not asked, refused, or answered only part of a window or of
the cross-check. Some unmeasured rows make the run "partial" (exit 2);
none measured makes it "blind" (exit 3) - a reader that can see nothing is
not a clean run. Every row also records what it was judged against: the
endpoint, the head and the finalized height and hash at read time, the
evidence block and its depth. "Final" is relative to a head; with the head
on the row, a later contradiction is computable rather than re-litigable,
and depth and finality can be compared on one line.

Each row carries the verdict of the run it came from and whether it was
measured. With --history=FILE the run appends one record and counts, per
key, the consecutive runs that could not measure it; at STALE_RUNS or more,
or once STALE_SECONDS of wall-clock have passed since it was last measured,
the row says "stale" with that time. Runs are counted by the process being
watched, so the clock rule does not depend on runs happening; the watcher
also checks the history file's own age, and can ping an external dead-man
switch (SATLEDGER_DEADMAN_URL) that alerts when the pings stop. Every
unmeasured row names its cause - quota, transient, refused, unsupported,
unavailable - and the summary counts them, so being rate-limited for an
hour reads as the blindness it is. The dead-man ping carries the run's
verdict and its measured count, so the external layer can tell "ran" from
"measured" too.

Every field on a row is chain-native and re-checkable by anyone with an
RPC URL, except `read_at`, which is this host's clock; the row names it in
`clock_fields`. No verdict - row or endpoint - uses this host's clock. A watcher that
alerts on transitions would otherwise announce a persistent "partial" once
and then fall silent while the same key stays unseen.

Duplicate lines for one key - a client retrying over HTTP after an MCP
timeout writes two - collapse to the earliest, and an unknown line whose key
later settled is resolved by the settlement. One row per key, never per line.

Lines from before 0.21.4 carry no network/asset and cannot be keyed; they
are counted and skipped rather than guessed at.

Usage:
    python scripts/reconcile.py funnel.jsonl
    python scripts/reconcile.py funnel.jsonl --now=2026-11-01T00:00:00Z --pay-to=0x...

pay_to defaults to SATLEDGER_X402_PAY_TO (set in the api container). Without
it the Transfer is not matched and a consumed slot is reported as "charged".
Known facilitator receivers come from SATLEDGER_X402_FACILITATOR_RECEIVERS or
--receivers=0x...,0x... (empty by default: the CDP facilitator settles
transferWithAuthorization straight to payTo).

Fetch the log first:
    ssh root@<vps> 'cd /opt/satledger && docker compose -f deploy/docker-compose.yml \\
        exec -T api cat /var/log/satledger/funnel.jsonl' > funnel.jsonl

Exit: 0 every row measured and nothing pending; 1 something pending,
underpaid or to investigate; 2 the chain could not be asked, refused, or
was only partly read for at least one key, or the reader disagreed with
itself; 3 blind - not one row was measured. Nothing here signs or sends.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

MARKER = "x402-funnel "
# keccak256("authorizationState(address,bytes32)")[:4]
AUTHORIZATION_STATE = "0xe94a0102"
# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# keccak256("AuthorizationUsed(address,bytes32)") / keccak256("AuthorizationCanceled(address,bytes32)")
# Emitted by the EIP-3009 token in the tx that consumes a nonce, with the
# authorizer and the nonce as indexed topics - the on-chain payment reference.
AUTH_USED_TOPIC = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"
AUTH_CANCELED_TOPIC = "0x1cdd46ff242716cdaa72d159d339a485b3438398348d68f09d7c8c0a59353d81"
# A facilitator-reported tx with no receipt yet is pending for this long
# after the line was written; after it, a hash that never mined is a fact.
MEMPOOL_GRACE_SECONDS = 2 * 3600
RPC: Dict[str, str] = {
    "eip155:8453": "https://mainnet.base.org",
    "eip155:84532": "https://sepolia.base.org",
}

Key = Tuple[str, str, str, str]


class ChainUnavailable(Exception):
    pass


class IncompleteRead(ChainUnavailable):
    """The chain answered, but not for the whole window. No verdict may rest on it.
    `cause` says why: quota, transient, or incomplete."""

    def __init__(self, message: str = "", cause: Optional[str] = None):
        super().__init__(message)
        self.cause = cause or "incomplete"


class RpcUnsupported(ChainUnavailable):
    """The configured RPC does not serve a method completeness depends on.
    A configuration fact, not a gap in the evidence: nothing re-reads it."""


class RpcRefused(ChainUnavailable):
    """The RPC refused with an error code this reader does not know. Not
    evidence and not known to be transient: printed with its code."""


class ChainContradiction(ChainUnavailable):
    """The chain, as read, says something that cannot be (two canonical
    consumers of one nonce). A reader or provider defect."""


class RpcError(ChainUnavailable):
    """A JSON-RPC or HTTP-level error, carrying what classification needs."""

    def __init__(self, url: str, code: Optional[int] = None, message: str = "",
                 http_status: Optional[int] = None):
        self.url, self.code, self.message, self.http_status = url, code, message, http_status
        super().__init__("%s: %s%s%s" % (url, "HTTP %s " % http_status if http_status else "",
                                         "code %s " % code if code is not None else "", message))


QUOTA_CODES = {-32005, -32011}
UNSUPPORTED_CODES = {-32601, -32004}
TRANSIENT_CODES = {-32603}
#: -32000 is a catch-all; only these markers make it something other than unknown.
CATCHALL_CODE = -32000
CATCHALL_TRANSIENT = ("timeout", "timed out", "try again", "temporarily", "busy", "connection reset")
CATCHALL_QUOTA = ("rate limit", "limit exceeded", "too many requests", "request limit")


def classify(exc: BaseException) -> str:
    """'quota' | 'unsupported' | 'transient' | 'refused', from code and status only."""
    if not isinstance(exc, RpcError):
        return "transient"          # transport failure: no code to read
    if exc.http_status == 429 or exc.code in QUOTA_CODES:
        return "quota"
    if exc.code in UNSUPPORTED_CODES:
        return "unsupported"
    if exc.code == CATCHALL_CODE:
        text = (exc.message or "").lower()
        if any(m in text for m in CATCHALL_QUOTA):
            return "quota"
        if any(m in text for m in CATCHALL_TRANSIENT):
            return "transient"
        return "refused"
    if exc.code is None or exc.code in TRANSIENT_CODES:
        return "transient"
    return "refused"


# eth_getLogs providers cap the block span per call; a window is read in
# pages that must tile it with no gap before any negative verdict is
# emitted. A page that comes back null, or errors, is a hole, not "empty".
PAGE_BLOCKS = 100
#: Blocks either side of a settled tx searched for its consuming event, so
#: the paging path runs on every settled row and not only on wide windows.
CROSS_BLOCKS = 150
# A Transfer nearer than this to the head can still be reorged away, so a
# "no Transfer" verdict over those blocks is not a verdict yet.
CONFIRMATIONS = 64


def parse_lines(lines: Iterable[str]) -> Tuple[List[Dict[str, Any]], int]:
    """Settlement-bearing records, plus how many could not be keyed."""
    kept, unkeyed = [], 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith(MARKER):
            raw = raw[len(MARKER):]
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if rec.get("event") not in ("settled", "settlement-unknown"):
            continue
        if not all(rec.get(k) for k in ("network", "asset", "payer", "nonce")):
            unkeyed += 1
            continue
        kept.append(rec)
    return kept, unkeyed


def key_of(rec: Dict[str, Any]) -> Key:
    return (rec["network"], rec["asset"].lower(), rec["payer"].lower(), rec["nonce"].lower())


def _ts(rec: Dict[str, Any]) -> datetime:
    return datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))


def group(records: List[Dict[str, Any]]) -> Dict[Key, Dict[str, Dict[str, Any]]]:
    """Per key, the earliest settled line and the earliest unknown line.

    The key names a nonce slot, not a signature: a payer can sign the same
    nonce more than once (a later validBefore, say) and only the first one
    submitted ever settles. So the slot's horizon is the LATEST validBefore
    seen across its unknown lines - closing it on the earliest signature's
    window would call a slot not-charged while a re-signed authorization
    for it can still land. The earliest line still supplies first_seen.
    """
    out: Dict[Key, Dict[str, Dict[str, Any]]] = {}
    for rec in sorted(records, key=_ts):
        slot = out.setdefault(key_of(rec), {})
        if rec["event"] not in slot:
            slot[rec["event"]] = dict(rec)  # earliest wins
            continue
        kept = slot[rec["event"]]
        if rec["event"] == "settlement-unknown":
            later = _int_or_none(rec.get("valid_before"))
            current = _int_or_none(kept.get("valid_before"))
            if later is not None and (current is None or later > current):
                kept["valid_before"] = rec.get("valid_before")
            kept["signatures"] = kept.get("signatures", 1) + 1
    return out


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _rpc(url: str, method: str, params: list) -> Any:
    """One JSON-RPC call; a rate limit (429) is retried with backoff, since the
    receipts oracle makes a read cost more requests than the public RPC's
    free tier likes. Anything else, or a limit that persists, is ChainUnavailable."""
    import time
    for attempt in range(RPC_RETRIES + 1):
        # Paced: the public endpoint limits per IP, and a burst of page reads
        # trips it long before a backoff can recover.
        wait = _LAST_CALL.get(url, 0.0) + RPC_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[url] = time.monotonic()
        try:
            return _rpc_once(url, method, params)
        except ChainUnavailable as exc:
            if attempt == RPC_RETRIES or classify(exc) != "quota":
                raise
            time.sleep(2 ** attempt)
    raise ChainUnavailable("%s: rate limited" % url)


#: Seconds between calls to one RPC, and rate-limit retries (backoff 1, 2, 4 ... s).
RPC_MIN_INTERVAL = 0.25
RPC_RETRIES = 5
_LAST_CALL: Dict[str, float] = {}
#: A negative capability probe is repeated after this long, within one run.
RPC_REPROBE_SECONDS = 60
_FINALIZED: Dict[str, Tuple[float, Optional[int], Optional[str]]] = {}


def finality(url: str) -> Tuple[Optional[int], Optional[str]]:
    """(number, hash) of the node's `finalized` block: on an L2, the last block
    whose batch is in a finalized L1 block. (None, None) if the provider will
    not say - then nothing is final. Cached for 30 s."""
    import time
    hit = _FINALIZED.get(url)
    if hit and time.monotonic() - hit[0] < 30:
        return hit[1], hit[2]
    try:
        header = _rpc(url, "eth_getBlockByNumber", ["finalized", False])
        number = _int_hex((header or {}).get("number")) if isinstance(header, dict) else None
        block_hash = ((header or {}).get("hash") or None) if isinstance(header, dict) else None
    except (ChainUnavailable, ValueError, TypeError, KeyError):
        number, block_hash = None, None
    _FINALIZED[url] = (time.monotonic(), number, block_hash)
    return number, block_hash


def _finalized_head(url: str) -> Optional[int]:
    return finality(url)[0]


#: `finalized` this close to the endpoint's own `latest` is the tag answered as latest.
SMELL_MAX_DEPTH = 2


def _header_tuple(url: str, tag: str) -> Optional[Tuple[int, str, Optional[int]]]:
    try:
        header = _rpc(url, "eth_getBlockByNumber", [tag, False])
    except ChainUnavailable:
        return None
    if not isinstance(header, dict) or _int_hex(header.get("number")) is None:
        return None
    return _int_hex(header.get("number")), (header.get("hash") or "").lower(), _int_hex(header.get("timestamp"))


def endpoint_finality(url: str) -> Optional[Dict[str, Any]]:
    """The endpoint's own `latest`, `safe` and `finalized` headers as (number,
    hash, timestamp), or None for a tag it will not answer."""
    return {tag: _header_tuple(url, tag) for tag in ("latest", "safe", "finalized")}


#: Seconds to wait before re-reading peers when the primary is ahead of all of them.
CROSS_RETRY_SECONDS = 10


def _finalized_contradictions(claims: List[Tuple[str, Optional[str], int, str, Optional[int]]],
                              header_fn) -> Dict[str, Any]:
    """Finalized claims that cannot all lie on one chain, and who vouched for what.

    Each claim is (host, url, finalized height, hash, safe height or None).
    Claims at one height must carry one hash. The highest claim anchors the
    rest, so it is witnessed first: by a second claim at its height (no
    call), or else by an eligible endpoint reporting the same block there.
    Eligible means its own `safe` head has reached the anchor's height, so
    the block it reports is derived from L1 data rather than a sequencer
    head that could still move; asking an endpoint below that would turn
    its lag into a false contradiction. No eligible endpoint and eligible
    endpoints that did not answer are different facts and are reported
    apart. Then each lower claim must match the hash the anchor's chain
    reports at its height: if every claim lies on that one chain they lie
    on one chain together, so one call per lower height covers every pair.
    A height that is not answered is not compared and is returned as such:
    the finding is a contradiction, never an absence.

    Returns {"contradictions": [{"height", "text", "sides": [{"hash", "at",
    "role"}, ...]}], "not_compared": [heights], "witness": {"endpoint",
    "height", "how"} or None, "unwitnessed": reason or None, "anchor":
    {"height", "at"}}."""
    claims = [c for c in claims if c[3]]
    result: Dict[str, Any] = {"contradictions": [], "not_compared": [], "witness": None, "unwitnessed": None,
                              "anchor": None}
    by_height: Dict[int, List[Tuple[str, Optional[str], int, str, Optional[int]]]] = {}
    for c in claims:
        by_height.setdefault(c[2], []).append(c)
    for n, group in sorted(by_height.items()):
        for c in group[1:]:
            if c[3] != group[0][3]:
                result["contradictions"].append({
                    "height": n, "text": "height %d: %s finalized at %s vs %s finalized at %s" % (
                        n, group[0][3][:12], group[0][0], c[3][:12], c[0]),
                    "sides": [{"hash": group[0][3], "at": [group[0][0]], "role": "finalized claim"},
                              {"hash": c[3], "at": [c[0]], "role": "finalized claim"}]})
    if result["contradictions"] or not by_height:
        return result
    top = max(by_height)
    anchors = by_height[top]
    names = [c[0] for c in anchors]
    # With a tie the first claim is the anchor and the second its witness.
    result["anchor"] = {"height": top, "at": names if len(anchors) == 1 else names[:1]}
    if len(anchors) > 1:
        result["witness"] = {"endpoint": anchors[1][0], "height": top, "how": "finalized the same block"}
    else:
        eligible = sorted((c for c in claims if c[2] != top and c[1] and c[4] is not None and c[4] >= top),
                          key=lambda c: -c[4])
        for c in eligible:
            theirs = ((header_fn(c[1], top) or (None, None, None))[1] or "").lower()
            if not theirs:
                continue
            if theirs != anchors[0][3]:
                result["contradictions"].append({
                    "height": top, "text": "height %d: %s finalized at %s (anchor) vs %s at %s (witness, safe %d)" % (
                        top, anchors[0][3][:12], ", ".join(names), theirs[:12], c[0], c[4]),
                    "sides": [{"hash": anchors[0][3], "at": names, "role": "anchor"},
                              {"hash": theirs, "at": [c[0]], "role": "witness"}]})
                return result
            result["witness"] = {"endpoint": c[0], "height": top, "how": "reports the block below its safe head %d" % c[4]}
            break
        else:
            result["unwitnessed"] = ("no other endpoint's safe head had reached %d" % top if not eligible else
                                     "%d endpoint(s) whose safe head had reached %d did not answer" % (len(eligible), top))
    asker = next((c for c in anchors if c[1]), None)
    lower = sorted(n for n in by_height if n != top)
    if asker is None:
        result["not_compared"] = lower
        return result
    # The chain asked is the anchor's, or with a tie its witness's: either way it
    # is vouched for by the other one.
    other = (result["witness"] or {}).get("endpoint")
    if other == asker[0]:
        other = anchors[0][0]
    vouched = "anchor, witnessed by %s" % other if other else "anchor, unwitnessed"
    for n in lower:
        theirs = ((header_fn(asker[1], n) or (None, None, None))[1] or "").lower()
        if not theirs:
            result["not_compared"].append(n)
        elif theirs != by_height[n][0][3]:
            at = [c[0] for c in by_height[n]]
            result["contradictions"].append({
                "height": n, "text": "height %d: %s finalized at %s vs %s on %s's chain (%s)" % (
                    n, by_height[n][0][3][:12], ", ".join(at), theirs[:12], asker[0], vouched),
                "sides": [{"hash": by_height[n][0][3], "at": at, "role": "finalized claim"},
                          {"hash": theirs, "at": [asker[0]], "role": vouched}]})
    return result


def endpoint_crosscheck(fit: Dict[str, Any], peers: List[str], finality_fn=None, header_fn=None,
                        sleep=None, primary_url: Optional[str] = None) -> Dict[str, Any]:
    """Compare the primary's finalized block with peer endpoints; see the module docstring."""
    import time
    finality_fn = finality_fn or endpoint_finality
    header_fn = header_fn or (lambda url, height: _header_tuple(url, hex(height)))
    sleep = sleep or time.sleep
    height, block_hash = fit.get("finalized_height"), (fit.get("finalized_hash") or "").lower()
    out = {"peers": len(peers), "answered": 0, "smell": False, "line": ""}
    if height is None or not peers:
        return out

    def read():
        got = []
        for peer in peers:
            heads = finality_fn(peer) or {}
            fin = heads.get("finalized")
            if fin:
                got.append((_host(peer), peer, fin[0], fin[1],
                            (heads.get("safe") or (None,))[0], (heads.get("latest") or (None,))[0]))
        return got
    seen = read()
    reading = "first read"
    out["answered"] = len(seen)
    if len(seen) < 2:
        out["line"] = "cross-check: %d of %d peers answered `finalized`; no quorum" % (len(seen), len(peers))
        return out
    unstable: List[str] = []
    if height > max(r[2] for r in seen):
        before = {r[1]: r[2] for r in seen}
        sleep(CROSS_RETRY_SECONDS)
        again = read()
        unstable = ["%s (%d -> %d)" % (r[0], before[r[1]], r[2]) for r in again if r[2] < before.get(r[1], -1)]
        answered = {r[1] for r in again}
        # Answered the first read and nothing on the second: not the same
        # finding as an endpoint that answered lower, so named apart.
        unstable += ["%s (%d -> no answer)" % (_host(url), n) for url, n in before.items() if url not in answered]
        seen = [r for r in again if r[2] >= before.get(r[1], -1)]
        reading = "re-read %d s later" % CROSS_RETRY_SECONDS
        out["answered"] = len(seen)
        dropped = "; unstable, dropped: %s" % ", ".join(unstable) if unstable else ""
        if len(seen) < 2:
            # The verdict rests on the reading it names. The first read is
            # not reused: a fallback printed as a re-confirmation is the
            # kind of line this file exists to refuse.
            out["line"] = ("cross-check: finalized %d was ahead of every peer, and %d of %d peers answered the re-read "
                           "stably; no quorum%s" % (height, len(seen), len(peers), dropped))
            return out
    top = max(seen, key=lambda r: r[2])
    triples = "; ".join("%s (%s, %s, %s)" % (r[0], r[2], r[4], r[5]) for r in seen)
    where = "%s; peers (finalized, safe, latest): %s%s" % (
        reading, triples, "; unstable, dropped: %s" % ", ".join(unstable) if unstable else "")
    # Every height comparison below assumes finality's safety half: no chain
    # finalizes two blocks at one height. Ranking heights cannot see that
    # fail - a peer on a minority fork, or one overstating its finalized,
    # wins on height alone - so the hashes are compared first, and a
    # contradiction is the finding instead of any height verdict.
    primary_safe = None
    if fit.get("depth_blocks") is not None and fit.get("safe_depth_blocks") is not None:
        primary_safe = height + fit["depth_blocks"] - fit["safe_depth_blocks"]
    claims = [(fit.get("host") or "primary", primary_url, height, block_hash, primary_safe)]
    claims += [(r[0], r[1], r[2], (r[3] or "").lower(), r[4]) for r in seen]
    hashes = _finalized_contradictions(claims, header_fn)
    contradictions, unasked = [c["text"] for c in hashes["contradictions"]], hashes["not_compared"]
    # The witness travels with every verdict, so an audit can tell one unverified
    # source from two independent ones without reading this file.
    out["witness"], out["contradictions"] = hashes["witness"], hashes["contradictions"]
    chain = ("every finalized hash lies on one chain" if not unasked else
             "no finalized hashes contradict; not compared at height(s) %s" % ", ".join(map(str, unasked)))
    if hashes["anchor"]:
        anchor = "%d at %s" % (hashes["anchor"]["height"], ", ".join(hashes["anchor"]["at"]))
        chain += (", its top block (%s) witnessed by %s" % (anchor, hashes["witness"]["endpoint"]) if hashes["witness"]
                  else "; the top claim (%s) has no second witness: %s, so the chain it anchors is unconfirmed"
                  % (anchor, hashes["unwitnessed"]))
    undetermined = ("cross-check: undetermined - finalized claims contradict (%s): one chain cannot finalize two "
                    "blocks at one height, and every verdict here ranks heights on that assumption, so none is "
                    "given (%s)" % ("; ".join(contradictions), where))
    if height > top[2]:
        tips = [r[5] for r in seen if r[5] is not None]
        # A ladder whose safe is at or below some peer's finalized is stale:
        # the chain has already finalized past it, so it sizes nothing.
        ladders = [r for r in seen if r[4] is not None and r[4] > top[2]]
        if tips and height > max(tips):
            out["smell"] = True
            out["line"] = ("cross-check: finalized %d is above every peer's latest block (%d) - a block no independent "
                           "endpoint has produced (%s)" % (height, max(tips), where))
        elif contradictions:
            out["smell"], out["undetermined"], out["line"] = True, True, undetermined
        elif not ladders:
            out["line"] = ("cross-check: finalized %d leads every peer by %d blocks and no peer gave a `safe` above the "
                           "highest peer `finalized` to size the lead against; not judged; %s (%s)" % (height, height - top[2], chain, where))
        else:
            # The freshest ladder, by `finalized` alone. Ranking by
            # finalized + safe (= 2 * finalized + gap) rewarded a peer for
            # being behind on finalized while current on safe - the inflated
            # gap this rule exists to refuse, back in through the key.
            ref = max(ladders, key=lambda r: r[2])
            depth, gap = height - ref[2], ref[4] - ref[2]
            # Two findings, two numbers: how far ahead of the whole field,
            # and how far into the one ladder that can size it.
            field = "leads the field by %d (highest peer finalized %d at %s); %d into %s's ladder (%d, %d)" % (
                height - top[2], top[2], top[0], depth, ref[0], ref[2], ref[4])
            if depth * 2 > gap:
                out["smell"] = True
                out["line"] = ("cross-check: finalized %d %s, more than half its finalized-to-safe gap of %d - nearer "
                               "its safe than its finalized: a clamp; %s (%s)" % (height, field, gap, chain, where))
            else:
                out["line"] = ("cross-check: finalized %d %s, under half its finalized-to-safe gap of %d - one "
                               "finality step: drift, not a clamp; %s (%s)" % (height, field, gap, chain, where))
        return out
    agree, differ = [], []
    for host, peer, n, h, _, _ in seen:
        if n < height:
            continue                                     # one batch behind: drift, not a vote
        other = h if n == height else (header_fn(peer, height) or (None, None, None))[1]
        if other is None:
            continue
        (agree if (other or "").lower() == block_hash else differ).append(host)
    if differ and len(differ) > len(agree):
        out["smell"] = True
        out["line"] = ("cross-check: at height %d the majority of peers that finalized it report another hash (%s) - "
                       "a fork or a fabricated block" % (height, ", ".join(differ)))
    elif contradictions:
        # A minority that disagrees, or two peers that disagree with each
        # other: not a lag, and not something a vote between them settles.
        out["smell"], out["undetermined"], out["line"] = True, True, undetermined
    else:
        behind = sum(1 for r in seen if r[2] < height)
        out["line"] = ("cross-check: finalized %d agrees with %d peer(s)%s; %s" % (
            height, len(agree), ", %d behind (drift)" % behind if behind else "", chain))
    return out


def endpoint_fitness(network: str, url: str, records: List[Dict[str, Any]], now: datetime,
                     floor_blocks: Optional[int] = None, finality_fn=None) -> Dict[str, Any]:
    """Judge an endpoint's finality tag once, on what it says about itself."""
    finality_fn = finality_fn or endpoint_finality
    host = _host(url)
    tags = finality_fn(url) or {}
    latest, safe, fin = tags.get("latest"), tags.get("safe"), tags.get("finalized")
    runs = [r for r in records if host in r.get("endpoints", {})]
    fit = {"host": host, "network": network, "finalized_height": fin[0] if fin else None,
           "finalized_hash": fin[1] if fin else None, "depth_blocks": None, "depth_s": None,
           "safe_depth_blocks": None, "smell": False, "reason": "",
           "history": ("no history yet (new install or first run)" if not records else
                       "first run through this endpoint in %d recorded runs since %s" % (len(records), records[0].get("at"))
                       if not runs else "%d recorded runs through this endpoint since %s" % (len(runs), runs[0].get("at")))}
    if not fin or not latest:
        fit["reason"] = "does not report %s: no row through it can be final" % ("a finalized head" if not fin else "a latest head")
        return fit
    fit["depth_blocks"] = latest[0] - fin[0]
    if latest[2] is not None and fin[2] is not None:
        fit["depth_s"] = latest[2] - fin[2]          # chain timestamps, both from this endpoint
    if safe:
        fit["safe_depth_blocks"] = latest[0] - safe[0]
    smells = []
    if safe:
        # The ladder must be strictly ordered: finalized < safe < latest.
        if fin[0] > safe[0]:
            smells.append("`finalized` (%d) is shallower than `safe` (%d): an inverted ladder - finalized answered "
                          "from the band between safe and latest, or from another view" % (fin[0], safe[0]))
        elif fin[0] == safe[0]:
            smells.append("`finalized` is block %d, the same height as `safe`%s: the tag is answered as safe, not finality"
                          % (fin[0], "" if fin[1] == safe[1] else " under a different hash"))
        if safe[0] >= latest[0]:
            smells.append("`safe` (%d) is at or above `latest` (%d): one of the two tags is not doing its job"
                          % (safe[0], latest[0]))
    else:
        fit["reason"] = "serves no `safe` tag: the band between safe and latest cannot be checked"
    if fit["depth_blocks"] <= SMELL_MAX_DEPTH:
        smells.append("`finalized` is %d block(s) behind its own `latest`: the tag is answered as latest, not finality"
                      % fit["depth_blocks"])
    if floor_blocks is not None and fit["depth_blocks"] < floor_blocks:
        smells.append("`finalized` is %d blocks behind `latest`, below the configured floor of %d"
                      % (fit["depth_blocks"], floor_blocks))
    if smells:
        fit["smell"], fit["reason"] = True, "; ".join(smells)
    return fit


def _is_final(url: str, block: Optional[int]) -> bool:
    fin = _finalized_head(url) if block is not None else None
    return fin is not None and block is not None and block <= fin


def _rpc_once(url: str, method: str, params: list) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    # A named agent: the public RPC answers 403 to urllib's default one.
    req = urllib.request.Request(url, data=body, headers={
        "content-type": "application/json",
        "user-agent": "satledger-reconcile/1 (+https://satledger.org)",
    })
    status = None
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            doc = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        # The body usually carries the JSON-RPC error (and its code).
        status = exc.code
        try:
            doc = json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001 - no body to read: status is all there is
            raise RpcError(url, None, str(exc.reason), status) from exc
    except Exception as exc:  # noqa: BLE001 - any transport failure is "unavailable"
        try:
            import requests  # the buyer venv has it; urllib's CA store here is old
        except ImportError:
            raise ChainUnavailable("%s: %s" % (url, exc)) from exc
        try:
            resp = requests.post(url, json=json.loads(body), timeout=30)
            status = resp.status_code if resp.status_code >= 400 else None
            doc = resp.json()
        except Exception as exc2:  # noqa: BLE001
            raise ChainUnavailable("%s: %s" % (url, exc2)) from exc2
    if not isinstance(doc, dict):
        raise RpcError(url, None, "non-object answer", status)
    if "error" in doc:
        err = doc["error"] if isinstance(doc["error"], dict) else {"message": str(doc["error"])}
        code = err.get("code")
        raise RpcError(url, code if isinstance(code, int) else None, str(err.get("message", "")), status)
    if status:
        raise RpcError(url, None, "HTTP error without a JSON-RPC error", status)
    return doc["result"]


def authorization_consumed(network: str, asset: str, payer: str, nonce: str) -> bool:
    """authorizationState(payer, nonce) on the token contract. True if spent."""
    url = RPC.get(network)
    if not url:
        raise ChainUnavailable("no RPC configured for %s" % network)
    data = AUTHORIZATION_STATE + payer[2:].lower().rjust(64, "0") + nonce[2:].lower().rjust(64, "0")
    result = _rpc(url, "eth_call", [{"to": asset, "data": data}, "latest"])
    if not isinstance(result, str) or len(result) <= 2:
        # An empty return is a call that reverted or hit no contract - not
        # a "false". The wrong asset address looks exactly like this.
        raise ChainUnavailable("%s returned no data for authorizationState on %s" % (url, asset))
    return int(result, 16) != 0


def _pad(address: str) -> str:
    return "0x" + address[2:].lower().rjust(64, "0")


def transfers_in_logs(logs: List[Dict[str, Any]], asset: str, payer: str) -> List[Dict[str, Any]]:
    """The asset's Transfer events whose `from` is the payer - never the first.

    Deduplicated on (blockHash, logIndex): providers return logs
    outside the requested span - inclusive at both ends, or a wider range on
    a cache hit - so the same log can arrive in two pages, and counted twice
    it would push a split payment's sum past the amount and fabricate a
    confirmed. The identity survives a provider that ignores the range.
    """
    out, seen = [], set()
    for log in logs or []:
        topics = log.get("topics") or []
        if (log.get("address") or "").lower() != asset.lower() or len(topics) < 3:
            continue
        if topics[0].lower() != TRANSFER_TOPIC or topics[1].lower() != _pad(payer):
            continue
        ident = ((log.get("blockHash") or log.get("transactionHash") or "").lower(), str(log.get("logIndex")))
        if ident in seen:
            continue
        seen.add(ident)
        out.append({
            "from": "0x" + topics[1][-40:].lower(),
            "to": "0x" + topics[2][-40:].lower(),
            "value": int(log.get("data") or "0x0", 16),
            "tx": log.get("transactionHash"),
            "log_index": log.get("logIndex"),
            "block_number": log.get("blockNumber"),
            "block_hash": log.get("blockHash"),
        })
    return out


def assert_canonical(url: str, transfers: List[Dict[str, Any]]) -> None:
    """Every log's blockHash must be the canonical hash at its height.

    A pending bucket at the head protects against a reorg that lands after
    the read. It does not protect a log that was already orphaned when it
    was read - head from one source and the page from another, or a reorg
    between the header read and eth_getLogs. One header per distinct block,
    not per log; a mismatch means the log is on a discarded branch and
    nothing may be concluded from it.
    """
    blocks = {}
    for t in transfers:
        if t.get("block_number") and t.get("block_hash"):
            blocks.setdefault(t["block_number"], t["block_hash"])
    for number, claimed in blocks.items():
        header = _rpc(url, "eth_getBlockByNumber", [number, False])
        canonical = (header or {}).get("hash") if isinstance(header, dict) else None
        if not canonical or canonical.lower() != claimed.lower():
            raise IncompleteRead("log at block %d is on a discarded branch (%s is not canonical)"
                                 % (int(number, 16), claimed[:12]))


_block_cache: Dict[Tuple[str, int], int] = {}


def block_at(network: str, unix_ts: int) -> int:
    """The first block at or after a unix time, by binary search on headers."""
    url = RPC[network]
    key = (network, unix_ts)
    if key in _block_cache:
        return _block_cache[key]

    def header(n):
        return _rpc(url, "eth_getBlockByNumber", [hex(n), False])

    latest = header(int(_rpc(url, "eth_blockNumber", []), 16))
    lo, hi = 0, int(latest["number"], 16)
    if int(latest["timestamp"], 16) < unix_ts:
        return hi
    while lo < hi:
        mid = (lo + hi) // 2
        if int(header(mid)["timestamp"], 16) < unix_ts:
            lo = mid + 1
        else:
            hi = mid
    _block_cache[key] = lo
    return lo


def _tiled_logs(url: str, network: str, address: str, topics: list,
                since: int, until: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """eth_getLogs over a time window, in pages that must tile it.

    A page that errors, or comes back null the way some providers answer old
    ranges, is a hole in the search - IncompleteRead - never an empty result.
    """
    head = int(_rpc(url, "eth_blockNumber", []), 16)
    from_block = block_at(network, since)
    to_block = min(block_at(network, until), head)
    return _tiled_blocks(url, address, topics, from_block, to_block, head)


def _tiled_blocks(url: str, address: str, topics: list, from_block: int, to_block: int,
                  head: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """eth_getLogs over a block range in PAGE_BLOCKS pages that must tile it,
    completed from block receipts and checked for strict order."""
    found: List[Dict[str, Any]] = []
    covered_to = from_block - 1
    for start in range(from_block, to_block + 1, PAGE_BLOCKS):
        end = min(start + PAGE_BLOCKS - 1, to_block)
        if start != covered_to + 1:
            raise IncompleteRead("pages do not tile the window at block %d" % start)
        found.extend(_page(url, address, topics, start, end))
        covered_to = end
    if covered_to != to_block:
        raise IncompleteRead("window ends at block %d but the read stopped at %d" % (to_block, covered_to))
    _strictly_increasing(found, "the walk over blocks %d..%d" % (from_block, to_block))
    found = _complete_from_receipts(url, address, topics, found)
    found.sort(key=lambda l: (_block_of(l), _index_of(l)))
    _strictly_increasing(found, "blocks %d..%d after the receipts check" % (from_block, to_block))
    meta = {"from_block": from_block, "to_block": to_block, "head": head,
            "confirmed_depth": to_block <= head - CONFIRMATIONS,
            "finalized": _is_final(url, to_block), "rpc": _host(url),
            "finalized_height": finality(url)[0], "finalized_hash": finality(url)[1]}
    return found, meta


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc or url


def _strictly_increasing(logs: List[Dict[str, Any]], where: str) -> None:
    keys = [(_block_of(l), _index_of(l)) for l in logs]
    for a, b in zip(keys, keys[1:]):
        if b <= a:
            raise IncompleteRead("%s: log at (block %d, index %d) %s - a provider repeated or reordered logs, "
                                 "and a repeat would inflate a sum" % (where, b[0], b[1], "repeated" if a == b else "out of order"))


def _block_of(log: Dict[str, Any]) -> int:
    try:
        return int(log.get("blockNumber"), 16)
    except (TypeError, ValueError):
        return -1


def _index_of(log: Dict[str, Any]) -> int:
    try:
        return int(log.get("logIndex"), 16)
    except (TypeError, ValueError):
        return -1


def _page(url: str, address: str, topics: list, start: int, end: int) -> List[Dict[str, Any]]:
    """One eth_getLogs range, followed past the last block it reached.

    A capped answer is a prefix: it stops at some block B and drops
    everything after. So after a range that returned logs, B+1..end is read
    as its own range, and so on until a tail returns nothing. Every range is
    at most PAGE_BLOCKS and ends at this page's end, so "nothing" is about
    blocks we bounded. The ranges are disjoint; the receipts check
    afterwards repairs a block B that was itself cut part-way. Strict order
    is checked, not assumed.
    """
    try:
        logs = _rpc(url, "eth_getLogs", [{"address": address, "topics": topics,
                                          "fromBlock": hex(start), "toBlock": hex(end)}])
    except ChainUnavailable as exc:
        kind = classify(exc)
        if kind == "unsupported":
            raise RpcUnsupported("eth_getLogs not served: %s" % exc) from exc
        if kind == "refused":
            raise RpcRefused("eth_getLogs blocks %d..%d refused: %s" % (start, end, exc)) from exc
        raise IncompleteRead("blocks %d..%d could not be read (%s): %s" % (start, end, kind, exc), cause=kind) from exc
    if not isinstance(logs, list):
        raise IncompleteRead("blocks %d..%d returned no list (%r)" % (start, end, type(logs).__name__))
    # Providers return logs outside the asked range (inclusive ends, cached
    # wider spans). Those belong to the page that owns their block, which
    # returns them too; kept here they would be counted twice.
    logs = [l for l in logs if start <= _block_of(l) <= end]
    _strictly_increasing(logs, "blocks %d..%d" % (start, end))
    if logs and _block_of(logs[-1]) < end:
        # A capped answer stops at some block: read what follows as its own
        # bounded range (never wider than this page, ending exactly at its end).
        logs = logs + _page(url, address, topics, _block_of(logs[-1]) + 1, end)
    return logs


def _matches(log: Dict[str, Any], address: str, topics: list) -> bool:
    """Does a receipt log satisfy the eth_getLogs filter (address + positional topics)?"""
    if (log.get("address") or "").lower() != address.lower():
        return False
    have = [t.lower() for t in (log.get("topics") or [])]
    for i, want in enumerate(topics or []):
        if want is None:
            continue
        if i >= len(have):
            return False
        if isinstance(want, list):
            if have[i] not in [w.lower() for w in want]:
                return False
        elif have[i] != want.lower():
            return False
    return True


_RECEIPTS_SERVED: Dict[str, Tuple[str, float, str]] = {}


def _require_receipts(url: str) -> None:
    """Probe eth_getBlockReceipts per RPC, by error code.

    "served" holds for the run. "unsupported" (RpcUnsupported) and "refused"
    (RpcRefused, an unknown code) are probed again after RPC_REPROBE_SECONDS,
    so a refusal seen at t=0 does not stamp the whole run. A quota or a
    transient failure is an incomplete read, not a classification.
    """
    import time
    cached = _RECEIPTS_SERVED.get(url)
    if cached:
        verdict, when, why = cached
        if verdict == "served":
            return
        if time.monotonic() - when < RPC_REPROBE_SECONDS:
            raise (RpcUnsupported if verdict == "unsupported" else RpcRefused)(why)
    try:
        answer = _rpc(url, "eth_getBlockReceipts", ["latest"])
    except ChainUnavailable as exc:
        kind = classify(exc)
        if kind in ("unsupported", "refused"):
            why = "%s: eth_getBlockReceipts %s (%s)" % (url, "not served" if kind == "unsupported" else "refused", exc)
            _RECEIPTS_SERVED[url] = (kind, time.monotonic(), why)
            raise (RpcUnsupported if kind == "unsupported" else RpcRefused)(why) from exc
        raise IncompleteRead("eth_getBlockReceipts probe failed (%s): %s" % (kind, exc), cause=kind) from exc
    if not isinstance(answer, list):
        why = "%s answers eth_getBlockReceipts(latest) with %r" % (url, type(answer).__name__)
        _RECEIPTS_SERVED[url] = ("refused", time.monotonic(), why)
        raise RpcRefused(why)
    _RECEIPTS_SERVED[url] = ("served", time.monotonic(), "")


def _complete_from_receipts(url: str, address: str, topics: list,
                            logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Check every block that contributed a log against its receipts.

    eth_getBlockReceipts(blockHash) returns every receipt of the block with
    its logs, bounded by the block, so no log cap applies. Logs the filter
    matches there but the page lacked were dropped by a cap and are taken
    from the receipts; a page log the receipts do not hold means the two
    reads disagree (another branch, a bad answer), which is IncompleteRead.
    """
    by_block: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for log in logs:
        h = (log.get("blockHash") or "").lower()
        if h:
            by_block.setdefault(h, {})[str(log.get("logIndex"))] = log
    out = list(logs)
    if by_block:
        _require_receipts(url)
    for h, have in by_block.items():
        try:
            receipts = _rpc(url, "eth_getBlockReceipts", [h])
        except ChainUnavailable as exc:
            if classify(exc) == "refused":
                raise RpcRefused("block %s: receipts refused: %s" % (h[:12], exc)) from exc
            raise IncompleteRead("block %s: receipts could not be read: %s" % (h[:12], exc)) from exc
        if not isinstance(receipts, list):
            raise IncompleteRead("block %s: receipts not served for this hash - orphaned or pruned; "
                                 "heals on the next pass" % h[:12])
        full = {}
        for receipt in receipts:
            for log in (receipt or {}).get("logs") or []:
                if _matches(log, address, topics):
                    full[str(log.get("logIndex"))] = log
        extra = set(have) - set(full)
        if extra:
            raise IncompleteRead("block %s: the page holds %d log(s) its receipts do not - the reads disagree"
                                 % (h[:12], len(extra)))
        out.extend(full[i] for i in sorted(set(full) - set(have), key=lambda x: int(x, 16)))
    return out


def consuming_event(network: str, asset: str, payer: str, nonce: str,
                    since: int, until: int) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    """(kind, tx, meta): the AuthorizationUsed/Canceled for this nonce in the window.

    kind is "used", "canceled" or None. At most one tx can consume a nonce,
    so a second match is a provider echo and is deduplicated away; two
    different txs would be a contradiction and raise.
    """
    url = RPC.get(network)
    if not url:
        raise ChainUnavailable("no RPC configured for %s" % network)
    logs, meta = _tiled_logs(url, network, asset, _auth_topics(payer, nonce), since, until)
    kind, tx = _consumer_in(url, logs, nonce)
    return kind, tx, meta


def _auth_topics(payer: str, nonce: str) -> list:
    return [[AUTH_USED_TOPIC, AUTH_CANCELED_TOPIC], _pad(payer), nonce.lower()]


def consuming_event_near(network: str, asset: str, payer: str, nonce: str,
                         block: int) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    """The consuming-event search over CROSS_BLOCKS either side of `block` -
    the settled row's cross-check, so the paging code runs on every row."""
    url = RPC.get(network)
    if not url:
        raise ChainUnavailable("no RPC configured for %s" % network)
    head = int(_rpc(url, "eth_blockNumber", []), 16)
    logs, meta = _tiled_blocks(url, asset, _auth_topics(payer, nonce),
                               max(0, block - CROSS_BLOCKS), min(head, block + CROSS_BLOCKS), head)
    kind, tx = _consumer_in(url, logs, nonce)
    return kind, tx, meta


def _consumer_in(url: str, logs: List[Dict[str, Any]], nonce: str) -> Tuple[Optional[str], Optional[str]]:
    """(kind, tx) of the one canonical consumer among these logs."""
    seen: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for log in logs:
        ident = ((log.get("blockHash") or "").lower(), str(log.get("logIndex")))
        seen.setdefault(ident, log)
    if not seen:
        return None, None
    # Two consuming txs is a contradiction only inside one canonical chain.
    # Across a reorg it is the expected artefact: keep the one whose block
    # is canonical, call it undetermined if none is, and reserve
    # "contradiction" for two canonical consumers - which cannot happen.
    # Canonicality is per log, then grouped by tx: the same tx mined on two
    # branches is two logs and one consumer.
    canonical_txs: Dict[str, Dict[str, Any]] = {}
    txs = {(log.get("transactionHash") or "").lower() for log in seen.values()}
    for log in seen.values():
        header = _rpc(url, "eth_getBlockByNumber", [log.get("blockNumber"), False])
        canonical = (header or {}).get("hash") if isinstance(header, dict) else None
        if canonical and canonical.lower() == (log.get("blockHash") or "").lower():
            canonical_txs[(log.get("transactionHash") or "").lower()] = log
    if len(canonical_txs) > 1:
        raise ChainContradiction("nonce %s… consumed in %d canonical txs, which cannot be: %s"
                               % (nonce[:10], len(canonical_txs), ", ".join(sorted(canonical_txs))))
    if not canonical_txs:
        raise IncompleteRead("the consuming event%s for nonce %s… %s on a discarded branch; retry after finality"
                             % ("s" if len(txs) > 1 else "", nonce[:10], "are all" if len(txs) > 1 else "is"))
    tx, log = next(iter(canonical_txs.items()))
    kind = "canceled" if (log.get("topics") or [""])[0].lower() == AUTH_CANCELED_TOPIC else "used"
    return kind, log.get("transactionHash") or tx


def unsolicited_transfers(network: str, asset: str, payer: str, pay_to: str,
                          since: int, until: int) -> List[Dict[str, Any]]:
    """Plain transfers from the payer to payTo in the window - bound to no nonce."""
    url = RPC.get(network)
    if not url:
        raise ChainUnavailable("no RPC configured for %s" % network)
    logs, _meta = _tiled_logs(url, network, asset, [TRANSFER_TOPIC, _pad(payer), _pad(pay_to)], since, until)
    found = transfers_in_logs(logs, asset, payer)
    assert_canonical(url, found)
    return found


def logs_bound_to(logs: List[Dict[str, Any]], asset: str, payer: str, nonce: str
                  ) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """(adjacent, later): the asset's payer-Transfer logs attributable to this
    nonce, and the payer Transfers found later in the slice that are not.

    `adjacent` is the contiguous run of the asset's own Transfer-from-payer
    logs after the nonce's AuthorizationUsed, with logs from other addresses
    skipped, ending at the first asset log that is anything else. `later` is
    every further payer Transfer on the asset before the next authorization
    event - evidence of something, but not of this authorization. None if the
    receipt carries no AuthorizationUsed for (payer, nonce) at all.
    """
    def _idx(log):
        try:
            return int(log.get("logIndex"), 16)
        except (TypeError, ValueError):
            return -1

    def _is_asset(log):
        return (log.get("address") or "").lower() == asset.lower()

    def _is_payer_transfer(log):
        topics = log.get("topics") or []
        return len(topics) >= 3 and topics[0].lower() == TRANSFER_TOPIC and topics[1].lower() == _pad(payer)

    def _is_auth_event(log):
        topics = log.get("topics") or []
        return bool(topics) and topics[0].lower() in (AUTH_USED_TOPIC, AUTH_CANCELED_TOPIC)

    ordered = sorted(logs or [], key=_idx)
    start = None
    for i, log in enumerate(ordered):
        topics = log.get("topics") or []
        if (_is_asset(log) and len(topics) >= 3 and topics[0].lower() == AUTH_USED_TOPIC
                and topics[1].lower() == _pad(payer) and topics[2].lower() == nonce.lower()):
            start = i
            break
    if start is None:
        return None
    adjacent, later, in_run = [], [], True
    for log in ordered[start + 1:]:
        if not _is_asset(log):
            continue  # a hook or proxy emitting between the pair is not a break
        if _is_auth_event(log):
            break
        if _is_payer_transfer(log):
            (adjacent if in_run else later).append(log)
        else:
            in_run = False  # the asset emitted something else: the run is over
    return adjacent, later


def transfers_in_tx(network: str, asset: str, payer: str, tx: str,
                    nonce: Optional[str] = None) -> Optional[Tuple[List[Dict[str, Any]], bool, List[Dict[str, Any]], Dict[str, Any]]]:
    """(the payer's Transfers in one tx bound to `nonce`, nonce_in_tx, later,
    meta), or None if the tx has no receipt yet. meta carries the head and
    whether the receipt's blocks are at confirmed depth.

    None is a distinct fact from an empty list: a hash still in the mempool
    has transferred nothing *yet*; a mined tx with no payer Transfer has
    transferred nothing. With a nonce, only the Transfers that follow that
    nonce's AuthorizationUsed (up to the next authorization event) count -
    a batched tx settling two authorizations attributes each Transfer to its
    own one. nonce_in_tx is False when the receipt carries no
    AuthorizationUsed for the nonce; the transfers returned are then all of
    the payer's, unbound, for the caller to treat as such.
    """
    url = RPC.get(network)
    if not url:
        raise ChainUnavailable("no RPC configured for %s" % network)
    receipt = _rpc(url, "eth_getTransactionReceipt", [tx])
    if receipt is None:
        return None
    if not isinstance(receipt, dict):
        raise ChainUnavailable("%s: unreadable receipt for %s" % (url, tx))
    logs = receipt.get("logs") or []
    nonce_in_tx = True
    later: List[Dict[str, Any]] = []
    if nonce:
        bound = logs_bound_to(logs, asset, payer, nonce)
        if bound is None:
            nonce_in_tx = False
        else:
            logs, later_logs = bound
            later = transfers_in_logs(later_logs, asset, payer)
    found = transfers_in_logs(logs, asset, payer)
    assert_canonical(url, found + later)
    head = int(_rpc(url, "eth_blockNumber", []), 16)
    # The receipt's own block, not its transfers': a tx that moved nothing
    # still has a depth, and "nothing was paid" is only final at depth.
    number = _int_hex(receipt.get("blockNumber"))
    block_hash = (receipt.get("blockHash") or "").lower() or None
    checked = False                      # is block_hash known to be canonical?
    if number is None:
        numbers = [(int(t["block_number"], 16), t.get("block_hash")) for t in found + later if t.get("block_number")]
        if numbers:
            number, block_hash = max(numbers)
            block_hash, checked = (block_hash or "").lower() or None, True   # assert_canonical read it
    final = _is_final(url, number) and block_hash is not None
    if final and not checked:
        # "final" is about this block, not about its height: the finalized
        # chain must report this hash there, or the receipt came from a fork.
        header = _rpc(url, "eth_getBlockByNumber", [hex(number), False])
        canonical = ((header or {}).get("hash") or "").lower() if isinstance(header, dict) else ""
        if canonical != block_hash:
            raise IncompleteRead("the receipt's block %s at height %d is not the finalized chain's (%s)"
                                 % (block_hash[:12], number, canonical[:12] or "none"), cause="transient")
    meta = {"head": head, "block": number, "block_hash": block_hash,
            "confirmed_depth": number is not None and number <= head - CONFIRMATIONS,
            "finalized": final,
            "finalized_height": finality(url)[0], "finalized_hash": finality(url)[1]}
    return found, nonce_in_tx, later, meta


def _int_hex(value: Any) -> Optional[int]:
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return None


def _horizon(rec: Dict[str, Any]) -> Optional[datetime]:
    vb = rec.get("valid_before")
    try:
        return datetime.fromtimestamp(int(vb), tz=timezone.utc) if vb is not None else None
    except (TypeError, ValueError):
        return None


def _idents(transfers: List[Dict[str, Any]], network: str = "") -> List[Tuple[str, str, str, str]]:
    """(network, blockNumber, blockHash, logIndex): the hash makes a slot stable
    across a reorg, where (txHash, logIndex) can come to point at different
    evidence; the number lets a run notice it holds two branches at one
    height; the network keeps the key unique once rows sum across rails."""
    return [(network, str(t.get("block_number") or ""), (t.get("block_hash") or t.get("tx") or "").lower(),
             str(t.get("log_index"))) for t in transfers]


def _groups(transfers: List[Dict[str, Any]]) -> Dict[str, int]:
    """Value per recipient in a run: the facts beside the verdict."""
    out: Dict[str, int] = {}
    for t in transfers:
        out[t["to"]] = out.get(t["to"], 0) + t["value"]
    return out


def _others_note(transfers: List[Dict[str, Any]], to: str) -> str:
    rest = {k: v for k, v in _groups(transfers).items() if k != to.lower()}
    if not rest:
        return ""
    return "; also in this run, to other receivers, not counted: " + ", ".join(
        "%d to %s…" % (v, k[:10]) for k, v in sorted(rest.items()))


def _sum_to(transfers: List[Dict[str, Any]], to: str, amount: Any, network: str = ""):
    """The transfers to `to` whose values sum to the amount, as one match, or None.

    A payment may arrive split - and an attacker can split one into dust -
    so the recorder sums per recipient rather than looking for one Transfer
    of the exact value. The match carries how many parts it took.
    """
    parts = [t for t in transfers if t["to"] == to.lower()]
    if not parts or amount is None:
        return None
    total = sum(t["value"] for t in parts)
    if total < int(amount):
        return None
    # Overpayment is a settled fact with a surplus, not an anomaly: a buyer
    # who rounded up, or paid two invoices in one transfer, must not sit in
    # a state that reads like fraud. The surplus rides on the row.
    return {"to": to.lower(), "value": total, "tx": ", ".join(sorted({t.get("tx") or "?" for t in parts})),
            "parts": len(parts), "surplus": total - int(amount), "logs": _idents(parts, network)}


def _paid_us(transfers: List[Dict[str, Any]], pay_to: Optional[str], amount: Any, network: str = ""):
    """The Transfer(s) that pay this authorization, or None."""
    if not pay_to:
        return None
    return _sum_to(transfers, pay_to, amount, network)


def _paid_receiver(transfers: List[Dict[str, Any]], receivers: List[str], amount: Any, network: str = ""):
    """The Transfer(s) that pay a known facilitator receiver for this amount, or None."""
    for r in receivers or []:
        hit = _sum_to(transfers, r, amount, network)
        if hit:
            return hit
    return None


def _short_group(transfers: List[Dict[str, Any]], pay_to: Optional[str], receivers: List[str], amount: Any):
    """(to, paid) for the known recipient that was paid short, or None.

    Checked after the paying groups: a group here is one that exists and
    does not cover the amount. payTo first, then the receivers."""
    if amount is None:
        return None
    groups = _groups(transfers)
    for to in ([pay_to] if pay_to else []) + list(receivers or []):
        paid = groups.get(to.lower())
        if paid is not None and paid < int(amount):
            return to.lower(), paid
    return None


def _parts_note(match: Dict[str, Any]) -> str:
    note = " in %d transfers" % match["parts"] if match.get("parts", 1) > 1 else ""
    if match.get("surplus"):
        note += " (pays %d against %d: surplus %d)" % (match["value"], match["value"] - match["surplus"], match["surplus"])
    return note


def _window_note(window: Optional[Dict[str, Any]]) -> str:
    if not window:
        return ""
    span = "%s..%s" % (
        datetime.fromtimestamp(window["since"], tz=timezone.utc).isoformat(timespec="seconds"),
        datetime.fromtimestamp(window["until"], tz=timezone.utc).isoformat(timespec="seconds"))
    blocks = ""
    if "from_block" in window:
        blocks = " blocks %d..%d, head %d" % (window["from_block"], window["to_block"], window["head"])
    return " [searched %s%s%s]" % (span, blocks, "; clamped" if window.get("clamped") else "")


def _classify_unmatched(transfers: List[Dict[str, Any]], payer: str, pay_to: Optional[str],
                        receivers: List[str]) -> str:
    """Why a payer Transfer did not match: 'self', 'known-to', or 'unknown-to'.

    A transfer back to the payer's own address can be nobody's payout - it is
    the A/A' case, and evidence of non-payment. A transfer to us or to a
    configured receiver with the wrong value is a value mismatch to look at.
    Only a transfer to an address nobody configured is a configuration gap.
    """
    known = {a.lower() for a in ([pay_to] if pay_to else []) + list(receivers or [])}
    if any(t["to"] == payer.lower() for t in transfers):
        return "self"
    if any(t["to"] in known for t in transfers):
        return "known-to"
    return "unknown-to"


def _describe(transfers: List[Dict[str, Any]]) -> str:
    if not transfers:
        return "no Transfer from the payer found"
    return "; ".join("tx %s to %s… value %d" % ((t.get("tx") or "?")[:12], t["to"][:10], t["value"])
                     for t in transfers[:3])


def judge(slot: Dict[str, Dict[str, Any]], consumed: Optional[bool], now: datetime,
          evidence: Optional[Dict[str, Any]] = None,
          pay_to: Optional[str] = None,
          receivers: Optional[List[str]] = None,
          payer: str = "",
          details: Optional[Dict[str, Any]] = None,
          network: str = "") -> Tuple[str, str]:
    """(state, note) for one key. `details`, if given, receives the structured
    match: paid, surplus, parts, shortfall, the log idents the verdict rests
    on, and `final` - whether the evidence sits at confirmed depth, so that
    an undetermined row says whether anything can still change it.

    `evidence` is what the chain said about the tx that consumed this nonce:
    kind ("used" / "canceled" / None), tx, transfers (None = no receipt yet),
    mined (False when a facilitator-reported hash has no receipt), and the
    window searched. None means it was not gathered (no pay_to, or the slot
    is unconsumed).
    """
    settled = slot.get("settled")
    unknown = slot.get("settlement-unknown")
    ev = evidence or {}
    window = ev.get("window")
    if consumed is None:
        return "unavailable", "chain not asked; not judged"
    amount = (settled or unknown).get("amount")
    searched = _window_note(window)
    # The weakest evidence decides: a deep Transfer in a shallow window, or a
    # deep window around a shallow receipt, is not final.
    depths = [d for d in (ev.get("tx_depth"), (window or {}).get("finalized"), ev.get("cross_depth"))
              if d is not None]
    depth = all(depths) if depths else None
    if details is not None:
        details["final"] = depth
    if depth is not None:
        searched += (" [final: L1-finalized; no reorg of either chain changes it]" if depth
                     else " [not final: not yet L1-finalized (about 20 min on Base); re-read next pass]")

    if not consumed:
        if settled:
            if ev.get("mined") is False:
                written = _ts(settled)
                if (now - written).total_seconds() < MEMPOOL_GRACE_SECONDS:
                    return "pending", "facilitator reported tx %s but it has no receipt yet (mempool); re-judged next pass" % settled.get("tx")
                return "investigate", ("facilitator reported tx %s, which never mined within %d s and the nonce is unconsumed - "
                                       "a distinct fact from a tx that mined and moved nothing" % (settled.get("tx"), MEMPOOL_GRACE_SECONDS))
            if ev.get("transfers") is not None:
                return "investigate", "facilitator reported tx %s, which mined without consuming this nonce" % settled.get("tx")
            return "investigate", "facilitator reported success, nonce not consumed on-chain"
        assert unknown
        horizon = _horizon(unknown)
        if horizon is None:
            return "pending", "no validBefore on the line; cannot close"
        if now < horizon:
            return "pending", "may still land until %s" % horizon.isoformat(timespec="seconds")
        return "closed", "validBefore passed with the nonce unconsumed: not charged"

    # consumed
    if not evidence or (ev.get("kind") is None and ev.get("transfers") is None and window is None):
        if settled:
            return "confirmed", "tx %s (slot consumed; transfer not matched - no pay_to)" % settled.get("tx")
        return "charged", "nonce consumed; terminal - confirm value and recipient by tx (no pay_to to match)"
    if ev.get("kind") == "canceled":
        return "not-paid", "the payer cancelled this authorization on-chain (AuthorizationCanceled, tx %s): the slot is burned and nothing was transferred%s" % (ev.get("tx"), searched)
    transfers = ev.get("transfers")
    if ev.get("kind") is None:
        # consumed, but no consuming event found where we looked
        if window and (window.get("clamped") or not window.get("complete", True)):
            why = ("window clamped to the lookback; the consuming tx may predate it" if window.get("clamped")
                   else "log read incomplete: %s" % window.get("gap", "a page was not read"))
            return "undetermined", "nonce consumed, consuming tx not found, but " + why + searched
        if window and window.get("confirmed_depth") is False:
            return "pending", ("nonce consumed, consuming tx not found yet, but the window reaches within %d blocks of the "
                               "head; re-judged next pass" % CONFIRMATIONS) + searched
        return "undetermined", ("nonce consumed, but no AuthorizationUsed/Canceled for it in the searched window; a plain "
                                "transfer to payTo would not satisfy this invoice (any found is reported once, on the "
                                "payer's unsolicited-transfer row)" + searched)
    if transfers is None:
        return "pending", "the consuming tx %s has no receipt yet; re-judged next pass" % ev.get("tx")
    origin = "tx %s" % ev.get("tx")
    if unknown and not settled:
        origin = "the facilitator's answer was lost, not the payment: " + origin
    elif unknown:
        origin += "; resolves an earlier unknown line"
    if ev.get("nonce_in_tx") is False:
        return "investigate", "the facilitator's tx %s does not carry AuthorizationUsed for this nonce; its transfers are not this authorization's: " % ev.get("tx") + _describe(transfers)
    later = ev.get("later") or []
    unattributed = (" (%d further payer transfer(s) later in the tx, not adjacent to this authorization, not counted)"
                    % len(later)) if later else ""
    paid = _paid_us(transfers, pay_to, amount, network)
    if paid:
        if details is not None:
            details.update(paid=paid["value"], surplus=paid["surplus"], parts=paid["parts"], logs=paid["logs"])
        return "confirmed", (origin + "; Transfer pays us" + _parts_note(paid) + _others_note(transfers, pay_to)
                             + unattributed + searched)
    via = _paid_receiver(transfers, receivers or [], amount, network)
    if via:
        if details is not None:
            details.update(paid=via["value"], surplus=via["surplus"], parts=via["parts"], logs=via["logs"])
        return "routed", (origin + "; Transfer pays the facilitator's receiver %s…%s, payout to us pending" % (via["to"][:10], _parts_note(via))
                          + _others_note(transfers, via["to"]) + unattributed + searched)
    together = transfers + later
    if later and ((pay_to and _sum_to(together, pay_to, amount)) or _paid_receiver(together, receivers or [], amount)):
        return "undetermined", ("the transfers adjacent to this authorization fall short of the amount, and the payer "
                                "transfers that would make it up sit later in the tx, not attributable to this nonce: "
                                + _describe(together) + searched)
    if not transfers:
        return "not-paid", "the tx that consumed this nonce (%s) carries no Transfer from the payer adjacent to its AuthorizationUsed: nothing was paid%s%s" % (ev.get("tx"), unattributed, searched)
    short = _short_group(transfers, pay_to, receivers or [], amount)
    if short:
        to, got = short
        if details is not None:
            details.update(paid=got, shortfall=int(amount) - got,
                           logs=_idents([t for t in transfers if t["to"] == to], network))
        verdict = ("terminal - the evidence is L1-finalized" if depth else
                   "short in what has been read; not terminal until the evidence is L1-finalized")
        return "underpaid", ("the authorization was used and paid short: %d to %s… against %d (shortfall %d); %s"
                             % (got, to[:10], int(amount), int(amount) - got, verdict)
                             + _others_note(transfers, to) + unattributed + searched)
    kind = _classify_unmatched(transfers, payer, pay_to, receivers or [])
    if kind == "self":
        return "not-paid", ("the tx that consumed this nonce transfers to the payer's own address - nobody's payout; "
                            "the authorization served was not paid: " + _describe(transfers) + searched)
    return "unrecognised-receiver", ("the tx that consumed this nonce pays a receiver not configured - a configuration "
                                     "gap, not a verdict on the buyer: " + _describe(transfers) + searched)


LOOKBACK_SECONDS = 7 * 24 * 3600  # when validAfter is zero/absent: "since genesis", bounded


def reconcile(lines: Iterable[str], now: Optional[datetime] = None,
              consumed_fn=None, event_fn=None, tx_fn=None, plain_fn=None,
              pay_to: Optional[str] = None,
              receivers: Optional[List[str]] = None,
              canonical_fn=None, cross_fn=None,
              skip_networks=None, skipped: Optional[Dict[str, int]] = None) -> Tuple[List[Dict[str, Any]], int]:
    now = now or datetime.now(timezone.utc)
    cross_fn = cross_fn or consuming_event_near
    _RECEIPTS_SERVED.clear()
    _FINALIZED.clear()
    # Resolved at call time, so a test can replace the chain by name.
    consumed_fn = consumed_fn or authorization_consumed
    event_fn = event_fn or consuming_event
    tx_fn = tx_fn or transfers_in_tx
    plain_fn = plain_fn or unsolicited_transfers
    records, unkeyed = parse_lines(lines)
    rows: List[Dict[str, Any]] = []
    unsolicited_windows: Dict[Tuple[str, str, str], Tuple[int, int]] = {}
    for key, slot in group(records).items():
        network, asset, payer, nonce = key
        if skip_networks and network in skip_networks:
            # The endpoint failed its own check; its rows are not judged at all.
            if skipped is not None:
                skipped[network] = skipped.get(network, 0) + 1
            continue
        settled = slot.get("settled")
        unknown = slot.get("settlement-unknown")
        evidence: Optional[Dict[str, Any]] = None
        unavailable_reason = ""
        try:
            consumed: Optional[bool] = consumed_fn(network, asset, payer, nonce)
            if pay_to:
                evidence = {"kind": None, "tx": None, "transfers": None, "mined": None, "window": None}
                if settled and settled.get("tx"):
                    result = tx_fn(network, asset, payer, settled["tx"], nonce)
                    transfers, nonce_in_tx, later, txmeta = _unpack_tx(result)
                    if transfers is not None and (nonce_in_tx or not consumed):
                        evidence.update(kind="used" if consumed else None, tx=settled["tx"],
                                        transfers=transfers, mined=True, nonce_in_tx=nonce_in_tx, later=later,
                                        tx_depth=txmeta.get("finalized"), txmeta=txmeta)
                        if consumed and nonce_in_tx and txmeta.get("block") is not None:
                            evidence.update(_cross_check(cross_fn, network, asset, payer, nonce,
                                                         settled["tx"], txmeta["block"]))
                    elif consumed:
                        # Reported hash has no receipt, yet the slot is burned:
                        # something else consumed it. Find that tx.
                        since = int(_ts(settled).timestamp()) - 3600
                        until = int(now.timestamp()) + 600
                        # No receipt, or a receipt that does not consume this
                        # nonce, while the slot is burned: find the real consumer.
                        _gather_event(evidence, event_fn, tx_fn, network, asset, payer, nonce, since, until, clamped=False)
                        evidence["mined"] = evidence.get("tx") is not None
                    else:
                        evidence["mined"] = False
                elif unknown and consumed:
                    # Both ends from the signature. The line's own time is
                    # a late sample of the lower bound, not the bound.
                    until = max(_int_or_none(unknown.get("valid_before")) or 0, int(now.timestamp())) + 600
                    since = _int_or_none(unknown.get("valid_after")) or 0
                    clamped = since <= 0
                    if clamped:
                        since = until - LOOKBACK_SECONDS
                    _gather_event(evidence, event_fn, tx_fn, network, asset, payer, nonce, since, until, clamped)
        except ChainUnavailable as exc:
            consumed = None
            unavailable_reason = str(exc)
            unsupported = ("unsupported-rpc" if isinstance(exc, RpcUnsupported)
                           else "rpc-refused" if isinstance(exc, RpcRefused) else None)
        else:
            unsupported = None
        details: Dict[str, Any] = {}
        state, note = judge(slot, consumed, now, evidence, pay_to, receivers, payer, details, network)
        if state == "unavailable":
            note = unavailable_reason
            if unsupported:
                state = unsupported
        cross = (evidence or {}).get("cross_check")
        if cross:
            note += " [cross-check: %s]" % cross
        first = min(slot.values(), key=_ts)
        signatures = (slot.get("settlement-unknown") or {}).get("signatures", 1)
        if signatures > 1 and state == "pending":
            note += " (%d signed authorizations seen for this nonce; horizon is the latest)" % signatures
        window = (evidence or {}).get("window")
        rows.append({
            "state": state, "network": network, "asset": asset, "payer": payer, "nonce": nonce,
            "path": first.get("path"), "amount": first.get("amount"), "first_seen": first["ts"],
            "lines": len(slot), "note": note, "window": window, "tx": (evidence or {}).get("tx"),
            "paid": details.get("paid"), "surplus": details.get("surplus"), "parts": details.get("parts"),
            "shortfall": details.get("shortfall"), "final": details.get("final"),
            "cross_check": (cross or "").split(":")[0] or None, "cross_tx": (evidence or {}).get("cross_tx"),
            "rpc": _host(RPC.get(network) or "?"),
            **_judged_against(evidence, window),
            "read_at": now.isoformat(timespec="seconds"),
            "clock_fields": ["read_at"],
            "cross_cause": (evidence or {}).get("cross_cause"),
            "logs": details.get("logs") or [],
        })
        if (state == "undetermined" and evidence and evidence.get("kind") is None and window
                and window.get("complete", True) and not window.get("clamped")):
            # An invoice with no consumer in a fully searched window: a
            # candidate for the payer-level unsolicited-transfer report.
            k = (network, asset, payer)
            lo, hi = window["since"], window["until"]
            if k in unsolicited_windows:
                lo, hi = min(lo, unsolicited_windows[k][0]), max(hi, unsolicited_windows[k][1])
            unsolicited_windows[k] = (lo, hi)

    _claim_logs(rows, canonical_fn)
    if pay_to:
        for (network, asset, payer), (lo, hi) in unsolicited_windows.items():
            try:
                plain = plain_fn(network, asset, payer, pay_to, lo, hi)
            except (RpcUnsupported, RpcRefused) as exc:
                rows.append({"state": "rpc-refused" if isinstance(exc, RpcRefused) else "unsupported-rpc",
                             "network": network, "asset": asset, "payer": payer, "nonce": None, "path": None,
                             "amount": None, "first_seen": None, "lines": 0,
                             "note": "the unsolicited-transfer search could not run: %s" % exc, "window": None,
                             "tx": None, "paid": None, "surplus": None, "parts": None, "shortfall": None,
                             "final": None, "cross_check": None, "cross_tx": None,
                             "rpc": _host(RPC.get(network) or "?"), "logs": []})
                plain = []
            except ChainUnavailable:
                plain = []
            if plain:
                rows.append({
                    "state": "unsolicited-transfer", "network": network, "asset": asset, "payer": payer,
                    "nonce": None, "path": None, "amount": None, "first_seen": None, "lines": 0,
                    "note": ("plain transfer(s) from this payer to payTo that consume no nonce and bind to no invoice - "
                             "money that landed outside this ledger, to refund or apply by hand: " + _describe(plain)
                             + " [searched %s..%s]" % (datetime.fromtimestamp(lo, tz=timezone.utc).isoformat(timespec="seconds"),
                                                       datetime.fromtimestamp(hi, tz=timezone.utc).isoformat(timespec="seconds"))),
                    "window": None, "tx": None, "paid": sum(t["value"] for t in plain), "surplus": None,
                    "parts": len(plain), "shortfall": None, "final": None, "cross_check": None,
                    "cross_tx": None, "rpc": _host(RPC.get(network) or "?"),
                    "logs": _idents(plain, network),
                })
    word, _seen, _total = run_word(rows)
    for row in rows:
        row["measured"], row["run"], row["unmeasured_cause"] = measured(row), word, unmeasured_cause(row)
    return rows, unkeyed


def _judged_against(evidence: Optional[Dict[str, Any]], window: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The heads a row was judged against, so a later run can recompute "final"."""
    tm = (evidence or {}).get("txmeta") or {}
    w = window or {}
    block = tm.get("block") if tm.get("block") is not None else w.get("to_block")
    head = tm.get("head") if tm.get("head") is not None else w.get("head")
    return {
        "evidence_block": block, "evidence_blockhash": tm.get("block_hash"), "head": head,
        "depth": (head - block) if isinstance(head, int) and isinstance(block, int) else None,
        "finalized_height": tm.get("finalized_height", w.get("finalized_height")),
        "finalized_hash": tm.get("finalized_hash", w.get("finalized_hash")),
    }


#: Consecutive runs that could not measure a key before the row says "stale".
STALE_RUNS = 4
#: ... or this much wall-clock since it was last measured, whatever the run count.
STALE_SECONDS = 90 * 60


def _age_seconds(later: Optional[str], earlier: Optional[str]) -> Optional[float]:
    try:
        a = datetime.fromisoformat(str(later).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(earlier).replace("Z", "+00:00"))
        return (a - b).total_seconds()
    except (TypeError, ValueError):
        return None


def _key_string(row: Dict[str, Any]) -> Optional[str]:
    if not row.get("nonce"):
        return None
    return "|".join(str(row.get(k) or "") for k in ("network", "asset", "payer", "nonce")).lower()


def apply_history(rows: List[Dict[str, Any]], records: List[Dict[str, Any]], at: str, word: str) -> Dict[str, Any]:
    """Count, per key, the consecutive runs (this one included) that did not
    measure it; mark the row stale at STALE_RUNS. Returns this run's record."""
    record = {"at": at, "run": word, "keys": {}}
    for row in rows:
        key = _key_string(row)
        if key is None:
            continue
        now_measured = bool(row.get("measured"))
        record["keys"][key] = now_measured
        streak, last_seen = (0 if now_measured else 1), (at if now_measured else None)
        for past in reversed(records):
            if key not in past.get("keys", {}):
                break
            if past["keys"][key]:
                last_seen = last_seen or past.get("at")
                break
            if not now_measured:
                streak += 1
        if last_seen is None:
            for past in reversed(records):
                if past.get("keys", {}).get(key):
                    last_seen = past.get("at")
                    break
        first_seen = next((r.get("at") for r in records if key in r.get("keys", {})), at)
        age = _age_seconds(at, last_seen or first_seen) if not now_measured else 0
        row["unmeasured_runs"], row["last_measured"] = streak, last_seen
        row["stale"] = (not now_measured) and (streak >= STALE_RUNS or (age is not None and age > STALE_SECONDS))
        if row["stale"]:
            row["note"] += (" [stale: not measured in the last %d runs (%s); last measured %s]"
                            % (streak, "%d min" % (age // 60) if age is not None else "age unknown", last_seen or "never"))
    return record


def unmeasured_cause(row: Dict[str, Any]) -> Optional[str]:
    """Why the reader did not see what this row needed, or None if it did."""
    if row["state"] == "unsupported-rpc":
        return "unsupported"
    if row["state"] == "rpc-refused":
        return "refused"
    if row["state"] == "unavailable":
        return "unavailable"
    if row.get("cross_check") == "incomplete":
        return row.get("cross_cause") or "incomplete"
    window = row.get("window") or {}
    if window.get("complete", True) is False:
        return window.get("cause") or "incomplete"
    return None


def measured(row: Dict[str, Any]) -> bool:
    """Did the reader actually see what this row needed?"""
    if unmeasured_cause(row) is not None:
        return False
    if row["state"] in ("unavailable", "unsupported-rpc", "rpc-refused"):
        return False
    if row.get("cross_check") == "incomplete":
        return False
    window = row.get("window") or {}
    return window.get("complete", True) is not False


def run_word(rows: List[Dict[str, Any]]) -> Tuple[str, int, int]:
    """("clean" | "partial" | "blind", measured, total)."""
    total = len(rows)
    seen = sum(1 for r in rows if measured(r))
    if total and not seen:
        return "blind", seen, total
    return ("partial" if seen < total else "clean"), seen, total


def _cross_check(cross_fn, network, asset, payer, nonce, tx, block) -> Dict[str, Any]:
    """Search for the consuming event around the settled tx's block and say
    whether the reader agrees with the receipt. None from cross_fn: not run."""
    try:
        found = cross_fn(network, asset, payer, nonce, block)
    except (RpcUnsupported, RpcRefused):
        raise
    except ChainContradiction as exc:
        return {"cross_check": "disagrees: %s" % exc, "cross_depth": False}
    except ChainUnavailable as exc:
        cause = getattr(exc, "cause", None) or classify(exc)
        return {"cross_check": "incomplete: %s" % exc, "cross_depth": False, "cross_cause": cause}
    if found is None:
        return {}
    kind, cross_tx, meta = found
    depth = (meta or {}).get("finalized")
    if kind is None:
        return {"cross_check": "disagrees: the log search around block %d missed the consuming event the receipt holds" % block,
                "cross_depth": False}
    if (cross_tx or "").lower() != (tx or "").lower():
        return {"cross_check": "disagrees: the log search found consumer %s, the receipt is %s" % (cross_tx, tx),
                "cross_depth": False}
    return {"cross_check": "agrees", "cross_depth": depth, "cross_tx": cross_tx}


def _unpack_tx(result):
    """tx_fn may return None (no receipt), a list (legacy: nonce assumed present,
    nothing later), (list, nonce_in_tx), (list, nonce_in_tx, later) or
    (list, nonce_in_tx, later, meta)."""
    if result is None:
        return None, True, [], {}
    if isinstance(result, tuple):
        later = list(result[2]) if len(result) > 2 else []
        meta = dict(result[3]) if len(result) > 3 and result[3] else {}
        return result[0], bool(result[1]), later, meta
    return result, True, [], {}


def canonical_at(network: str, number: str) -> Tuple[Optional[str], int]:
    """(canonical block hash at that height, finalized head) - for settling a
    two-branch claim. A finalized head of -1 means the provider will not say."""
    url = RPC.get(network)
    if not url:
        raise ChainUnavailable("no RPC configured for %s" % network)
    fin = _finalized_head(url)
    header = _rpc(url, "eth_getBlockByNumber", [number, False])
    return ((header or {}).get("hash") or "").lower() or None, (fin if fin is not None else -1)


def _claim_logs(rows: List[Dict[str, Any]], canonical_fn=None) -> None:
    """One log may support one verdict, and one height may hold one branch.

    Identity is (network, blockNumber, blockHash, logIndex). Two rows resting
    on one identity are both undetermined - one payment cannot settle two
    invoices. Two identities that differ only in blockHash are the same slot
    read from two branches; a run holding both cannot say which branch it
    read near the head, so every row resting on either is undetermined; once
    the height is L1-finalized the chain has answered, and only rows
    resting on the orphan lose their verdict. Per-row predicates cannot see
    either collision, so both are checked across the run."""
    canonical_fn = canonical_fn or canonical_at
    owners: Dict[Tuple[str, str, str, str], List[int]] = {}
    heights: Dict[Tuple[str, str, str], Dict[str, List[int]]] = {}
    for i, row in enumerate(rows):
        if row["state"] in ("confirmed", "routed"):
            for ident in row.get("logs") or []:
                owners.setdefault(ident, []).append(i)
                network, number, block_hash, log_index = ident
                heights.setdefault((network, number, log_index), {}).setdefault(block_hash, []).append(i)
    for ident, idxs in owners.items():
        if len(idxs) > 1:
            names = ", ".join("%s…" % (rows[i]["nonce"] or "?")[:10] for i in idxs)
            for i in idxs:
                rows[i]["state"] = "undetermined"
                rows[i]["note"] = ("the same Transfer (block %s, log %s) would settle %d rows (%s); one payment cannot "
                                   "settle two invoices, so none of them is confirmed by it" % (ident[2][:12], ident[3], len(idxs), names))
    for (network, number, log_index), branches in heights.items():
        if len(branches) <= 1:
            continue
        try:
            canonical, finalized = canonical_fn(network, number)
        except ChainUnavailable:
            canonical, finalized = None, -1
        settled = (canonical in branches and _int_hex(number) is not None
                   and _int_hex(number) <= finalized)
        for block_hash, idxs in branches.items():
            if settled and block_hash == canonical:
                continue
            for i in idxs:
                rows[i]["state"] = "undetermined"
                rows[i]["final"] = False
                if settled:
                    rows[i]["note"] = ("rested on block %s at height %s, which is orphaned: the canonical block there is %s, "
                                       "L1-finalized; this evidence is gone, the next pass reads the canonical one"
                                       % (block_hash[:12], number, canonical[:12]))
                else:
                    rows[i]["note"] = ("log %s at height %s was read under %d block hashes in this run (%s), near the head; "
                                       "the chain has not yet said which is canonical - re-read next pass"
                                       % (log_index, number, len(branches), ", ".join(h[:12] for h in branches)))


def _gather_event(evidence, event_fn, tx_fn, network, asset, payer, nonce, since, until, clamped):
    """Fill evidence with the consuming event and its tx's transfers, or the gap."""
    window = {"since": since, "until": until, "clamped": clamped, "complete": True}
    evidence["window"] = window
    try:
        kind, tx, meta = event_fn(network, asset, payer, nonce, since, until)
    except IncompleteRead as exc:
        # The chain answered for part of the window. That is "not searched",
        # the same bucket as a clamp - never "no event".
        window["complete"], window["gap"], window["cause"] = False, str(exc), exc.cause
        return
    window.update(meta or {})
    evidence["kind"], evidence["tx"] = kind, tx
    if tx and kind == "used":
        try:
            transfers, nonce_in_tx, later, txmeta = _unpack_tx(tx_fn(network, asset, payer, tx, nonce))
            evidence["transfers"], evidence["nonce_in_tx"], evidence["later"] = transfers, nonce_in_tx, later
            if txmeta.get("finalized") is not None:
                evidence["tx_depth"] = txmeta["finalized"]
            evidence["txmeta"] = txmeta
        except IncompleteRead as exc:
            window["complete"], window["gap"], window["cause"] = False, str(exc), exc.cause


def main(argv: List[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    now = None
    import os
    pay_to = os.environ.get("SATLEDGER_X402_PAY_TO") or None
    history = None
    receivers = [r.strip() for r in (os.environ.get("SATLEDGER_X402_FACILITATOR_RECEIVERS") or "").split(",") if r.strip()]
    # SATLEDGER_RECONCILE_RPC="eip155:8453=https://...,eip155:84532=https://..." - which endpoint
    # reads the chain is a deploy decision: mainnet.base.org refuses eth_getLogs past a small
    # per-IP quota, and the paging and receipts checks need an endpoint that serves both.
    for pair in (os.environ.get("SATLEDGER_RECONCILE_RPC") or "").split(","):
        net, _, url = pair.strip().partition("=")
        if net and url:
            RPC[net] = url
    for flag in argv[1:]:
        if flag.startswith("--now="):
            now = datetime.fromisoformat(flag.split("=", 1)[1].replace("Z", "+00:00"))
        if flag.startswith("--rpc="):
            net, _, url = flag[len("--rpc="):].partition("=")
            RPC[net] = url
        if flag.startswith("--pay-to="):
            pay_to = flag.split("=", 1)[1]
        if flag.startswith("--history="):
            history = flag.split("=", 1)[1]
        if flag.startswith("--receivers="):
            receivers = [r.strip() for r in flag.split("=", 1)[1].split(",") if r.strip()]
    if not args:
        print(__doc__)
        return 2
    with open(args[0], encoding="utf-8") as handle:
        lines = list(handle)
    records: List[Dict[str, Any]] = []
    if history:
        try:
            with open(history, encoding="utf-8") as handle:
                records = [json.loads(l) for l in handle if l.strip()][-200:]
        except FileNotFoundError:
            pass
    when = now or datetime.now(timezone.utc)

    # One judgement per endpoint, before any row.
    floors = {}
    for pair in (os.environ.get("SATLEDGER_FINALITY_FLOOR") or "").split(","):
        net, _, blocks = pair.strip().partition("=")
        if net and blocks:
            floors[net] = int(blocks)          # blocks, not seconds: no clock in the verdict
    networks = sorted({rec.get("network") for rec in parse_lines(lines)[0] if rec.get("network")})
    fitness = [endpoint_fitness(net, RPC[net], records, when, floors.get(net)) for net in networks if RPC.get(net)]
    samples: Dict[str, List[str]] = {}
    for pair in (os.environ.get("SATLEDGER_FINALITY_SAMPLE") or "").split(","):
        net, _, urls = pair.strip().partition("=")
        if net and urls:
            samples[net] = [u.strip() for u in urls.split("|") if u.strip()]
    for f in fitness:
        peers = [u for u in samples.get(f["network"], []) if _host(u) != f["host"]]
        if f["smell"] or not peers:
            f["cross"] = "" if f["smell"] else "single endpoint: a clamp just below an honest safe is not checkable"
            continue
        cross = endpoint_crosscheck(f, peers, primary_url=RPC[f["network"]])
        f["cross"], f["witness"] = cross["line"], cross.get("witness")
        if cross["smell"]:
            f["smell"], f["reason"] = True, cross["line"]
    smelly = {f["network"] for f in fitness if f["smell"]}
    skipped: Dict[str, int] = {}
    rows, unkeyed = reconcile(lines, now=now, pay_to=pay_to, receivers=receivers,
                              skip_networks=smelly, skipped=skipped)

    word, seen, total = run_word(rows)
    total += sum(skipped.values())
    if total and not seen:
        word = "blind"
    elif seen < total:
        word = "partial"
    stale = 0
    if history:
        at = when.isoformat(timespec="seconds")
        record = apply_history(rows, records, at, word)
        record["endpoints"] = {f["host"]: {"depth_blocks": f["depth_blocks"], "depth_s": f["depth_s"],
                                           "safe_depth_blocks": f["safe_depth_blocks"],
                                           "finalized_height": f["finalized_height"], "smell": f["smell"],
                                           "witness": f.get("witness")}
                               for f in fitness}
        with open(history, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        stale = sum(1 for r in rows if r.get("stale"))
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    for row in rows:
        judged = ""
        if row.get("evidence_block") is not None:
            judged = "  block %s depth %s finalized %s" % (row["evidence_block"], row.get("depth"), row.get("finalized_height"))
        print("%-21s %s %s  %s  payer %s…  nonce %s…  first %s  lines %d  via %s%s  %s" % (
            row["state"], row["network"], row["path"], row["amount"], row["payer"][:10],
            (row["nonce"] or "-")[:10], (row["first_seen"] or "-")[:19], row["lines"], row.get("rpc"), judged, row["note"]))
    print()
    for f in fitness:
        print("endpoint %s (%s): finalized %s, %s blocks (%s s chain time) behind latest, safe %s behind; %s%s%s" % (
            f["host"], f["network"], f["finalized_height"], f["depth_blocks"], f["depth_s"], f["safe_depth_blocks"],
            f["history"], ("; " + f["cross"]) if f.get("cross") and not f["smell"] else "",
            ("; SMELL: %s - %d row(s) not judged" % (f["reason"], skipped.get(f["network"], 0))) if f["smell"]
            else ("; " + f["reason"] if f["reason"] else "")))
    causes: Dict[str, int] = {}
    for row in rows:
        c = row.get("unmeasured_cause")
        if c:
            causes[c] = causes.get(c, 0) + 1
    print("keys: %d  %s  unkeyed lines skipped: %d  run: %s, measured %d of %d%s%s" % (
        len(rows), "  ".join("%s=%d" % kv for kv in sorted(counts.items())), unkeyed, word, seen, total,
        " (unmeasured: %s)" % ", ".join("%s %d" % kv for kv in sorted(causes.items())) if causes else "",
        ", stale %d" % stale if stale else ""))
    if word == "blind":
        return 3
    if smelly or counts.get("unavailable") or counts.get("undetermined") or counts.get("unsupported-rpc") \
            or counts.get("rpc-refused") or word == "partial" or stale \
            or any(row.get("cross_check") == "disagrees" for row in rows):
        return 2
    if counts.get("pending") or counts.get("investigate") or counts.get("not-paid") or counts.get("underpaid") \
            or counts.get("unrecognised-receiver") or counts.get("unsolicited-transfer"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
