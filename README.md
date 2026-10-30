# x402-reconcile

Reconcile an x402 seller's settlement records against the chain.

An x402 seller usually learns that it was paid from its facilitator's answer. This tool checks that answer against the chain. For each authorization the seller served, it asks the token contract whether the EIP-3009 nonce was consumed. It then finds the transaction that consumed it, pairs the Transfer that the authorization caused, and checks that `to` and `value` match what was served. Every row gets a verdict, unless the evidence read was not enough. Then the row says that instead.

It is one Python file with no dependencies beyond the standard library. It never signs or sends anything.

This is the reconciler that runs in production at [satledger](https://satledger.org). It was reviewed in public, one round after another, on [parthod0x/satroot#2](https://github.com/parthod0x/satroot/issues/2). The module docstring at the top of [`scripts/reconcile.py`](scripts/reconcile.py) is the specification. It is long because each paragraph records a failure someone pointed out.

## What it reads

A log of JSON lines, one per settlement event. An optional `x402-funnel ` prefix is stripped. Only two events are read:

```json
{"ts": "2026-10-21T09:12:03+00:00", "event": "settled", "path": "/x402/topup", "amount": "500000",
 "network": "eip155:8453", "asset": "0x8335…2913", "payer": "0x…", "nonce": "0x…", "tx": "0x…"}
{"ts": "…", "event": "settlement-unknown", "amount": "…", "network": "…", "asset": "…", "payer": "…",
 "nonce": "0x…", "valid_after": "1793000000", "valid_before": "1793000600"}
```

`settled` is a settlement the facilitator confirmed, with its transaction hash. `settlement-unknown` is one whose answer was lost, for example by a timeout. Each authorization is keyed by `(network, asset, payer, nonce)`, because EIP-3009 scopes nonces to the token contract. A line missing any of those four fields is counted and skipped, not guessed at.

## Running it

```
python scripts/reconcile.py funnel.jsonl --pay-to=0xYourPayTo
python scripts/reconcile.py funnel.jsonl --pay-to=0x… --history=reconcile-history.jsonl --rpc=eip155:8453=https://…
```

The settings come from the environment. The names carry satledger's prefix, because this is the production file unchanged.

| Variable | Meaning |
| --- | --- |
| `SATLEDGER_X402_PAY_TO` | your payTo address, or pass `--pay-to=` |
| `SATLEDGER_X402_FACILITATOR_RECEIVERS` | addresses your facilitator may route through; a payment into one is `routed`, not unpaid |
| `SATLEDGER_RECONCILE_RPC` | `net=url,…`; the endpoint must serve both `eth_getLogs` and `eth_getBlockReceipts` |
| `SATLEDGER_FINALITY_SAMPLE` | `net=url\|url\|…`, peer endpoints whose `finalized` block the primary is compared against |
| `SATLEDGER_FINALITY_FLOOR` | `net=blocks`, an optional minimum finality depth for a chain whose `safe` tag is itself late |

The endpoint matters. On 28 October 2026, `mainnet.base.org` refused `eth_getLogs` from a server IP with `-32011 request limit reached`. `1rpc.io/base` caps log ranges at 50 blocks and refuses block receipts. `base-rpc.publicnode.com` served both.

## What it prints

One line per authorization: its state, the endpoint that judged it, its evidence block, depth and finalized height, and a note saying why. Then a line per endpoint, and a summary line:

```
confirmed  eip155:8453 /x402/provision  500000  payer 0x…  …  via base-rpc.publicnode.com  block 53107038 depth 103007 finalized 53209521  tx 0x…; Transfer pays us [final: L1-finalized; …] [cross-check: agrees]
endpoint base-rpc.publicnode.com (eip155:8453): finalized 53274399, 677 blocks (1354 s chain time) behind latest, safe 51 behind; …; cross-check: finalized 53274399 agrees with 1 peer(s), 3 behind (drift)
keys: 5  confirmed=5  unkeyed lines skipped: 7  run: clean, measured 5 of 5
```

The possible states:

| State | Meaning |
| --- | --- |
| `confirmed` | paid to payTo |
| `routed` | paid into a facilitator receiver |
| `underpaid` | paid short |
| `not-paid` | the nonce was consumed without paying you |
| `unrecognised-receiver` | paid an address nobody configured |
| `investigate` | the facilitator's report and the chain disagree |
| `pending` | not settled yet, may still land |
| `closed` | the authorization expired unused, not charged |
| `undetermined` | not provable from the evidence read |
| `unavailable` | the chain was not asked |
| `unsupported-rpc` | the endpoint lacks a method this needs |
| `rpc-refused` | an error code with no known meaning |
| `unsolicited-transfer` | a plain transfer to you that settles no invoice |

`final` means the evidence block is at or below the endpoint's L1-finalized head and carries that chain's hash at that height. A run that could measure nothing says `blind`, not `clean`.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | every row measured and settled |
| 1 | something a human should look at: pending, underpaid, not paid, investigate |
| 2 | the chain could not be asked, refused, or was only partly read, or the reader disagreed with itself |
| 3 | blind: not one row was measured |

## What it cannot see

These limits are stated in the docstring and repeated here:

- a majority of peer endpoints agreeing on the same wrong `finalized` block;
- peers that are one backend under several names;
- a deployment with only one source of chain data;
- a provider that returns an ordered log answer with a block missing from the middle.

## Tests

```
pip install pytest
python -m pytest tests -q
```

The tests run fully offline: a fixture refuses any call that would reach a network. Each test is named for the failure it pins.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
