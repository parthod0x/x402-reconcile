"""The reconciler's state machine, from parthod0x/satroot#2, pinned offline.

Evidence is bound to the nonce: the tx that consumed it is found through
AuthorizationUsed/AuthorizationCanceled(payer, nonce), and that tx's
transfers are what gets matched. The chain is a pair of fakes here - one
answering "which tx consumed this nonce", one answering "what did that tx
transfer" - and what is under test is the judgement: the three-way split
between judged-good, judged-bad and not-judged, dedupe, tiling, depth, and
the boundary between runs that minia2auk kept pointing the next test at.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest  # noqa: E402

import reconcile  # noqa: E402

_REAL_RPC = reconcile._rpc   # captured before the autouse fixture stubs it
_REAL_NEAR = reconcile.consuming_event_near


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing here may reach a chain. A test that wants a reader injects a fake;
    the module-level defaults are stubbed so an un-injected path cannot leak
    out to the public RPC (which once cost a 28-second 'offline' run)."""
    def refuse(*a, **k):
        raise AssertionError("a test reached the network-facing default: %r" % (a[:1],))
    monkeypatch.setattr(reconcile, "_rpc", refuse)
    monkeypatch.setattr(reconcile, "unsolicited_transfers", lambda *a, **k: [])
    # the settled-row cross-check is off unless a test asks for it
    monkeypatch.setattr(reconcile, "consuming_event_near", lambda *a, **k: None)
    monkeypatch.setattr(reconcile, "endpoint_finality", lambda url: None)
    reconcile._RECEIPTS_SERVED.clear()


NET, ASSET, PAYER = "eip155:8453", "0x" + "aa" * 20, "0x" + "bb" * 20
PAY_TO = "0x" + "ee" * 20
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
PAST = str(int(datetime(2026, 10, 21, 11, 0, tzinfo=timezone.utc).timestamp()))
FUTURE = str(int(datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc).timestamp()))
VA = str(int(datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc).timestamp()))
DEEP = {"from_block": 100, "to_block": 900, "head": 1000, "confirmed_depth": True, "finalized": True}


def _line(event, nonce, ts, **extra):
    rec = {"ts": ts, "event": event, "path": "/x402/provision", "amount": "500000",
           "network": NET, "asset": ASSET, "payer": PAYER, "nonce": nonce}
    rec.update(extra)
    return "x402-funnel " + json.dumps(rec)


def _transfer(to, value, tx, index=1, block=500):
    return {"from": PAYER.lower(), "to": to.lower(), "value": value, "tx": tx,
            "log_index": hex(index), "block_number": hex(block), "block_hash": "0xcanon%x" % block}


def _chain(consumed=True, kind="used", tx="0xcons", transfers=None, meta=None, receipt_null=False):
    """A fake chain: the nonce was consumed (or not) by `tx` of `kind`, which moved `transfers`."""
    def consumed_fn(network, asset, payer, nonce):
        return consumed

    def event_fn(network, asset, payer, nonce, since, until):
        return (kind, tx, dict(meta or DEEP)) if kind else (None, None, dict(meta or DEEP))

    def tx_fn(network, asset, payer, txh, nonce=None):
        if receipt_null:
            return None
        return list(transfers or [])

    def plain_fn(network, asset, payer, pay_to, since, until):
        return []
    return dict(consumed_fn=consumed_fn, event_fn=event_fn, tx_fn=tx_fn, plain_fn=plain_fn)


def _by_nonce(lines, now=NOW, **kw):
    rows, _ = reconcile.reconcile(lines, now=now, **kw)
    return {r["nonce"]: r for r in rows}


# ---- the machine, state by state ------------------------------------------

def test_unconsumed_slots_pend_and_close_on_the_latest_window():
    n = {k: "0x" + ("%02x" % i) * 32 for i, k in enumerate(("pending", "closed", "nohorizon"), start=1)}
    lines = [
        _line("settlement-unknown", n["pending"], "2026-10-21T10:03:00+00:00", valid_before=FUTURE),
        _line("settlement-unknown", n["closed"], "2026-10-21T10:04:00+00:00", valid_before=PAST),
        _line("settlement-unknown", n["nohorizon"], "2026-10-21T10:05:00+00:00"),
    ]
    rows = _by_nonce(lines, pay_to=PAY_TO, **_chain(consumed=False, kind=None))
    assert {k: rows[v]["state"] for k, v in n.items()} == {"pending": "pending", "closed": "closed", "nohorizon": "pending"}
    assert "not charged" in rows[n["closed"]]["note"]


def test_a_settled_line_whose_tx_pays_us_is_confirmed():
    nonce = "0x" + "11" * 32
    rows = _by_nonce([_line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xfac")], pay_to=PAY_TO,
                     **_chain(transfers=[_transfer(PAY_TO, 500000, "0xfac")]))
    assert rows[nonce]["state"] == "confirmed" and "Transfer pays us" in rows[nonce]["note"]


def test_a_settled_line_whose_nonce_is_unconsumed_is_investigated_after_the_mempool_grace():
    """A reported hash with no receipt is pending, then a fact of its own (minia2auk)."""
    nonce = "0x" + "12" * 32
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xghost")
    rows = _by_nonce([line], now=datetime(2026, 10, 21, 10, 30, tzinfo=timezone.utc), pay_to=PAY_TO,
                     **_chain(consumed=False, kind=None, receipt_null=True))
    assert rows[nonce]["state"] == "pending" and "mempool" in rows[nonce]["note"]
    rows = _by_nonce([line], now=datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc), pay_to=PAY_TO,
                     **_chain(consumed=False, kind=None, receipt_null=True))
    assert rows[nonce]["state"] == "investigate" and "never mined" in rows[nonce]["note"]
    # mined but did not consume the nonce: investigate, not pending
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(consumed=False, kind=None, transfers=[]))
    assert rows[nonce]["state"] == "investigate" and "without consuming" in rows[nonce]["note"]


def test_a_reported_hash_with_no_receipt_but_a_burned_slot_follows_the_real_consumer():
    """The facilitator named one hash; another tx consumed the nonce. Judge that one."""
    nonce = "0x" + "13" * 32
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xstale")
    calls = []

    def tx_fn(network, asset, payer, txh, nonce=None):
        calls.append(txh)
        return None if txh == "0xstale" else [_transfer(PAY_TO, 500000, "0xreal")]
    chain = _chain(kind="used", tx="0xreal")
    rows = _by_nonce([line], pay_to=PAY_TO, consumed_fn=chain["consumed_fn"], event_fn=chain["event_fn"], tx_fn=tx_fn)
    assert rows[nonce]["state"] == "confirmed" and rows[nonce]["tx"] == "0xreal", rows[nonce]
    assert calls == ["0xstale", "0xreal"]


def test_could_not_ask_is_its_own_state_not_a_verdict():
    nonce = "0x" + "77" * 32

    def down(*a):
        raise reconcile.ChainUnavailable("rpc down")
    rows = _by_nonce([_line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_before=PAST)],
                     pay_to=PAY_TO, consumed_fn=down)
    assert rows[nonce]["state"] == "unavailable" and "rpc down" in rows[nonce]["note"]


# ---- bound evidence -------------------------------------------------------

def test_consumed_is_not_the_same_as_paid():
    """A' (to self, value 1) burns the slot A was served on. Not charged - not paid."""
    nonce = "0x" + "a1" * 32
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer(PAYER, 1, "0xselfpay")]))
    assert rows[nonce]["state"] == "not-paid" and "0xselfpay" in rows[nonce]["note"], rows[nonce]
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer(PAY_TO, 500000, "0xpaidus")]))
    assert rows[nonce]["state"] == "confirmed" and "answer was lost" in rows[nonce]["note"]


def test_a_cancelled_authorization_is_a_verdict():
    nonce = "0x" + "a2" * 32
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(kind="canceled", tx="0xcancel"))
    assert rows[nonce]["state"] == "not-paid" and "cancelled" in rows[nonce]["note"], rows[nonce]


def test_one_transfer_cannot_satisfy_two_invoices():
    """Two open invoices, one payment: only the nonce it consumed is paid (minia2auk)."""
    n1, n2 = "0x" + "a3" * 32, "0x" + "a4" * 32
    lines = [_line("settlement-unknown", n, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE) for n in (n1, n2)]
    one_transfer = [_transfer(PAY_TO, 500000, "0xonce")]

    def event_fn(network, asset, payer, nonce, since, until):
        return ("used", "0xonce", dict(DEEP)) if nonce == n1 else (None, None, dict(DEEP))

    def tx_fn(network, asset, payer, txh, nonce=None):
        return list(one_transfer)
    rows = _by_nonce(lines, pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=event_fn, tx_fn=tx_fn)
    assert rows[n1]["state"] == "confirmed"
    assert rows[n2]["state"] == "undetermined", "the same transfer satisfied a second invoice: " + str(rows[n2])
    assert "no AuthorizationUsed" in rows[n2]["note"]


def test_without_a_pay_to_the_old_verdict_stands_and_says_why():
    nonce = "0x" + "a5" * 32
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_before=FUTURE)

    def must_not(*a):
        raise AssertionError("the chain must not be asked for transfers without a pay_to")
    rows = _by_nonce([line], consumed_fn=lambda *a: True, event_fn=must_not, tx_fn=must_not)
    assert rows[nonce]["state"] == "charged" and "no pay_to" in rows[nonce]["note"]


def test_a_payment_into_the_facilitators_receiver_is_routed_not_fraud():
    nonce = "0x" + "b2" * 32
    vault = "0x" + "fa" * 20
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    rows = _by_nonce([line], pay_to=PAY_TO, receivers=[vault], **_chain(transfers=[_transfer(vault, 500000, "0xbatch")]))
    assert rows[nonce]["state"] == "routed", rows[nonce]
    # the same transfer with no receiver configured is a configuration gap, never a verdict on the buyer
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer(vault, 500000, "0xbatch")]))
    assert rows[nonce]["state"] == "unrecognised-receiver"


def test_known_receiver_paid_short_is_underpaid_and_overpayment_confirms():
    nonce = "0x" + "b3" * 32
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer(PAY_TO, 1, "0xd1"), _transfer(PAY_TO, 1, "0xd2", 2)]))
    assert rows[nonce]["state"] == "underpaid", rows[nonce]
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer(PAY_TO, 300000, "0xp1"), _transfer(PAY_TO, 200000, "0xp2", 2)]))
    assert rows[nonce]["state"] == "confirmed" and "in 2 transfers" in rows[nonce]["note"]
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer(PAY_TO, 500100, "0xround")]))
    assert rows[nonce]["state"] == "confirmed" and "surplus 100" in rows[nonce]["note"]


def test_the_consuming_tx_with_no_payer_transfer_is_not_paid():
    nonce = "0x" + "b4" * 32
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[]))
    assert rows[nonce]["state"] == "not-paid" and "no Transfer from the payer" in rows[nonce]["note"]


# ---- the window: clamp, depth, holes, orphans -------------------------------

def test_consumed_but_no_event_in_a_clamped_or_shallow_window_is_not_a_verdict():
    nonce = "0x" + "c1" * 32
    clamped = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after="0", valid_before=FUTURE)
    rows = _by_nonce([clamped], pay_to=PAY_TO, **_chain(kind=None))
    assert rows[nonce]["state"] == "undetermined" and rows[nonce]["window"]["clamped"] is True
    shallow = dict(DEEP, to_block=990, confirmed_depth=False)
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(kind=None, meta=shallow))
    assert rows[nonce]["state"] == "pending" and "re-judged" in rows[nonce]["note"]
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(kind=None))
    assert rows[nonce]["state"] == "undetermined" and "blocks 100..900, head 1000" in rows[nonce]["note"]


def test_the_scan_window_comes_from_the_signature_not_our_clock():
    nonce = "0x" + "c2" * 32
    seen = []

    def event_fn(network, asset, payer, nonce_, since, until):
        seen.append((since, until)); return (None, None, dict(DEEP))
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    reconcile.reconcile([line], now=NOW, consumed_fn=lambda *a: True, event_fn=event_fn, tx_fn=lambda *a: [], pay_to=PAY_TO)
    assert seen[0][0] == int(VA) and seen[0][1] >= int(FUTURE)
    seen.clear()
    line0 = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after="0", valid_before=FUTURE)
    reconcile.reconcile([line0], now=NOW, consumed_fn=lambda *a: True, event_fn=event_fn, tx_fn=lambda *a: [], pay_to=PAY_TO)
    assert seen[0][0] == seen[0][1] - reconcile.LOOKBACK_SECONDS and seen[0][0] > 0


def _rpc_table(pages, head, by_hash=None):
    """pages: {(from, to): logs | None}; by_hash: {blockHash: logs} overriding a block's receipts."""
    calls = []
    by_hash = {k.lower(): v for k, v in (by_hash or {}).items()}

    def rpc(url, method, params):
        calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(head)
        if method == "eth_getBlockByNumber":
            if params[0] in ("finalized", "safe", "latest"):
                n = head - 100 if params[0] == "finalized" else head
            else:
                n = int(params[0], 16)
            return {"number": hex(n), "timestamp": hex(n), "hash": "0xcanon%x" % n}
        if method == "eth_getBlockReceipts":
            h = params[0].lower()
            if h in by_hash:
                return [{"logs": by_hash[h]}]
            seen = {}
            for logs in pages.values():
                for l in logs or []:
                    if (l.get("blockHash") or "").lower() == h:
                        seen[(l.get("transactionHash"), l.get("logIndex"))] = l
            return [{"logs": list(seen.values())}]
        if method == "eth_getLogs":
            key = (int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16))
            if key in pages:
                return pages[key]
            # a tail probe: what the listed pages hold inside this range
            lo, hi = key
            seen = {}
            for logs in pages.values():
                for l in logs or []:
                    if lo <= int(l["blockNumber"], 16) <= hi:
                        seen[(l.get("transactionHash"), l.get("logIndex"))] = l
            return sorted(seen.values(), key=lambda l: (int(l["blockNumber"], 16), int(l["logIndex"], 16)))
        if method == "eth_getTransactionReceipt":
            return {"logs": []}
        raise AssertionError(method)
    return rpc, calls


def _event_log(nonce, tx, block, kind="used", orphan=False, index=0):
    topic = reconcile.AUTH_CANCELED_TOPIC if kind == "canceled" else reconcile.AUTH_USED_TOPIC
    return {"address": ASSET, "topics": [topic, reconcile._pad(PAYER), nonce], "data": "0x",
            "transactionHash": tx, "logIndex": hex(index), "blockNumber": hex(block),
            "blockHash": ("0xorphan%x" if orphan else "0xcanon%x") % block}


def test_pages_must_tile_the_window_and_a_null_page_is_a_hole(monkeypatch):
    monkeypatch.setattr(reconcile, "block_at", lambda network, ts: ts)
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 100)
    nonce = "0x" + "d1" * 32
    rpc, calls = _rpc_table({(1000, 1099): [], (1100, 1199): None, (1200, 1250): []}, head=1250)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    try:
        reconcile.consuming_event(NET, ASSET, PAYER, nonce, 1000, 1250)
    except reconcile.IncompleteRead as exc:
        assert "1100..1199" in str(exc)
    else:
        raise AssertionError("a null page was read as empty")
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after="1000", valid_before="1250")
    rows = _by_nonce([line], consumed_fn=lambda *a: True, event_fn=reconcile.consuming_event, tx_fn=reconcile.transfers_in_tx, pay_to=PAY_TO)
    assert rows[nonce]["state"] == "undetermined" and "1100..1199" in rows[nonce]["window"]["gap"]
    rpc, calls = _rpc_table({(1000, 1099): [], (1100, 1199): [], (1200, 1250): []}, head=1250)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    kind, tx, meta = reconcile.consuming_event(NET, ASSET, PAYER, nonce, 1000, 1250)
    assert (kind, tx) == (None, None) and meta["from_block"] == 1000 and meta["to_block"] == 1250
    assert len([c for c in calls if c[0] == "eth_getLogs"]) == 3


def test_an_event_echoed_in_two_pages_is_one_event_and_an_orphaned_one_is_none(monkeypatch):
    monkeypatch.setattr(reconcile, "block_at", lambda network, ts: ts)
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 100)
    nonce = "0x" + "d2" * 32
    ev = _event_log(nonce, "0xonce", 1150)
    rpc, _ = _rpc_table({(1000, 1099): [ev], (1100, 1199): [ev], (1200, 1250): []}, head=1250)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    kind, tx, _meta = reconcile.consuming_event(NET, ASSET, PAYER, nonce, 1000, 1250)
    assert (kind, tx) == ("used", "0xonce"), "an echoed event was read as two consumers"
    ghost = _event_log(nonce, "0xghost", 1150, orphan=True)
    rpc, _ = _rpc_table({(1000, 1099): [], (1100, 1199): [ghost], (1200, 1250): []}, head=1250)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    try:
        reconcile.consuming_event(NET, ASSET, PAYER, nonce, 1000, 1250)
    except reconcile.IncompleteRead as exc:
        assert "discarded branch" in str(exc)
    else:
        raise AssertionError("an orphaned event was accepted")


def test_two_consuming_txs_across_a_reorg_is_not_a_contradiction(monkeypatch):
    """Keep the canonical one; none canonical -> retry after finality; two canonical cannot be."""
    monkeypatch.setattr(reconcile, "block_at", lambda network, ts: ts)
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 1000)
    nonce = "0x" + "d3" * 32
    # the old branch's consumer and the canonical one, both in the page
    rpc, _ = _rpc_table({(1000, 1250): [_event_log(nonce, "0xold", 1100, orphan=True), _event_log(nonce, "0xnew", 1200, index=1)]}, head=1250)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    assert reconcile.consuming_event(NET, ASSET, PAYER, nonce, 1000, 1250)[:2] == ("used", "0xnew")
    # none canonical: the chain is mid-reorg; no verdict
    rpc, _ = _rpc_table({(1000, 1250): [_event_log(nonce, "0xold", 1100, orphan=True), _event_log(nonce, "0xnew", 1200, index=1, orphan=True)]}, head=1250)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    try:
        reconcile.consuming_event(NET, ASSET, PAYER, nonce, 1000, 1250)
    except reconcile.IncompleteRead as exc:
        assert "retry after finality" in str(exc)
    else:
        raise AssertionError("orphaned consumers were accepted")
    # two canonical consumers for one nonce cannot happen; if the chain says so, say so
    rpc, _ = _rpc_table({(1000, 1250): [_event_log(nonce, "0xa", 1100), _event_log(nonce, "0xb", 1200, index=1)]}, head=1250)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    try:
        reconcile.consuming_event(NET, ASSET, PAYER, nonce, 1000, 1250)
    except reconcile.ChainUnavailable as exc:
        assert "cannot be" in str(exc)
    else:
        raise AssertionError("two canonical consumers for one nonce were accepted")


def test_transfers_in_tx_distinguishes_no_receipt_from_no_transfer(monkeypatch):
    def rpc(url, method, params):
        if method == "eth_getTransactionReceipt":
            return None if params[0] == "0xmempool" else {"logs": []}
        if method == "eth_blockNumber":
            return hex(1000)
        if method == "eth_getBlockByNumber":
            # The finalized head; answered here so this test does not depend
            # on another test having filled the module's finality cache.
            return {"number": hex(900), "hash": "0x" + "f9" * 32}
        raise AssertionError(method)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    assert reconcile.transfers_in_tx(NET, ASSET, PAYER, "0xmempool") is None
    assert reconcile.transfers_in_tx(NET, ASSET, PAYER, "0xmined")[:3] == ([], True, [])


def test_the_transfer_selected_is_the_payers_not_the_first_and_dedupes():
    relayer, pad = "0x" + "11" * 20, reconcile._pad
    base = {"address": ASSET, "transactionHash": "0xt", "blockNumber": "0x10", "blockHash": "0xcanon10"}
    logs = [
        dict(base, topics=[reconcile.TRANSFER_TOPIC, pad(relayer), pad(PAY_TO)], data=hex(7), logIndex="0x0"),
        dict(base, topics=[reconcile.TRANSFER_TOPIC, pad(PAYER), pad(PAY_TO)], data=hex(500000), logIndex="0x1"),
        dict(base, topics=[reconcile.TRANSFER_TOPIC, pad(PAYER), pad(PAY_TO)], data=hex(500000), logIndex="0x1"),  # echo
        dict(base, address="0x" + "cc" * 20, topics=[reconcile.TRANSFER_TOPIC, pad(PAYER), pad(PAY_TO)], data=hex(500000), logIndex="0x2"),
    ]
    found = reconcile.transfers_in_logs(logs, ASSET, PAYER)
    assert [(t["from"], t["to"], t["value"]) for t in found] == [(PAYER.lower(), PAY_TO.lower(), 500000)]


# ---- bookkeeping ----------------------------------------------------------

def test_one_row_per_key_earliest_line_wins_and_the_latest_window_holds():
    nonce = "0x" + "55" * 32
    lines = [
        _line("settlement-unknown", nonce, "2026-10-21T10:05:00+00:00", path="/x402/receipt", valid_after=VA, valid_before=PAST),
        _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", path="/x402/provision", valid_after=VA, valid_before=FUTURE),
    ]
    rows = _by_nonce(lines, pay_to=PAY_TO, **_chain(consumed=False, kind=None))
    row = rows[nonce]
    assert row["state"] == "pending" and "2 signed authorizations" in row["note"]
    assert row["first_seen"].startswith("2026-10-21T10:00:00") and row["path"] == "/x402/provision"
    lines.append(_line("settled", nonce, "2026-10-21T10:06:00+00:00", tx="0x1"))
    rows = _by_nonce(lines, pay_to=PAY_TO, **_chain(transfers=[_transfer(PAY_TO, 500000, "0x1")]))
    assert rows[nonce]["state"] == "confirmed" and "resolves an earlier unknown" in rows[nonce]["note"]


def test_the_key_is_the_full_tuple():
    nonce = "0x" + "99" * 32
    lines = [
        _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0x1"),
        _line("settled", nonce, "2026-10-21T10:01:00+00:00", tx="0x2", asset="0x" + "cc" * 20),
    ]
    seen = []

    def consumed(network, asset, payer, nonce_):
        seen.append(asset); return True
    rows, _ = reconcile.reconcile(lines, now=NOW, consumed_fn=consumed, event_fn=lambda *a: (None, None, dict(DEEP)), tx_fn=lambda *a: [])
    assert len(rows) == 2 and sorted(seen) == sorted([ASSET, "0x" + "cc" * 20])


def test_lines_without_the_full_key_are_counted_not_guessed():
    old = "x402-funnel " + json.dumps({"ts": "2026-10-20T13:53:58+00:00", "event": "settled",
                                       "path": "/x402/provision", "amount": "500000", "tx": "0x7100"})
    rows, unkeyed = reconcile.reconcile([old, "not json at all", ""], now=NOW, consumed_fn=lambda *a: True)
    assert rows == [] and unkeyed == 1


def test_exit_codes_say_what_is_left(tmp_path, monkeypatch):
    ok, pend, undet, gap = ("0x" + x * 32 for x in ("01", "02", "03", "04"))
    log = tmp_path / "funnel.jsonl"
    monkeypatch.setattr(reconcile, "authorization_consumed", lambda n, a, p, x: x in (ok, undet, gap))
    monkeypatch.setattr(reconcile, "consuming_event",
                        lambda n, a, p, x, s, u: ("used", "0xg", dict(DEEP)) if x == gap else (None, None, dict(DEEP)))
    monkeypatch.setattr(reconcile, "transfers_in_tx",
                        lambda n, a, p, t, nonce=None: [_transfer(PAY_TO, 500000, "0x1")] if t == "0x1" else [_transfer("0x" + "dd" * 20, 500000, "0xg")])
    log.write_text("\n".join([
        _line("settled", ok, "2026-10-21T10:00:00+00:00", tx="0x1"),
        _line("settlement-unknown", pend, "2026-10-21T10:01:00+00:00", valid_before=FUTURE),
    ]), encoding="utf-8")
    assert reconcile.main(["reconcile", str(log), "--now=2026-10-21T12:00:00Z", "--pay-to=" + PAY_TO]) == 1
    log.write_text(_line("settled", ok, "2026-10-21T10:00:00+00:00", tx="0x1"), encoding="utf-8")
    assert reconcile.main(["reconcile", str(log), "--now=2026-10-21T12:00:00Z", "--pay-to=" + PAY_TO]) == 0
    log.write_text(_line("settlement-unknown", undet, "2026-10-21T10:00:00+00:00", valid_after="0", valid_before=FUTURE), encoding="utf-8")
    assert reconcile.main(["reconcile", str(log), "--now=2026-10-21T12:00:00Z", "--pay-to=" + PAY_TO]) == 2
    log.write_text(_line("settlement-unknown", gap, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE), encoding="utf-8")
    assert reconcile.main(["reconcile", str(log), "--now=2026-10-21T12:00:00Z", "--pay-to=" + PAY_TO]) == 1

    def down(*a):
        raise reconcile.ChainUnavailable("rpc down")
    monkeypatch.setattr(reconcile, "authorization_consumed", down)
    # one row, chain not asked: nothing was measured - blind, not "could not ask for some"
    assert reconcile.main(["reconcile", str(log), "--now=2026-10-21T12:00:00Z", "--pay-to=" + PAY_TO]) == 3
    # two rows, one not asked: partial
    monkeypatch.setattr(reconcile, "authorization_consumed", lambda n, a, p, x: (_ for _ in ()).throw(reconcile.ChainUnavailable("down")) if x == gap else True)
    log.write_text(chr(10).join([_line("settled", ok, "2026-10-21T10:00:00+00:00", tx="0x1"),
                              _line("settlement-unknown", gap, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)]),
                   encoding="utf-8")
    assert reconcile.main(["reconcile", str(log), "--now=2026-10-21T12:00:00Z", "--pay-to=" + PAY_TO]) == 2


def test_a_plain_transfer_is_reported_once_per_payer_and_never_satisfies_an_invoice():
    """Money that landed without consuming a nonce is a payee-level fact (minia2auk)."""
    n1, n2 = "0x" + "f1" * 32, "0x" + "f3" * 32
    lines = [_line("settlement-unknown", n, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE) for n in (n1, n2)]
    chain = _chain(kind=None)
    scans = []

    def plain_fn(network, asset, payer, to, since, until):
        scans.append((since, until)); return [_transfer(PAY_TO, 500000, "0xplain")]
    rows, _ = reconcile.reconcile(lines, now=NOW, pay_to=PAY_TO, consumed_fn=chain["consumed_fn"],
                                  event_fn=chain["event_fn"], tx_fn=chain["tx_fn"], plain_fn=plain_fn)
    by_state = {}
    for r in rows:
        by_state.setdefault(r["state"], []).append(r)
    assert len(by_state.get("undetermined", [])) == 2, rows
    assert all("unsolicited-transfer row" in r["note"] for r in by_state["undetermined"])
    assert len(by_state.get("unsolicited-transfer", [])) == 1, "the plain transfer was attached to invoices"
    payee = by_state["unsolicited-transfer"][0]
    assert payee["nonce"] is None and "0xplain" in payee["note"] and payee["paid"] == 500000
    assert len(scans) == 1, "scanned once per payer, not once per row"
    rows = _by_nonce(lines[:1], pay_to=PAY_TO, **chain)
    assert rows[n1]["state"] == "undetermined" and "unsolicited-transfer row" in rows[n1]["note"]


def test_the_consuming_txs_transfers_are_matched_not_the_event_alone():
    """AuthorizationUsed carries no amount or recipient; value 0 to payTo is not paid."""
    nonce = "0x" + "f2" * 32
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer(PAY_TO, 0, "0xzero")]))
    assert rows[nonce]["state"] == "underpaid" and rows[nonce]["shortfall"] == 500000, rows[nonce]   # paid zero to payTo
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer("0x" + "dd" * 20, 500000, "0xelse")]))
    assert rows[nonce]["state"] == "unrecognised-receiver"


def _receipt_log(topic0, topics_rest, data, index, tx="0xbatch", block=700):
    return {"address": ASSET, "topics": [topic0] + topics_rest, "data": data, "transactionHash": tx,
            "logIndex": hex(index), "blockNumber": hex(block), "blockHash": "0xcanon%x" % block}


def test_a_batched_tx_attributes_each_transfer_to_its_own_authorization(monkeypatch):
    """Two invoices settled in one tx: each row gets its own Transfer, no surplus, no shared log."""
    n1, n2 = "0x" + "b1" * 32, "0x" + "b5" * 32
    pad = reconcile._pad
    logs = [
        _receipt_log(reconcile.AUTH_USED_TOPIC, [pad(PAYER), n1], "0x", 10),
        _receipt_log(reconcile.TRANSFER_TOPIC, [pad(PAYER), pad(PAY_TO)], hex(500000), 11),
        _receipt_log(reconcile.AUTH_USED_TOPIC, [pad(PAYER), n2], "0x", 12),
        _receipt_log(reconcile.TRANSFER_TOPIC, [pad(PAYER), pad(PAY_TO)], hex(500000), 13),
    ]
    assert [int(l["logIndex"], 16) for l in reconcile.logs_bound_to(logs, ASSET, PAYER, n1)[0]] == [11]
    assert [int(l["logIndex"], 16) for l in reconcile.logs_bound_to(logs, ASSET, PAYER, n2)[0]] == [13]
    assert reconcile.logs_bound_to(logs, ASSET, PAYER, "0x" + "99" * 32) is None

    def rpc(url, method, params):
        if method == "eth_getTransactionReceipt":
            return {"logs": logs}
        if method == "eth_getBlockByNumber":
            if params[0] == "finalized":
                return {"number": hex(995), "hash": "0xfin"}
            return {"hash": "0xcanon%x" % int(params[0], 16)}
        if method == "eth_blockNumber":
            return hex(1000)
        raise AssertionError(method)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    lines = [_line("settled", n, "2026-10-21T10:00:00+00:00", tx="0xbatch") for n in (n1, n2)]
    rows = _by_nonce(lines, pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: ("used", "0xbatch", dict(DEEP)),
                     tx_fn=reconcile.transfers_in_tx)
    assert rows[n1]["state"] == "confirmed" and rows[n2]["state"] == "confirmed", rows
    assert rows[n1]["paid"] == 500000 and rows[n1]["surplus"] == 0, "the batch was summed against one row"
    assert rows[n1]["logs"] != rows[n2]["logs"], "two rows rest on the same log"


def test_two_rows_resting_on_one_log_are_both_undetermined():
    """The structural guard, for a reader that cannot pair (legacy fakes return unpaired lists)."""
    n1, n2 = "0x" + "c3" * 32, "0x" + "c4" * 32
    lines = [_line("settlement-unknown", n, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE) for n in (n1, n2)]
    same = [_transfer(PAY_TO, 500000, "0xonce", index=5)]
    rows = _by_nonce(lines, pay_to=PAY_TO, consumed_fn=lambda *a: True,
                     event_fn=lambda *a: ("used", "0xonce", dict(DEEP)), tx_fn=lambda *a, **k: list(same))
    assert rows[n1]["state"] == rows[n2]["state"] == "undetermined", rows
    assert "cannot settle two invoices" in rows[n1]["note"]


def test_surplus_and_parts_are_structured_fields():
    nonce = "0x" + "c5" * 32
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer(PAY_TO, 300100, "0xp1", 1), _transfer(PAY_TO, 200000, "0xp2", 2)]))
    row = rows[nonce]
    assert (row["state"], row["paid"], row["surplus"], row["parts"]) == ("confirmed", 500100, 100, 2)
    assert row["logs"] == [(NET, "0x1f4", "0xcanon1f4", "0x1"), (NET, "0x1f4", "0xcanon1f4", "0x2")], \
        "claims are keyed on (network, blockNumber, blockHash, logIndex)"


def test_a_facilitator_tx_that_does_not_consume_this_nonce_is_not_its_evidence():
    nonce = "0x" + "c6" * 32
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xother")
    # the reported tx mined and moved money, but carries no AuthorizationUsed for this nonce,
    # while the slot is burned: the real consumer is looked up instead
    rows = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True,
                     event_fn=lambda *a: ("used", "0xreal", dict(DEEP)),
                     tx_fn=lambda n, a, p, t, nonce=None: ([_transfer(PAY_TO, 500000, t)], t == "0xreal"))
    assert rows[nonce]["state"] == "confirmed" and rows[nonce]["tx"] == "0xreal", rows[nonce]


def test_the_last_slice_is_not_open_ended_a_plain_transfer_after_it_is_not_surplus(monkeypatch):
    """A plain transfer later in the same multicall must not print as surplus (minia2auk)."""
    nonce = "0x" + "e4" * 32
    pad = reconcile._pad
    logs = [
        _receipt_log(reconcile.AUTH_USED_TOPIC, [pad(PAYER), nonce], "0x", 10),
        _receipt_log(reconcile.TRANSFER_TOPIC, [pad(PAYER), pad(PAY_TO)], hex(500000), 11),
        # the asset then emits an Approval-shaped event (not a payer transfer): the run is over
        _receipt_log("0x" + "ab" * 32, [pad(PAYER), pad("0x" + "77" * 20)], hex(1), 12),
        _receipt_log(reconcile.TRANSFER_TOPIC, [pad(PAYER), pad(PAY_TO)], hex(999999), 13),   # plain transfer, later
    ]
    adjacent, later = reconcile.logs_bound_to(logs, ASSET, PAYER, nonce)
    assert [int(l["logIndex"], 16) for l in adjacent] == [11] and [int(l["logIndex"], 16) for l in later] == [13]

    def rpc(url, method, params):
        if method == "eth_getTransactionReceipt":
            return {"logs": logs}
        if method == "eth_getBlockByNumber":
            if params[0] == "finalized":
                return {"number": hex(995), "hash": "0xfin"}
            return {"hash": "0xcanon%x" % int(params[0], 16)}
        if method == "eth_blockNumber":
            return hex(1000)
        raise AssertionError(method)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xbatch")
    rows = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: ("used", "0xbatch", dict(DEEP)),
                     tx_fn=reconcile.transfers_in_tx)
    row = rows[nonce]
    assert row["state"] == "confirmed" and row["surplus"] == 0 and row["paid"] == 500000, row
    assert "not adjacent" in row["note"] and "not counted" in row["note"]


def test_an_adjacent_run_short_of_the_amount_is_undetermined_not_made_up_from_later_logs():
    nonce = "0x" + "e5" * 32
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    short, later = [_transfer(PAY_TO, 200000, "0xb", 11)], [_transfer(PAY_TO, 300000, "0xb", 13)]
    rows = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: ("used", "0xb", dict(DEEP)),
                     tx_fn=lambda *a, **k: (short, True, later))
    assert rows[nonce]["state"] == "undetermined" and "not attributable" in rows[nonce]["note"], rows[nonce]


def test_a_hook_or_proxy_log_between_the_pair_does_not_break_it():
    nonce = "0x" + "e6" * 32
    pad = reconcile._pad
    hook = dict(_receipt_log("0x" + "cd" * 32, [], "0x", 11), address="0x" + "99" * 20)   # another contract
    logs = [
        _receipt_log(reconcile.AUTH_USED_TOPIC, [pad(PAYER), nonce], "0x", 10),
        hook,
        _receipt_log(reconcile.TRANSFER_TOPIC, [pad(PAYER), pad(PAY_TO)], hex(500000), 12),
    ]
    adjacent, later = reconcile.logs_bound_to(logs, ASSET, PAYER, nonce)
    assert [int(l["logIndex"], 16) for l in adjacent] == [12] and later == []


def test_a_transfer_by_another_token_in_the_slice_is_not_evidence():
    nonce = "0x" + "e7" * 32
    pad = reconcile._pad
    foreign = dict(_receipt_log(reconcile.TRANSFER_TOPIC, [pad(PAYER), pad(PAY_TO)], hex(500000), 11), address="0x" + "cc" * 20)
    logs = [_receipt_log(reconcile.AUTH_USED_TOPIC, [pad(PAYER), nonce], "0x", 10), foreign]
    adjacent, later = reconcile.logs_bound_to(logs, ASSET, PAYER, nonce)
    assert adjacent == [] and later == []


def test_an_authorization_paying_a_third_party_pairs_but_does_not_confirm():
    """receiveWithAuthorization also emits AuthorizationUsed with the payer as authorizer;
    the pairing is right and the `to` check is what keeps the wrong invoice from being credited."""
    nonce = "0x" + "e8" * 32
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer("0x" + "33" * 20, 500000, "0x3rd")]))
    assert rows[nonce]["state"] == "unrecognised-receiver", rows[nonce]


def test_claims_are_keyed_on_block_hash_so_a_reorg_is_a_collision_not_a_reuse():
    n1, n2 = "0x" + "e9" * 32, "0x" + "ea" * 32
    lines = [_line("settlement-unknown", n, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE) for n in (n1, n2)]
    # same tx and logIndex, different blockHash: distinct evidence, no collision
    a = dict(_transfer(PAY_TO, 500000, "0xsame", 5), block_hash="0xblockA")
    b = dict(_transfer(PAY_TO, 500000, "0xsame", 5), block_hash="0xblockB")
    common = dict(pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: ("used", "0xsame", dict(DEEP)),
                  tx_fn=lambda n, a_, p, t, nonce=None: ([a] if nonce == n1 else [b], True))
    # same height, two hashes, near the head: the chain has not answered, neither row is a verdict
    rows = _by_nonce(lines, canonical_fn=lambda network, number: ("0xblocka", 400), **common)
    assert rows[n1]["state"] == rows[n2]["state"] == "undetermined" and "near the head" in rows[n1]["note"]
    assert rows[n1]["final"] is False
    # the same height now under the finalized head: determinate - the canonical row stands, the orphan's loses its evidence
    rows = _by_nonce(lines, canonical_fn=lambda network, number: ("0xblocka", 600), **common)
    assert rows[n1]["state"] == "confirmed", rows[n1]
    assert rows[n2]["state"] == "undetermined" and "orphaned" in rows[n2]["note"] and rows[n2]["final"] is False


# ---- round ten: groups, underpaid, finality, branches, capped pages ----------------------

def test_one_run_several_receivers_only_the_pay_to_group_counts():
    """payTo 1 + friend 5 against 5 is not a confirmed 6 (minia2auk)."""
    nonce = "0x" + "f1" * 32
    friend = "0x" + "77" * 20
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xmulti")
    run = [_transfer(PAY_TO, 100000, "0xmulti", 11), _transfer(friend, 500000, "0xmulti", 12)]
    rows = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=run))
    row = rows[nonce]
    assert row["state"] == "underpaid" and row["paid"] == 100000 and row["shortfall"] == 400000, row
    assert "not counted" in row["note"] and friend[:10] in row["note"]
    # the other way round: confirmed from the payTo group alone, the friend's transfer printed, surplus 0
    run = [_transfer(PAY_TO, 500000, "0xmulti", 11), _transfer(friend, 100000, "0xmulti", 12)]
    row = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=run))[nonce]
    assert row["state"] == "confirmed" and row["surplus"] == 0 and row["paid"] == 500000
    assert "also in this run" in row["note"] and "100000 to " + friend[:10] in row["note"]


def test_underpaid_is_terminal_and_distinct_from_undetermined():
    nonce = "0x" + "f2" * 32
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xshort")
    row = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer(PAY_TO, 200000, "0xshort")]))[nonce]
    assert row["state"] == "underpaid" and row["shortfall"] == 300000 and "terminal" in row["note"]
    # the same shortfall with later logs that would make it up is the other bucket
    row = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True,
                    event_fn=lambda *a: ("used", "0xshort", dict(DEEP)),
                    tx_fn=lambda *a, **k: ([_transfer(PAY_TO, 200000, "0xshort", 11)], True,
                                           [_transfer(PAY_TO, 300000, "0xshort", 13)]))[nonce]
    assert row["state"] == "undetermined"


def test_undetermined_says_whether_depth_can_still_change_it():
    nonce = "0x" + "f3" * 32
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    short, later = [_transfer(PAY_TO, 200000, "0xb", 11)], [_transfer(PAY_TO, 300000, "0xb", 13)]
    deep = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: ("used", "0xb", dict(DEEP)),
                     tx_fn=lambda *a, **k: (short, True, later, {"confirmed_depth": True, "finalized": True}))[nonce]
    assert deep["state"] == "undetermined" and deep["final"] is True and "final:" in deep["note"]
    shallow = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: ("used", "0xb", dict(DEEP)),
                        tx_fn=lambda *a, **k: (short, True, later, {"confirmed_depth": False, "finalized": False}))[nonce]
    assert shallow["state"] == "undetermined" and shallow["final"] is False and "not final" in shallow["note"]


def test_a_receipt_reports_its_own_depth(monkeypatch):
    nonce = "0x" + "f4" * 32
    pad = reconcile._pad
    logs = [_receipt_log(reconcile.AUTH_USED_TOPIC, [pad(PAYER), nonce], "0x", 10, block=990),
            _receipt_log(reconcile.TRANSFER_TOPIC, [pad(PAYER), pad(PAY_TO)], hex(500000), 11, block=990)]

    def rpc(url, method, params):
        if method == "eth_getTransactionReceipt":
            return {"logs": logs}
        if method == "eth_getBlockByNumber":
            if params[0] == "finalized":
                return {"number": hex(995), "hash": "0xfin"}
            return {"hash": "0xcanon%x" % int(params[0], 16)}
        if method == "eth_blockNumber":
            return hex(1000)
        raise AssertionError(method)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    found, in_tx, later, meta = reconcile.transfers_in_tx(NET, ASSET, PAYER, "0xbatch", nonce)
    assert len(found) == 1 and meta == {"head": 1000, "block": 990, "block_hash": "0xcanon3de", "confirmed_depth": False,
                                        "finalized": True, "finalized_height": 995, "finalized_hash": "0xfin"}, meta
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xbatch")
    row = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: ("used", "0xbatch", dict(DEEP)),
                    tx_fn=reconcile.transfers_in_tx)[nonce]
    # a row is judged on its evidence; whether the endpoint's tag is trustworthy is judged once, per endpoint
    assert row["state"] == "confirmed" and row["final"] is True and "finality_suspect" not in row


def test_claims_are_unique_per_network():
    """The same block hash and log index on two rails is two facts, not a collision."""
    n1, n2 = "0x" + "f5" * 32, "0x" + "f6" * 32
    other = "eip155:84532"
    lines = [_line("settlement-unknown", n1, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE),
             _line("settlement-unknown", n2, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE, network=other)]
    t = _transfer(PAY_TO, 500000, "0xsame", 5)
    rows = _by_nonce(lines, pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: ("used", "0xsame", dict(DEEP)),
                     tx_fn=lambda *a, **k: ([dict(t)], True))
    assert rows[n1]["state"] == rows[n2]["state"] == "confirmed"
    assert rows[n1]["logs"][0][0] == NET and rows[n2]["logs"][0][0] == other


def test_a_capped_page_is_followed_past_its_last_block_and_repaired_from_receipts(monkeypatch):
    """A count cap returns a prefix: halves are not a partition, the tail is (minia2auk)."""
    nonce = "0x" + "f7" * 32
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 1000)

    def ev(block, i):
        return _event_log(nonce, "0xt%d" % i, block, index=i)
    capped = [ev(1010, 1), ev(1020, 2), ev(1040, 3)]           # stops part-way through block 1040
    tail = [ev(1100, 5), ev(1200, 6)]                          # what the cap dropped after 1040
    rpc, calls = _rpc_table({(1000, 1250): capped, (1041, 1250): tail, (1201, 1250): []}, head=1250,
                            by_hash={"0xcanon%x" % 1040: [ev(1040, 3), ev(1040, 4)]})   # 1040 really holds two
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    monkeypatch.setattr(reconcile, "block_at", lambda network, ts: 1000 if ts < 500 else 1250)
    logs, meta = reconcile._tiled_logs(reconcile.RPC[NET], NET, ASSET, [[reconcile.AUTH_USED_TOPIC]], 0, 1000)
    assert sorted(int(l["logIndex"], 16) for l in logs) == [1, 2, 3, 4, 5, 6]
    ranges = [(p[0]["fromBlock"], p[0]["toBlock"]) for m, p in calls if m == "eth_getLogs"]
    assert ranges == [(hex(1000), hex(1250)), (hex(1041), hex(1250)), (hex(1201), hex(1250))], "disjoint ranges, no halves"


def test_a_page_log_its_receipts_do_not_hold_is_an_incomplete_read(monkeypatch):
    nonce = "0x" + "f8" * 32
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 1000)
    one = _event_log(nonce, "0xa", 1250)
    rpc, _ = _rpc_table({(1000, 1250): [one]}, head=1250, by_hash={"0xcanon%x" % 1250: []})
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    monkeypatch.setattr(reconcile, "block_at", lambda network, ts: 1000 if ts < 500 else 1250)
    with pytest.raises(reconcile.IncompleteRead, match="reads disagree"):
        reconcile._tiled_logs(reconcile.RPC[NET], NET, ASSET, [[reconcile.AUTH_USED_TOPIC]], 0, 1000)


def test_an_unordered_page_is_not_trusted_to_be_a_prefix(monkeypatch):
    nonce = "0x" + "f9" * 32
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 1000)
    rpc, _ = _rpc_table({(1000, 1250): [_event_log(nonce, "0xb", 1200), _event_log(nonce, "0xa", 1100)]}, head=1250)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    monkeypatch.setattr(reconcile, "block_at", lambda network, ts: 1000 if ts < 500 else 1250)
    with pytest.raises(reconcile.IncompleteRead, match="out of order"):
        reconcile._tiled_logs(reconcile.RPC[NET], NET, ASSET, [[reconcile.AUTH_USED_TOPIC]], 0, 1000)


def test_final_is_the_weakest_evidence_and_underpaid_is_terminal_only_when_final():
    nonce = "0x" + "fa" * 32
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after=VA, valid_before=FUTURE)
    short = [_transfer(PAY_TO, 200000, "0xs", 11)]
    shallow_window = dict(DEEP, confirmed_depth=False, finalized=False)
    cases = [(DEEP, True, True), (DEEP, False, False), (shallow_window, True, False)]
    for window, tx_depth, final in cases:
        row = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True,
                        event_fn=lambda *a, w=window: ("used", "0xs", dict(w)),
                        tx_fn=lambda *a, d=tx_depth, **k: (list(short), True, [], {"confirmed_depth": d, "finalized": d}))[nonce]
        assert row["state"] == "underpaid" and row["final"] is final, (window, tx_depth, row)
        if final:
            assert "terminal - the evidence is L1-finalized" in row["note"]
        else:
            assert "short in what has been read" in row["note"] and "terminal -" not in row["note"]


def test_a_receipt_with_no_transfer_still_has_a_depth(monkeypatch):
    nonce = "0x" + "fb" * 32
    pad = reconcile._pad
    logs = [_receipt_log(reconcile.AUTH_USED_TOPIC, [pad(PAYER), nonce], "0x", 10, block=100)]

    def rpc(url, method, params):
        if method == "eth_getTransactionReceipt":
            return {"logs": logs, "blockNumber": hex(100), "blockHash": "0xcanon64"}
        if method == "eth_getBlockByNumber":
            if params[0] == "finalized":
                return {"number": hex(995), "hash": "0xfin"}
            return {"hash": "0xcanon%x" % int(params[0], 16)}
        if method == "eth_blockNumber":
            return hex(1000)
        raise AssertionError(method)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    found, in_tx, later, meta = reconcile.transfers_in_tx(NET, ASSET, PAYER, "0xnothing", nonce)
    assert found == [] and meta["confirmed_depth"] is True and meta["finalized"] is True


def test_the_authorization_is_keyed_by_token_contract():
    """EIP-3009 nonces are scoped to the token: the same (payer, nonce) on another asset is another row."""
    nonce = "0x" + "fc" * 32
    other = "0x" + "cc" * 20
    lines = [_line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xa"),
             _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xb", asset=other)]
    asked = []

    def consumed_fn(network, asset, payer, n):
        asked.append(asset)
        return asset == ASSET
    rows, _ = reconcile.reconcile(lines, now=NOW, pay_to=PAY_TO, consumed_fn=consumed_fn,
                                  event_fn=lambda *a: (None, None, dict(DEEP)),
                                  tx_fn=lambda network, asset, payer, tx, nonce=None: ([_transfer(PAY_TO, 500000, tx)], asset == ASSET),
                                  plain_fn=lambda *a: [])
    by_asset = {r["asset"]: r for r in rows}
    assert set(asked) == {ASSET, other} and len(rows) == 2
    assert by_asset[ASSET]["state"] == "confirmed" and by_asset[other]["state"] == "investigate"


# ---- round twelve: strict order, bounded pages, capability, cross-check -------------------

def test_a_repeated_log_is_an_incomplete_read_not_a_surplus(monkeypatch):
    """10, 11, 11, 12 is in order and still wrong: a repeat inflates a sum (minia2auk)."""
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 1000)
    nonce = "0x" + "e1" * 32
    ev = _event_log(nonce, "0xa", 1100, index=11)
    rpc, _ = _rpc_table({(1000, 1250): [_event_log(nonce, "0xa", 1100, index=10), ev, dict(ev), _event_log(nonce, "0xa", 1100, index=12)]},
                        head=1250)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    with pytest.raises(reconcile.IncompleteRead, match="repeated"):
        reconcile._tiled_blocks(reconcile.RPC[NET], ASSET, [[reconcile.AUTH_USED_TOPIC]], 1000, 1250, 1250)


def test_logs_outside_the_asked_range_are_dropped_not_counted_twice(monkeypatch):
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 100)
    nonce = "0x" + "e2" * 32
    ev = _event_log(nonce, "0xa", 1150)
    rpc, _ = _rpc_table({(1000, 1099): [ev], (1100, 1199): [ev], (1200, 1250): []}, head=1250)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    logs, _ = reconcile._tiled_blocks(reconcile.RPC[NET], ASSET, [[reconcile.AUTH_USED_TOPIC]], 1000, 1250, 1250)
    assert len(logs) == 1


def test_every_range_asked_for_is_bounded_and_the_last_ends_at_the_window(monkeypatch):
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 100)
    rpc, calls = _rpc_table({(1000, 1099): [], (1100, 1199): [], (1200, 1250): []}, head=1250)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    reconcile._tiled_blocks(reconcile.RPC[NET], ASSET, [[reconcile.AUTH_USED_TOPIC]], 1000, 1250, 1250)
    ranges = [(int(p[0]["fromBlock"], 16), int(p[0]["toBlock"], 16)) for m, p in calls if m == "eth_getLogs"]
    assert all(b - a + 1 <= 100 for a, b in ranges) and ranges[-1][1] == 1250


def test_a_provider_without_block_receipts_is_its_own_state(monkeypatch):
    """Not served is a configuration fact, not evidence; it must not read as undetermined."""
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 1000)
    monkeypatch.setattr(reconcile, "block_at", lambda network, ts: ts)
    nonce = "0x" + "e3" * 32
    base, _ = _rpc_table({(1000, 1250): [_event_log(nonce, "0xa", 1100)]}, head=1250)

    def rpc(url, method, params):
        if method == "eth_getBlockReceipts":
            raise reconcile.RpcError(url, -32601, "the method eth_getBlockReceipts does not exist")
        return base(url, method, params)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    line = _line("settlement-unknown", nonce, "2026-10-21T10:00:00+00:00", valid_after="1000", valid_before="1250")
    rows = _by_nonce([line], consumed_fn=lambda *a: True, event_fn=reconcile.consuming_event,
                     tx_fn=reconcile.transfers_in_tx, pay_to=PAY_TO)
    assert rows[nonce]["state"] == "unsupported-rpc" and "eth_getBlockReceipts" in rows[nonce]["note"], rows[nonce]


def test_a_block_whose_receipts_are_not_served_is_a_gap_that_heals(monkeypatch):
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 1000)
    nonce = "0x" + "e4" * 32
    base, _ = _rpc_table({(1000, 1250): [_event_log(nonce, "0xa", 1100)]}, head=1250)

    def rpc(url, method, params):
        if method == "eth_getBlockReceipts" and params[0] != "latest":
            return None
        return base(url, method, params)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    with pytest.raises(reconcile.IncompleteRead, match="orphaned or pruned"):
        reconcile._tiled_blocks(reconcile.RPC[NET], ASSET, [[reconcile.AUTH_USED_TOPIC]], 1000, 1250, 1250)


def test_a_rate_limit_that_never_clears_is_a_hole_not_an_empty_page(monkeypatch):
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 100)

    def rpc(url, method, params):
        raise reconcile.ChainUnavailable("%s: HTTP Error 429: Too Many Requests" % url)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    with pytest.raises(reconcile.IncompleteRead, match="could not be read"):
        reconcile._page(reconcile.RPC[NET], ASSET, [[reconcile.AUTH_USED_TOPIC]], 1000, 1099)


def test_the_retry_backs_off_on_429_and_gives_up_as_unavailable(monkeypatch):
    calls, slept = [], []

    def once(url, method, params):
        calls.append(method)
        raise reconcile.RpcError(url, None, "Too Many Requests", 429)
    monkeypatch.setattr(reconcile, "_rpc_once", once)
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    with pytest.raises(reconcile.RpcError, match="429"):
        _REAL_RPC(reconcile.RPC[NET], "eth_blockNumber", [])
    backoff = [x for x in slept if x >= 1]
    assert len(calls) == reconcile.RPC_RETRIES + 1 and backoff == [2 ** i for i in range(reconcile.RPC_RETRIES)]
    calls.clear()

    def other(url, method, params):
        calls.append(method)
        raise reconcile.ChainUnavailable("%s: connection refused" % url)
    monkeypatch.setattr(reconcile, "_rpc_once", other)
    with pytest.raises(reconcile.ChainUnavailable):
        _REAL_RPC(reconcile.RPC[NET], "eth_blockNumber", [])
    assert len(calls) == 1, "only a rate limit is retried"


def test_every_settled_row_runs_the_paging_code_as_a_cross_check():
    nonce = "0x" + "e5" * 32
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xfac")
    tx_fn = lambda *a, **k: ([_transfer(PAY_TO, 500000, "0xfac")], True, [], {"confirmed_depth": True, "finalized": True, "block": 500})
    seen = []

    def agrees(network, asset, payer, n, block):
        seen.append(block)
        return "used", "0xFAC", dict(DEEP)
    row = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: (None, None, dict(DEEP)),
                    tx_fn=tx_fn, cross_fn=agrees)[nonce]
    assert seen == [500] and row["state"] == "confirmed" and row["cross_check"] == "agrees" and row["final"] is True
    missed = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: (None, None, dict(DEEP)),
                       tx_fn=tx_fn, cross_fn=lambda *a: (None, None, dict(DEEP)))[nonce]
    assert missed["cross_check"] == "disagrees" and missed["final"] is False and "missed" in missed["note"]

    def broken(*a):
        raise reconcile.IncompleteRead("blocks 350..449 could not be read")
    partial = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: (None, None, dict(DEEP)),
                        tx_fn=tx_fn, cross_fn=broken)[nonce]
    assert partial["state"] == "confirmed" and partial["cross_check"] == "incomplete" and partial["final"] is False


def test_a_disagreeing_cross_check_exits_2(tmp_path, monkeypatch):
    nonce = "0x" + "e6" * 32
    f = tmp_path / "funnel.jsonl"
    f.write_text(_line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xfac") + "\n", encoding="utf-8")
    monkeypatch.setattr(reconcile, "authorization_consumed", lambda *a: True)
    monkeypatch.setattr(reconcile, "transfers_in_tx",
                        lambda *a, **k: ([_transfer(PAY_TO, 500000, "0xfac")], True, [], {"confirmed_depth": True, "finalized": True, "block": 500}))
    monkeypatch.setattr(reconcile, "consuming_event_near", lambda *a, **k: (None, None, dict(DEEP)))
    assert reconcile.main(["reconcile.py", str(f), "--pay-to=" + PAY_TO, "--now=2026-10-21T12:00:00Z"]) == 2


def test_a_refusal_with_an_unknown_code_is_its_own_bucket_not_a_capability(monkeypatch):
    """1rpc.io: -32600 'Method eth_getBlockReceipts not allowed' (live, 28 Oct 2026). -32600 is
    'invalid request' by code, so it is rpc-refused - printed, exit 2 - not classified by prose."""
    def rpc(url, method, params):
        raise reconcile.RpcError(url, -32600, "Method eth_getBlockReceipts not allowed")
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    with pytest.raises(reconcile.RpcRefused, match="-32600"):
        reconcile._require_receipts("https://1rpc.io/base")


# ---- round thirteen: finality, codes, re-probe, blind runs, endpoint per row ----------------

def test_classification_reads_codes_never_prose():
    E = reconcile.RpcError
    assert reconcile.classify(E("u", -32011, "request limit reached")) == "quota"
    assert reconcile.classify(E("u", None, "anything", 429)) == "quota"
    assert reconcile.classify(E("u", -32601, "method not found")) == "unsupported"
    assert reconcile.classify(E("u", -32000, "header not found")) == "refused"
    assert reconcile.classify(reconcile.ChainUnavailable("connection reset")) == "transient"
    # reworded, or a code nobody listed: the explicit unknown bucket, never evidence
    assert reconcile.classify(E("u", -32602, "eth_getLogs is limited to 0 - 50 blocks range")) == "refused"
    assert reconcile.classify(E("u", -39999, "method disabled for this key")) == "refused"
    # under the catch-all, only an allowlisted marker is known; the rest is the unknown bucket
    assert reconcile.classify(E("u", -32000, "method not supported")) == "refused"
    assert reconcile.classify(E("u", -32000, "request timed out")) == "transient"
    assert reconcile.classify(E("u", -32000, "limit exceeded")) == "quota"


def test_a_quota_at_the_probe_is_not_a_classification_and_a_refusal_is_reprobed(monkeypatch):
    answers = [reconcile.RpcError("u", -32011, "request limit reached"),
               reconcile.RpcError("u", -32600, "not allowed"),
               [{"logs": []}]]
    clock = [1000.0]

    def rpc(url, method, params):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    with pytest.raises(reconcile.IncompleteRead, match="quota"):
        reconcile._require_receipts("u")            # a quota: incomplete, nothing cached
    with pytest.raises(reconcile.RpcRefused):
        reconcile._require_receipts("u")            # an unknown refusal: cached ...
    with pytest.raises(reconcile.RpcRefused):
        reconcile._require_receipts("u")            # ... and not re-asked within the window
    clock[0] += reconcile.RPC_REPROBE_SECONDS + 1
    reconcile._require_receipts("u")                # re-probed, and now served
    assert answers == []


def test_a_page_refused_with_an_unknown_code_is_rpc_refused_not_a_hole(monkeypatch):
    def rpc(url, method, params):
        raise reconcile.RpcError(url, -32602, "eth_getLogs is limited to 0 - 50 blocks range")
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    with pytest.raises(reconcile.RpcRefused, match="-32602"):
        reconcile._page(reconcile.RPC[NET], ASSET, [[reconcile.AUTH_USED_TOPIC]], 1000, 1099)


def test_final_means_l1_finalized_not_64_blocks(monkeypatch):
    """64 Base blocks are ~128 s; the batch can still be reorged on L1 (minia2auk)."""
    monkeypatch.setattr(reconcile, "PAGE_BLOCKS", 1000)
    rpc, _ = _rpc_table({(1000, 1250): []}, head=2000)             # finalized = 1900 in the fake
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    _, meta = reconcile._tiled_blocks(reconcile.RPC[NET], ASSET, [[reconcile.AUTH_USED_TOPIC]], 1000, 1250, 2000)
    assert meta["confirmed_depth"] is True and meta["finalized"] is True
    reconcile._FINALIZED.clear()
    rpc, _ = _rpc_table({(1000, 1250): []}, head=1300)             # 50 deep... and above finalized 1200
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    _, meta = reconcile._tiled_blocks(reconcile.RPC[NET], ASSET, [[reconcile.AUTH_USED_TOPIC]], 1000, 1250, 1300)
    assert meta["finalized"] is False
    reconcile._FINALIZED.clear()
    rpc, _ = _rpc_table({(1000, 1250): []}, head=1400)             # 150 deep, so past 64, but finalized is 1300
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    _, meta = reconcile._tiled_blocks(reconcile.RPC[NET], ASSET, [[reconcile.AUTH_USED_TOPIC]], 1000, 1300, 1400)
    assert meta["confirmed_depth"] is True and meta["finalized"] is True
    reconcile._FINALIZED.clear()

    def no_finality(url, method, params):
        if method == "eth_getBlockByNumber" and params[0] == "finalized":
            raise reconcile.RpcError(url, -32602, "unknown block tag")
        return rpc(url, method, params)
    monkeypatch.setattr(reconcile, "_rpc", no_finality)
    _, meta = reconcile._tiled_blocks(reconcile.RPC[NET], ASSET, [[reconcile.AUTH_USED_TOPIC]], 1000, 1250, 1400)
    assert meta["finalized"] is False, "a provider that will not say makes nothing final"


def test_two_canonical_consumers_in_the_cross_check_disagree():
    nonce = "0x" + "d7" * 32
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xfac")
    tx_fn = lambda *a, **k: ([_transfer(PAY_TO, 500000, "0xfac")], True, [], {"confirmed_depth": True, "finalized": True, "block": 500})

    def two(*a):
        raise reconcile.ChainContradiction("nonce consumed in 2 canonical txs, which cannot be: 0xfac, 0xother")
    row = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: (None, None, dict(DEEP)),
                    tx_fn=tx_fn, cross_fn=two)[nonce]
    assert row["cross_check"] == "disagrees" and "cannot be" in row["note"] and row["final"] is False
    ok = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: (None, None, dict(DEEP)),
                   tx_fn=tx_fn, cross_fn=lambda *a: ("used", "0xfac", dict(DEEP)))[nonce]
    assert ok["cross_check"] == "agrees" and ok["cross_tx"] == "0xfac"


def test_the_cross_check_is_keyed_on_authorizer_and_nonce_not_the_tx(monkeypatch):
    seen = []

    def rpc(url, method, params):
        if method == "eth_blockNumber":
            return hex(2000)
        if method == "eth_getLogs":
            seen.append(params[0])
            return []
        if method == "eth_getBlockByNumber":
            return {"number": hex(1900), "hash": "0xf"}
        raise AssertionError(method)
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    nonce = "0x" + "d8" * 32
    reconcile.__dict__["consuming_event_near"] = reconcile.consuming_event_near  # the real one
    kind, tx, meta = _REAL_NEAR(NET, ASSET, PAYER, nonce, 1500)
    assert (kind, tx) == (None, None)
    assert all(f["topics"] == [[reconcile.AUTH_USED_TOPIC, reconcile.AUTH_CANCELED_TOPIC], reconcile._pad(PAYER), nonce]
               and "transactionHash" not in f for f in seen)


def test_a_run_that_measured_nothing_is_blind_and_exits_3(tmp_path, monkeypatch):
    nonce = "0x" + "d9" * 32
    f = tmp_path / "funnel.jsonl"
    f.write_text(_line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xfac") + "\n", encoding="utf-8")
    monkeypatch.setattr(reconcile, "authorization_consumed", lambda *a: True)
    monkeypatch.setattr(reconcile, "transfers_in_tx", lambda *a, **k: (
        [_transfer(PAY_TO, 500000, "0xfac")], True, [], {"confirmed_depth": True, "finalized": True, "block": 500}))

    def blind(*a, **k):
        raise reconcile.IncompleteRead("blocks 350..449 could not be read (quota)")
    monkeypatch.setattr(reconcile, "consuming_event_near", blind)
    assert reconcile.main(["reconcile.py", str(f), "--pay-to=" + PAY_TO, "--now=2026-10-21T12:00:00Z"]) == 3
    rows, _ = reconcile.reconcile(open(f, encoding="utf-8"), now=NOW, pay_to=PAY_TO)
    assert reconcile.run_word(rows) == ("blind", 0, 1)
    monkeypatch.setattr(reconcile, "consuming_event_near", lambda *a, **k: ("used", "0xfac", dict(DEEP)))
    assert reconcile.main(["reconcile.py", str(f), "--pay-to=" + PAY_TO, "--now=2026-10-21T12:00:00Z"]) == 0


def test_every_row_records_the_endpoint_that_judged_it():
    nonce = "0x" + "da" * 32
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xfac")
    row = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer(PAY_TO, 500000, "0xfac")]))[nonce]
    assert row["rpc"] == "mainnet.base.org"


# ---- round fourteen: judged-against heads, -32000, history ----------------------------------

def test_a_permanent_header_not_found_names_a_config_fix_not_a_hole(monkeypatch):
    def rpc(url, method, params):
        raise reconcile.RpcError(url, -32000, "header not found")
    monkeypatch.setattr(reconcile, "_rpc", rpc)
    with pytest.raises(reconcile.RpcRefused, match="header not found"):
        reconcile._page(reconcile.RPC[NET], ASSET, [[reconcile.AUTH_USED_TOPIC]], 1000, 1099)


def test_a_row_carries_the_heads_it_was_judged_against():
    nonce = "0x" + "c1" * 32
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xfac")
    meta = {"confirmed_depth": True, "finalized": True, "block": 500, "head": 1100,
            "finalized_height": 600, "finalized_hash": "0xf600"}
    row = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: (None, None, dict(DEEP)),
                    tx_fn=lambda *a, **k: ([_transfer(PAY_TO, 500000, "0xfac")], True, [], dict(meta)))[nonce]
    assert (row["evidence_block"], row["head"], row["depth"], row["finalized_height"], row["finalized_hash"]) == \
        (500, 1100, 600, 600, "0xf600")
    assert row["measured"] is True and row["run"] == "clean"


def _hist_row(nonce, measured_now):
    return {"state": "confirmed", "network": NET, "asset": ASSET, "payer": PAYER, "nonce": nonce,
            "measured": measured_now, "note": ""}


def test_history_counts_consecutive_unmeasured_runs_and_marks_stale():
    nonce = "0x" + "c2" * 32
    key = reconcile._key_string(_hist_row(nonce, True))
    records = [{"at": "t0", "run": "clean", "keys": {key: True}}]
    for i in range(1, reconcile.STALE_RUNS):
        row = _hist_row(nonce, False)
        records.append(reconcile.apply_history([row], records, "t%d" % i, "blind"))
        assert row["unmeasured_runs"] == i and row["stale"] is False and row["last_measured"] == "t0"
    row = _hist_row(nonce, False)
    reconcile.apply_history([row], records, "t%d" % reconcile.STALE_RUNS, "blind")
    assert row["stale"] is True and "last measured t0" in row["note"]
    # one measured run resets it
    row = _hist_row(nonce, True)
    reconcile.apply_history([row], records, "tz", "clean")
    assert row["unmeasured_runs"] == 0 and row["stale"] is False and row["last_measured"] == "tz"


def test_the_history_file_is_appended_and_a_stale_key_exits_2(tmp_path, monkeypatch):
    nonce = "0x" + "c3" * 32
    f = tmp_path / "funnel.jsonl"
    h = tmp_path / "history.jsonl"
    f.write_text(_line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xfac") + "\n", encoding="utf-8")
    monkeypatch.setattr(reconcile, "authorization_consumed", lambda *a: True)
    monkeypatch.setattr(reconcile, "transfers_in_tx", lambda *a, **k: (
        [_transfer(PAY_TO, 500000, "0xfac")], True, [], {"confirmed_depth": True, "finalized": True, "block": 500}))
    monkeypatch.setattr(reconcile, "consuming_event_near", lambda *a, **k: ("used", "0xfac", dict(DEEP)))
    args = ["reconcile.py", str(f), "--pay-to=" + PAY_TO, "--now=2026-10-21T12:00:00Z", "--history=" + str(h)]
    assert reconcile.main(args) == 0
    assert len(h.read_text(encoding="utf-8").splitlines()) == 1

    def gone(*a, **k):
        raise reconcile.IncompleteRead("quota")
    monkeypatch.setattr(reconcile, "consuming_event_near", gone)
    codes = [reconcile.main(args) for _ in range(reconcile.STALE_RUNS)]
    assert codes == [3] * reconcile.STALE_RUNS            # blind every time; the last one is also stale
    last = json.loads(h.read_text(encoding="utf-8").splitlines()[-1])
    assert last["run"] == "blind" and list(last["keys"].values()) == [False]


# ---- round fifteen: evidence hash, finality smell, clock staleness, causes ----------------

def _receipt_rpc(receipt_hash, canonical_hash, finalized=900, head=1100):
    def rpc(url, method, params):
        if method == "eth_getTransactionReceipt":
            return {"logs": [], "blockNumber": hex(500), "blockHash": receipt_hash}
        if method == "eth_blockNumber":
            return hex(head)
        if method == "eth_getBlockByNumber":
            if params[0] == "finalized":
                return {"number": hex(finalized), "hash": "0xfin"}
            return {"hash": canonical_hash}
        raise AssertionError(method)
    return rpc


def test_final_asserts_the_evidence_block_is_on_the_finalized_chain(monkeypatch):
    """Heights ordering correctly is not the claim; the hash at that height is (minia2auk)."""
    monkeypatch.setattr(reconcile, "_rpc", _receipt_rpc("0xabc", "0xabc"))
    *_, meta = reconcile.transfers_in_tx(NET, ASSET, PAYER, "0xt", None)
    assert meta["finalized"] is True and meta["block_hash"] == "0xabc"
    reconcile._FINALIZED.clear()
    monkeypatch.setattr(reconcile, "_rpc", _receipt_rpc("0xabc", "0xother"))
    with pytest.raises(reconcile.IncompleteRead, match="not the finalized chain"):
        reconcile.transfers_in_tx(NET, ASSET, PAYER, "0xt", None)


def _tags(latest, safe, fin, lh="0xl", sh="0xs", fh="0xf"):
    return lambda url: {"latest": (latest, lh, latest * 2), "safe": (safe, sh, safe * 2) if safe else None,
                        "finalized": (fin, fh, fin * 2) if fin else None}


def test_an_endpoint_smells_on_what_it_says_about_itself_not_on_a_trend():
    """A median over its own runs absorbs a persistent clamp; depth from its own tags cannot (minia2auk)."""
    url, now = "https://rpc.example", datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    honest = reconcile.endpoint_fitness(NET, url, [], now, finality_fn=_tags(53232924, 53232876, 53232349))
    assert honest["smell"] is False and honest["depth_blocks"] == 575 and honest["safe_depth_blocks"] == 48
    as_latest = reconcile.endpoint_fitness(NET, url, [], now, finality_fn=_tags(1000, 990, 1000, fh="0xl"))
    assert as_latest["smell"] is True and "answered as latest" in as_latest["reason"]
    as_safe = reconcile.endpoint_fitness(NET, url, [], now, finality_fn=_tags(1000, 952, 952, sh="0xs", fh="0xs"))
    assert as_safe["smell"] is True and "answered as safe" in as_safe["reason"]
    # a clamp that persists is still a smell on the hundredth run: nothing here learns it
    clamped_history = [{"at": "t%d" % i, "keys": {}, "endpoints": {"rpc.example": {"depth_blocks": 0}}} for i in range(100)]
    assert reconcile.endpoint_fitness(NET, url, clamped_history, now, finality_fn=_tags(1000, 990, 1000, fh="0xl"))["smell"] is True
    # the floor is in blocks, the deployment's statement about its chain
    assert reconcile.endpoint_fitness(NET, url, [], now, floor_blocks=600,
                                      finality_fn=_tags(53232924, 53232876, 53232349))["smell"] is True
    # the band between safe and latest: finalized = latest - 50 while honest safe is 57 behind (minia2auk)
    band = reconcile.endpoint_fitness(NET, url, [], now, finality_fn=_tags(53232924, 53232867, 53232874))
    assert band["smell"] is True and "inverted ladder" in band["reason"]
    # safe == latest is its own lie
    flat = reconcile.endpoint_fitness(NET, url, [], now, finality_fn=_tags(1000, 1000, 400, sh="0xl"))
    assert flat["smell"] is True and "not doing its job" in flat["reason"]
    # same height as safe under another hash: two views, still answered as safe
    split = reconcile.endpoint_fitness(NET, url, [], now, finality_fn=_tags(1000, 952, 952, sh="0xs", fh="0xother"))
    assert split["smell"] is True and "different hash" in split["reason"]
    # no safe tag: the band is unchecked and the line says so, the latest rule still holds
    nosafe = reconcile.endpoint_fitness(NET, url, [], now, finality_fn=_tags(1000, None, 400))
    assert nosafe["smell"] is False and "cannot be checked" in nosafe["reason"]
    assert reconcile.endpoint_fitness(NET, url, [], now, finality_fn=_tags(1000, None, 999))["smell"] is True
    missing = reconcile.endpoint_fitness(NET, url, [], now, finality_fn=_tags(1000, 990, None))
    assert missing["smell"] is False and "finalized head" in missing["reason"]


def test_the_endpoint_line_tells_a_new_install_from_a_young_endpoint():
    url, now = "https://rpc.example", datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    tags = _tags(53232924, 53232876, 53232349)
    assert "new install" in reconcile.endpoint_fitness(NET, url, [], now, finality_fn=tags)["history"]
    other = [{"at": "2026-10-28T17:43:44+00:00", "keys": {}, "endpoints": {"elsewhere.example": {}}}]
    assert "first run through this endpoint in 1 recorded runs" in reconcile.endpoint_fitness(NET, url, other, now, finality_fn=tags)["history"]
    mine = other + [{"at": "2026-10-28T18:00:00+00:00", "keys": {}, "endpoints": {"rpc.example": {}}}]
    assert "1 recorded runs through this endpoint since 2026-10-28T18:00:00" in reconcile.endpoint_fitness(NET, url, mine, now, finality_fn=tags)["history"]


def test_a_smelly_endpoint_fails_once_and_its_rows_are_not_judged(tmp_path, monkeypatch, capsys):
    nonces = ["0x" + c * 32 for c in ("b4", "b5", "b6")]
    f = tmp_path / "funnel.jsonl"
    h = tmp_path / "history.jsonl"
    f.write_text("\n".join(_line("settled", n, "2026-10-21T10:00:00+00:00", tx="0xfac") for n in nonces) + "\n", encoding="utf-8")
    monkeypatch.setattr(reconcile, "endpoint_finality", _tags(1000, 990, 1000, fh="0xl"))
    judged = []
    monkeypatch.setattr(reconcile, "authorization_consumed", lambda *a: judged.append(a) or True)
    code = reconcile.main(["reconcile.py", str(f), "--pay-to=" + PAY_TO, "--now=2026-10-21T12:00:00Z", "--history=" + str(h)])
    out = capsys.readouterr().out
    assert code == 3 and judged == [], "no row was judged through a smelly endpoint"
    assert out.count("SMELL") == 1 and "3 row(s) not judged" in out and "measured 0 of 3" in out
    last = json.loads(h.read_text(encoding="utf-8").splitlines()[-1])
    assert last["endpoints"]["mainnet.base.org"]["smell"] is True and last["run"] == "blind"
    assert "lag_s" not in last["endpoints"]["mainnet.base.org"], "no clock in the record the verdict rests on"


def test_the_row_names_its_clock_fields():
    nonce = "0x" + "b7" * 32
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xfac")
    row = _by_nonce([line], pay_to=PAY_TO, **_chain(transfers=[_transfer(PAY_TO, 500000, "0xfac")]))[nonce]
    assert row["clock_fields"] == ["read_at"] and row["read_at"] == NOW.isoformat(timespec="seconds")


def test_stale_fires_on_the_clock_even_when_runs_are_rare():
    nonce = "0x" + "b2" * 32
    key = reconcile._key_string(_hist_row(nonce, True))
    records = [{"at": "2026-10-28T10:00:00+00:00", "run": "clean", "keys": {key: True}}]
    row = _hist_row(nonce, False)
    reconcile.apply_history([row], records, "2026-10-28T11:00:00+00:00", "partial")    # 60 min, 1 run
    assert row["stale"] is False
    row = _hist_row(nonce, False)
    reconcile.apply_history([row], records, "2026-10-28T12:00:00+00:00", "partial")    # 120 min, still 1 run
    assert row["stale"] is True and "120 min" in row["note"]


def test_quota_and_transient_both_count_as_unmeasured_and_name_themselves():
    nonce = "0x" + "b3" * 32
    line = _line("settled", nonce, "2026-10-21T10:00:00+00:00", tx="0xfac")
    tx_fn = lambda *a, **k: ([_transfer(PAY_TO, 500000, "0xfac")], True, [], {"confirmed_depth": True, "finalized": True, "block": 500})
    for cause in ("quota", "transient"):
        def broken(*a, c=cause):
            raise reconcile.IncompleteRead("blocks 350..449 could not be read", cause=c)
        row = _by_nonce([line], pay_to=PAY_TO, consumed_fn=lambda *a: True, event_fn=lambda *a: (None, None, dict(DEEP)),
                        tx_fn=tx_fn, cross_fn=broken)[nonce]
        assert row["measured"] is False and row["unmeasured_cause"] == cause


# ---- round nineteen: compare endpoints, not constants ----------------------------------------

def _peers(table, safe_depth=None, latest_depth=None):
    """table: {url: (finalized_height, hash)}; each peer's safe and latest sit the given depth above it."""
    def heads(url):
        if url not in table:
            return None
        n, h = table[url]
        got = {"finalized": (n, h, 0)}
        if safe_depth is not None:
            got["safe"] = (n + safe_depth, "0xsafe", 0)
        if latest_depth is not None:
            got["latest"] = (n + latest_depth, "0xtip", 0)
        return got
    return heads


def _silent(url, height):
    """header_fn for an endpoint that answers no height: nothing is witnessed or compared."""
    return None


def test_a_clamp_just_below_safe_leads_by_most_of_the_gap():
    """Peers' finalized is ~570 below their safe; the clamp sits one block under safe."""
    fit = {"finalized_height": 53274399 + 569, "finalized_hash": "0xclamp"}
    peers = {"https://a": (53274399, "0x48fd"), "https://b": (53274228, "0xb9fe"), "https://c": (53272688, "0x4749")}
    slept = []
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620), header_fn=_silent, sleep=slept.append)
    assert out["smell"] is True and "a clamp" in out["line"] and slept == [10]
    assert "re-read 10 s later" in out["line"]


def test_an_honest_lead_of_one_finality_step_is_drift_not_a_clamp():
    """Live, 31 Oct: mainnet.base.org led blastapi by 186 blocks for about 290 s, and every
    other peer alternated between the two heights. Ten seconds does not separate that from a
    clamp; the size of the lead against the peers' own gap does."""
    fit = {"finalized_height": 53317988, "finalized_hash": "0xnew"}
    peers = {"https://publicnode": (53317802, "0xold"), "https://blast": (53317802, "0xold"),
             "https://1rpc": (53316253, "0xolder")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620), header_fn=_silent, sleep=lambda s: None)
    assert out["smell"] is False and "drift, not a clamp" in out["line"] and "leads the field by 186" in out["line"]
    assert "publicnode (53317802, 53318372, 53318422)" in out["line"], "each peer's triple is printed"


def test_a_finalized_block_above_every_peer_tip_is_fabricated():
    fit = {"finalized_height": 2000, "finalized_hash": "0xfuture"}
    peers = {"https://a": (1000, "0xa"), "https://b": (990, "0xb")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620), header_fn=_silent, sleep=lambda s: None)
    assert out["smell"] is True and "no independent endpoint has produced" in out["line"]


def test_a_lead_with_no_safe_to_size_it_is_stated_not_judged():
    fit = {"finalized_height": 1500, "finalized_hash": "0xmine"}
    peers = {"https://a": (1000, "0xa"), "https://b": (990, "0xb")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers), header_fn=_silent, sleep=lambda s: None)
    assert out["smell"] is False and "not judged" in out["line"]


def test_quorum_is_required_of_the_re_read_the_verdict_rests_on():
    """minia2auk, round 20: read 1 a=900 b=950, read 2 a=980 b=None. The re-read
    has one peer, so it is no quorum - not a clamp, and not the first read reused."""
    fit = {"finalized_height": 1000, "finalized_hash": "0xmine"}
    reads = iter([{"https://a": (900, "0xa"), "https://b": (950, "0xb")}, {"https://a": (980, "0xa")}])
    current = {}

    def heads(url):
        return _peers(current, 570, 620)(url)

    def sleep(seconds):
        current.clear()
        current.update(next(reads))
    current.update(next(reads))
    out = reconcile.endpoint_crosscheck(fit, ["https://a", "https://b"], finality_fn=heads, sleep=sleep)
    assert out["smell"] is False and "answered the re-read stably; no quorum" in out["line"] and out["answered"] == 1
    assert "unstable, dropped: b (950 -> no answer)" in out["line"], "the peer that left is named"


def _scripted(reads):
    """finality_fn and sleep that serve reads[0] until the re-read, then reads[1].
    Each read is {url: {"finalized": n, "safe": n or None, "latest": n or None}}."""
    state = {"i": 0}

    def heads(url):
        got = reads[state["i"]].get(url)
        if got is None:
            return None
        return {tag: (got[tag], "0x%s" % tag, 0) for tag in ("finalized", "safe", "latest") if got.get(tag) is not None}

    def sleep(seconds):
        state["i"] = 1
    return heads, sleep


def test_the_gap_never_pairs_one_peers_safe_with_anothers_finalized():
    """minia2auk, round 21: A safe 1000 / finalized 900, B finalized 1005 and no safe.
    max(safe) - max(finalized) = -5, and any lead fired as a clamp. B has no ladder,
    and A's is stale (B finalized past A's safe), so nothing sizes the lead."""
    read = {"https://a": {"finalized": 900, "safe": 1000, "latest": 1050},
            "https://b": {"finalized": 1005, "safe": None, "latest": 1060}}
    heads, sleep = _scripted([read, read])
    out = reconcile.endpoint_crosscheck({"finalized_height": 1010, "finalized_hash": "0x"}, list(read),
                                        finality_fn=heads, header_fn=_silent, sleep=sleep)
    # B has finalized past A's safe, so A's ladder is stale and sizes nothing
    assert out["smell"] is False and "not judged" in out["line"] and "b (1005, None, 1060)" in out["line"]
    read["https://a"]["safe"] = 1200
    heads, sleep = _scripted([read, read])
    out = reconcile.endpoint_crosscheck({"finalized_height": 1010, "finalized_hash": "0x"}, list(read),
                                        finality_fn=heads, header_fn=_silent, sleep=sleep)
    # minia2auk, round 22: 5 ahead of the field and 110 into a's ladder are two findings
    assert out["smell"] is False and "leads the field by 5 (highest peer finalized 1005 at b)" in out["line"]
    assert "110 into a's ladder (900, 1200)" in out["line"] and "gap of 300" in out["line"]


def test_a_peer_whose_finalized_goes_backwards_is_dropped_by_name():
    """Live, 31 Oct: publicnode answered 53317988, then 53317802, from one URL."""
    first = {"https://publicnode": {"finalized": 53317988, "safe": 53318560, "latest": 53318610},
             "https://blast": {"finalized": 53317802, "safe": 53318560, "latest": 53318610},
             "https://drpc": {"finalized": 53317802, "safe": 53318560, "latest": 53318610}}
    second = dict(first, **{"https://publicnode": {"finalized": 53317802, "safe": 53318560, "latest": 53318610}})
    heads, sleep = _scripted([first, second])
    out = reconcile.endpoint_crosscheck({"finalized_height": 53317990, "finalized_hash": "0x"}, list(first),
                                        finality_fn=heads, header_fn=_silent, sleep=sleep)
    assert "unstable, dropped: publicnode (53317988 -> 53317802)" in out["line"]
    assert out["answered"] == 2 and out["smell"] is False


def _finalized_chain(hashes, calls=None):
    """header_fn for one canonical chain: {height: hash}, None above or between them."""
    def header(url, height):
        if calls is not None:
            calls.append((url, height))
        return (height, hashes[height], 0) if height in hashes else None
    return header


def test_drift_is_not_a_clamp_and_hashes_decide_at_one_height():
    """Live, 30 Oct: publicnode and drpc at 53274399, mainnet.base.org and blastapi at 53274228, 1rpc 53272688."""
    fit = {"finalized_height": 53274399, "finalized_hash": "0x48fd"}
    peers = {"https://drpc": (53274399, "0x48fd"), "https://base": (53274228, "0xb9fe"), "https://1rpc": (53272688, "0x4749")}
    chain = _finalized_chain({53274399: "0x48fd", 53274228: "0xb9fe", 53272688: "0x4749"})
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers), header_fn=chain, sleep=lambda s: None)
    assert out["smell"] is False and "agrees with 1 peer(s), 2 behind (drift)" in out["line"]
    assert "every finalized hash lies on one chain" in out["line"]
    # primary behind a peer: that peer is asked for its hash at the primary's height
    fit = {"finalized_height": 53274228, "finalized_hash": "0xb9fe"}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers), header_fn=chain, sleep=lambda s: None)
    assert out["smell"] is False and "agrees with 2 peer(s)" in out["line"]


def test_a_different_hash_at_one_height_by_majority_is_a_fork():
    fit = {"finalized_height": 1000, "finalized_hash": "0xmine"}
    peers = {"https://a": (1000, "0xreal"), "https://b": (1000, "0xreal"), "https://c": (1000, "0xmine")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers), sleep=lambda s: None)
    assert out["smell"] is True and "fork or a fabricated block" in out["line"]


def test_fewer_than_two_peers_is_no_quorum_not_a_verdict():
    fit = {"finalized_height": 1000, "finalized_hash": "0xmine"}
    out = reconcile.endpoint_crosscheck(fit, ["https://a", "https://b"], finality_fn=_peers({"https://a": (900, "0x")}),
                                        sleep=lambda s: None)
    assert out["smell"] is False and "no quorum" in out["line"]


def test_the_ladder_is_the_freshest_not_the_one_behind_with_the_top_safe():
    """minia2auk, round 22: ranking by finalized + safe (= 2*finalized + gap) let a peer behind
    on finalized but carrying a higher safe size the bar. Safe cannot pass the tip, so the stale
    peer wins only while it lags by less than the fresh peer's safe-to-tip distance. That
    distance is a property of the chain, not of this rule: 45-60 blocks on Base, read from
    base-rpc.publicnode.com on 30-31 Oct 2026, where the effect is small. A chain with a longer
    safe-to-tip distance admits a larger inversion under the old key. The current key ranks by
    finalized alone and needs no such bound anywhere. Here 1293 is past fresh's midpoint (1290) and short of
    stale's (1295), so the old key called a clamp drift."""
    read = {"https://fresh": {"finalized": 1000, "safe": 1580, "latest": 1630},
            "https://stale": {"finalized": 960, "safe": 1630, "latest": 1630}}
    heads, sleep = _scripted([read, read])
    out = reconcile.endpoint_crosscheck({"finalized_height": 1293, "finalized_hash": "0x"}, list(read),
                                        finality_fn=heads, header_fn=_silent, sleep=sleep)
    assert out["smell"] is True and "293 into fresh's ladder (1000, 1580)" in out["line"], out["line"]



# ---- round twenty-four: the safety assumption, checked rather than inherited ----------------

def test_two_peers_at_one_finalized_height_with_two_hashes_is_undetermined():
    """minia2auk, round 24: a disagreement on height is a lag; agreement on height with two
    hashes is not, and no vote between the two settles it. The primary agrees with one of them
    and is ahead of neither, which used to read as agreement with a disagreeing minority."""
    fit = {"finalized_height": 1000, "finalized_hash": "0xaaa"}
    peers = {"https://a": (1000, "0xaaa"), "https://b": (1200, "0xb12"), "https://c": (1200, "0xc12")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620),
                                        header_fn=_finalized_chain({1000: "0xaaa"}), sleep=lambda s: None)
    assert out["smell"] is True and out["undetermined"] is True
    assert out["line"].startswith("cross-check: undetermined") and "height 1200: 0xb12 finalized at b vs 0xc12 finalized at c" in out["line"]


def test_a_peer_overstating_finalized_on_another_chain_does_not_win_on_height():
    """The case comparing equal heights cannot see: the forked peer advertises a higher
    finalized than anyone, so it shares no height with them. Its own chain is asked for the
    others' finalized heights and reports different blocks there."""
    fit = {"finalized_height": 1000, "finalized_hash": "0xaaa", "host": "primary"}
    peers = {"https://a": (1000, "0xaaa"), "https://b": (990, "0x990"), "https://fork": (1400, "0xf14")}
    fork_chain = _finalized_chain({1000: "0xf10", 990: "0xf99"})
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620),
                                        header_fn=fork_chain, sleep=lambda s: None)
    assert out["smell"] is True and out["undetermined"] is True
    assert "height 1000: 0xaaa finalized at primary, a vs 0xf10 on fork's chain (anchor, unwitnessed)" in out["line"], out["line"]
    assert "height 990: 0x990 finalized at b vs 0xf99 on fork's chain (anchor, unwitnessed)" in out["line"]


def test_a_leading_primary_is_asked_for_the_peers_finalized_blocks():
    """In the lead branch the primary is the highest claim, so its own chain is asked for
    each peer's finalized height: a lead on a fork is a contradiction, not drift."""
    fit = {"finalized_height": 53317988, "finalized_hash": "0xnew"}
    peers = {"https://publicnode": (53317802, "0xold"), "https://blast": (53317802, "0xold"),
             "https://1rpc": (53316253, "0xolder")}
    calls = []
    honest = _finalized_chain({53317988: "0xnew", 53317802: "0xold", 53316253: "0xolder"}, calls)
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620), header_fn=honest,
                                        sleep=lambda s: None, primary_url="https://primary")
    assert out["smell"] is False and "drift, not a clamp" in out["line"]
    assert "its top block (53317988 at primary) witnessed by publicnode" in out["line"], out["line"]
    assert out["witness"] == {"endpoint": "publicnode", "operator_hint": "publicnode",
                              "operator_hint_basis": {"host": "publicnode", "suffix": "", "rule": "single label"},
                              "shares_operator_hint_with_anchor": False, "height": 53317988,
                              "how": "reports the block below its safe head 53318372"}
    assert sorted(calls) == [("https://primary", 53316253), ("https://primary", 53317802),
                             ("https://publicnode", 53317988)], "one witness call, then one call per lower height"
    forked = _finalized_chain({53317802: "0xfork", 53316253: "0xolder"})
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620), header_fn=forked,
                                        sleep=lambda s: None, primary_url="https://primary")
    assert out["smell"] is True and "height 53317802: 0xold finalized at publicnode, blast vs 0xfork" in out["line"]


def test_a_height_the_highest_claimant_does_not_answer_is_not_a_contradiction():
    """A load-balanced backend behind the asked URL may not have the height: an absence, not a finding."""
    fit = {"finalized_height": 1000, "finalized_hash": "0xaaa"}
    peers = {"https://a": (1000, "0xaaa"), "https://b": (1200, "0xb12"), "https://c": (900, "0x900")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620),
                                        header_fn=lambda url, h: None, sleep=lambda s: None)
    assert out["smell"] is False and "undetermined" not in out["line"]
    assert "not compared at height(s) 900 (b: no block), 1000 (b: no block)" in out["line"], \
        "an absence is stated, not passed off as agreement"


# ---- round twenty-five: the anchor is a claim with a witness, not an assumption -------------

def _per_endpoint(chains):
    """header_fn serving each url its own chain: {url: {height: hash}}."""
    return lambda url, height: (height, chains[url][height], 0) if height in chains.get(url, {}) else None


def test_an_invented_top_claim_below_another_peers_tip_is_contradicted_by_a_witness():
    """minia2auk, round 25: A, B, C honest at 1000 / 1002 / 1005; D claims 1008 with an invented
    hash and answers lower heights from its real chain; E is honest, finalized 1005, tip 1010.
    Fabricated-first does not fire (1008 is below E's tip), and every comparison was made
    against D - the one value that met no second source. The run printed "every finalized
    hash lies on one chain"."""
    real = {1000: "0xa10", 1002: "0xa12", 1005: "0xa15", 1008: "0xa18"}
    fit = {"finalized_height": 1000, "finalized_hash": "0xa10", "host": "a"}
    peers = {"https://b": (1002, "0xa12"), "https://c": (1005, "0xa15"), "https://d": (1008, "0xd18"),
             "https://e": (1005, "0xa15")}
    chains = {url: real for url in peers}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 5), header_fn=_per_endpoint(chains),
                                        sleep=lambda s: None, primary_url="https://a")
    assert out["smell"] is True and out["undetermined"] is True, out["line"]
    assert "height 1008: 0xd18 finalized at d (anchor) vs 0xa18 at c (witness, safe 1575)" in out["line"], out["line"]
    assert [side["role"] for side in out["contradictions"][0]["sides"]] == ["anchor", "witness"]


def test_a_top_claim_nobody_else_reports_is_stated_unwitnessed():
    """No other endpoint has the anchor's block yet: an absence, stated on the line, not passed
    off as one chain and not called a contradiction."""
    real = {1000: "0xa10", 1005: "0xa15"}
    fit = {"finalized_height": 1000, "finalized_hash": "0xa10", "host": "a"}
    peers = {"https://b": (1000, "0xa10"), "https://d": (1005, "0xa15")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620),
                                        header_fn=_per_endpoint({"https://b": {1000: "0xa10"}, "https://d": real}),
                                        sleep=lambda s: None, primary_url="https://a")
    assert out["smell"] is False
    assert ("the top claim (1005 at d) has no second witness: 1 endpoint(s) whose safe head had reached 1005 "
            "gave no block there (b at safe 1570: no block), so the chain it anchors is unconfirmed") in out["line"], out["line"]
    assert out["witness"] is None


def test_two_claims_at_the_top_witness_each_other_without_a_call():
    real = {1000: "0xa10", 1005: "0xa15"}
    calls = []
    fit = {"finalized_height": 1000, "finalized_hash": "0xa10", "host": "a"}
    peers = {"https://c": (1005, "0xa15"), "https://d": (1005, "0xa15")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620),
                                        header_fn=_finalized_chain(real, calls), sleep=lambda s: None, primary_url="https://a")
    assert out["smell"] is False and "its top block (1005 at c) witnessed by d" in out["line"], out["line"]
    assert all(h != 1005 for _, h in calls), "the witness cost no call"


# ---- round twenty-six: an eligible witness, and roles a machine can read --------------------

def test_no_endpoint_whose_safe_head_reached_the_anchor_is_said_so_without_a_call():
    """minia2auk, round 26: "no peer had reached the height" and "we asked a peer that was
    behind" are different facts. The anchor is the highest finalized claim, so no peer's
    finalized can have reached it; the bound is the peer's safe head. Here every peer's safe
    is below 1100, so nobody is asked and the line says why."""
    calls = []
    fit = {"finalized_height": 1000, "finalized_hash": "0xa10", "host": "a"}
    peers = {"https://b": (1000, "0xa10"), "https://d": (1100, "0xd11")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 50, 60),
                                        header_fn=_finalized_chain({1000: "0xa10", 1100: "0xd11"}, calls),
                                        sleep=lambda s: None, primary_url="https://a")
    assert "no second witness: no other endpoint's safe head had reached 1100" in out["line"], out["line"]
    assert all(h != 1100 for _, h in calls), "an ineligible endpoint is not asked"


def test_an_endpoint_below_the_anchor_on_safe_is_skipped_for_one_that_has_reached_it():
    calls = []
    real = {990: "0x990", 1000: "0xa10", 1005: "0xa15"}
    fit = {"finalized_height": 1000, "finalized_hash": "0xa10", "host": "a"}
    read = {"https://lag": {"finalized": 1000, "safe": 1003, "latest": 1010},
            "https://ok": {"finalized": 990, "safe": 1400, "latest": 1450},
            "https://d": {"finalized": 1005, "safe": 1500, "latest": 1550}}
    heads = lambda url: {tag: (read[url][tag], {"finalized": {1000: "0xa10", 990: "0x990", 1005: "0xa15"}[read[url]["finalized"]]}
                               .get(tag, "0x%s" % tag), 0) for tag in ("finalized", "safe", "latest")}
    out = reconcile.endpoint_crosscheck(fit, list(read), finality_fn=heads, header_fn=_finalized_chain(real, calls),
                                        sleep=lambda s: None, primary_url="https://a")
    assert "its top block (1005 at d) witnessed by ok" in out["line"], out["line"]
    assert ("https://lag", 1005) not in calls and ("https://ok", 1005) in calls


def test_a_contradiction_says_which_side_was_witnessed():
    """An ancestry mismatch carries the anchor's status, so a reader can weigh it without the
    docstring: witnessed by a second endpoint, or one unverified source."""
    real = {1000: "0xa10", 1005: "0xa15"}
    fit = {"finalized_height": 1005, "finalized_hash": "0xa15", "host": "a"}
    peers = {"https://c": (1005, "0xa15"), "https://b": (1000, "0xbad")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620),
                                        header_fn=_finalized_chain(real), sleep=lambda s: None)
    assert out["smell"] is True
    assert "0xbad finalized at b vs 0xa10 on c's chain (anchor, witnessed by a)" in out["line"], out["line"]
    sides = out["contradictions"][0]["sides"]
    assert sides[0]["role"] == "finalized claim" and sides[1]["role"] == "anchor, witnessed by a"


def test_the_witness_is_kept_in_the_history_record(monkeypatch, tmp_path):
    """The identity is the cheap part to keep: one call decides, and the record stays auditable."""
    fit = {"host": "primary", "network": NET, "finalized_height": 1000, "finalized_hash": "0xa10",
           "depth_blocks": 600, "depth_s": 1200, "safe_depth_blocks": 50, "smell": False, "reason": "", "history": ""}
    monkeypatch.setattr(reconcile, "endpoint_fitness", lambda *a, **k: dict(fit))
    monkeypatch.setattr(reconcile, "endpoint_crosscheck",
                        lambda f, peers, **k: {"line": "cross-check: ok", "smell": False,
                                               "witness": {"endpoint": "drpc", "height": 1000, "how": "finalized the same block"},
                                               "no_witness": [{"endpoint": "1rpc", "safe": 1050, "outcome": "error (RpcError)"}]})
    monkeypatch.setattr(reconcile, "RPC", {NET: "https://primary"})
    monkeypatch.delenv("SATLEDGER_RECONCILE_RPC", raising=False)
    monkeypatch.setenv("SATLEDGER_FINALITY_SAMPLE", "%s=https://drpc|https://base" % NET)
    monkeypatch.setattr(reconcile, "reconcile", lambda *a, **k: ([], 0))
    log = tmp_path / "funnel.jsonl"
    log.write_text(_line("settled", "0x" + "c1" * 32, "2026-10-21T10:00:00+00:00", tx="0xfac") + "\n")
    history = tmp_path / "history.jsonl"
    reconcile.main(["reconcile.py", str(log), "--history=%s" % history])
    record = json.loads(history.read_text().splitlines()[-1])
    assert record["endpoints"]["primary"]["witness"]["endpoint"] == "drpc"
    assert record["endpoints"]["primary"]["no_witness"] == [{"endpoint": "1rpc", "safe": 1050, "outcome": "error (RpcError)"}], \
        "a peer that fails run after run is visible across the history, by name"


# ---- round twenty-seven: the failing side has a name, and two kinds of failure --------------

def test_an_eligible_peer_that_errors_is_named_apart_from_one_with_no_block():
    """minia2auk, round 27: a count says how many peers gave no block, not which; and an RPC
    error (the peer is failing) and a successful answer without the block (pruned, or lagging
    inside its own safe) say different things about the peer."""
    fit = {"finalized_height": 1000, "finalized_hash": "0xa10", "host": "a"}
    peers = {"https://broken": (1000, "0xa10"), "https://pruned": (990, "0x990"), "https://d": (1005, "0xa15")}

    def header(url, height):
        if url == "https://broken":
            raise reconcile.RpcError(url, -32000, "upstream gone", 502)
        if url == "https://pruned":
            return None
        return {1005: (1005, "0xa15", 0), 1000: (1000, "0xa10", 0), 990: (990, "0x990", 0)}.get(height)
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620), header_fn=header,
                                        sleep=lambda s: None, primary_url="https://a")
    assert out["witness"] is None
    single = lambda name: {"host": name, "suffix": "", "rule": "single label"}
    assert out["no_witness"] == [
        {"endpoint": "broken", "operator_hint": "broken", "operator_hint_basis": single("broken"),
         "safe": 1570, "outcome": "error (RpcError)"},
        {"endpoint": "pruned", "operator_hint": "pruned", "operator_hint_basis": single("pruned"),
         "safe": 1560, "outcome": "no block"}], out["no_witness"]
    assert "(broken at safe 1570: error (RpcError); pruned at safe 1560: no block)" in out["line"], out["line"]
    assert "upstream gone" not in out["line"] and "https://" not in out["line"], \
        "only the error's class is kept: a message can carry an endpoint URL with a key in it"


def test_the_default_reader_raises_on_an_rpc_failure_instead_of_reading_it_as_no_block(monkeypatch):
    def failing(url, method, params):
        raise reconcile.ChainUnavailable("%s: connection refused" % url)
    monkeypatch.setattr(reconcile, "_rpc", failing)
    with pytest.raises(reconcile.ChainUnavailable):
        reconcile._header_at("https://peer", 1005)
    monkeypatch.setattr(reconcile, "_rpc", lambda url, method, params: None)
    assert reconcile._header_at("https://peer", 1005) is None


# ---- round twenty-eight: roles count endpoints; an operator hint groups them ----------------

def test_the_operator_hint_is_the_registrable_domain_and_says_no_more():
    """minia2auk, round 28: two endpoints behind one provider are one voice. The hostname is
    already in hand, and a domain suffix is a cheaper grouping key than an identity claim."""
    hint = reconcile.operator_hint
    assert hint("base-mainnet.g.alchemy.com") == hint("base.alchemy.com") == "alchemy.com"
    assert hint("base-rpc.publicnode.com:443") == "publicnode.com"
    assert hint("rpc.example.co.uk") == "example.co.uk" and hint("node.example.com.au") == "example.com.au"
    assert hint("mainnet.base.org") == "base.org" and hint("1rpc.io") == "1rpc.io"
    assert hint("user:secret@rpc.example.com") == "example.com"
    assert hint("203.0.113.7:8545") == "203.0.113.7" and hint("[2001:db8::1]:8545") == "[2001:db8::1]"
    assert hint("primary") == "primary"
    assert "not a proof" in " ".join(reconcile.operator_hint.__doc__.split())


def test_a_witness_of_the_anchors_own_operator_is_flagged_as_possibly_one_source():
    """An anchor and a witness on one provider read as two independent voices while being one,
    so the witness says it shares the anchor's hint and the line says so."""
    real = {1000: "0xa10", 1005: "0xa15"}
    fit = {"finalized_height": 1000, "finalized_hash": "0xa10", "host": "primary.example.net"}
    peers = {"https://base.alchemy.com": (1005, "0xa15"), "https://base-mainnet.g.alchemy.com": (1000, "0xa10")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620),
                                        header_fn=_finalized_chain(real), sleep=lambda s: None,
                                        primary_url="https://primary.example.net")
    assert out["witness"]["operator_hint"] == "alchemy.com"
    assert out["witness"]["shares_operator_hint_with_anchor"] is True
    assert ("witnessed by base-mainnet.g.alchemy.com (same operator hint as the anchor, alchemy.com: "
            "possibly one source, not two)") in out["line"], out["line"]


def test_every_side_and_every_silent_peer_carries_its_operator_hints():
    real = {1000: "0xa10", 1005: "0xa15"}
    fit = {"finalized_height": 1005, "finalized_hash": "0xa15", "host": "a.drpc.org"}
    peers = {"https://b.drpc.org": (1005, "0xa15"), "https://c.blast.io": (1000, "0xbad")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620),
                                        header_fn=_finalized_chain(real), sleep=lambda s: None)
    sides = out["contradictions"][0]["sides"]
    assert [s["operator_hints"] for s in sides] == [["blast.io"], ["drpc.org"]]
    assert out["witness"]["shares_operator_hint_with_anchor"] is True

    fit = {"finalized_height": 1000, "finalized_hash": "0xa10", "host": "a"}
    peers = {"https://x.llamarpc.com": (1000, "0xa10"), "https://y.llamarpc.com": (990, "0x990"),
             "https://d.example.org": (1005, "0xa15")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620),
                                        header_fn=lambda url, height: None, sleep=lambda s: None, primary_url="https://a")
    assert [w["operator_hint"] for w in out["no_witness"]] == ["llamarpc.com", "llamarpc.com"], \
        "two silent endpoints, one operator: the record shows it"


# ---- round twenty-nine: the basis rides with the hint; a mismatch is not a vote ---------------

def test_every_hint_carries_its_basis_so_a_missing_suffix_shows_on_the_row():
    """minia2auk, round 29: the short suffix list is applied to hostnames nobody here chose,
    and a suffix it lacks splits or merges operators silently. With the basis on the row, the
    miss is readable: here `ltd.uk` is not in the list, so two unrelated companies under it
    share the hint `ltd.uk` - and the row says the rule was `last two labels`, suffix `uk`."""
    basis = reconcile.operator_hint_basis
    assert basis("base-mainnet.g.alchemy.com") == {"hint": "alchemy.com", "host": "base-mainnet.g.alchemy.com",
                                                   "suffix": "com", "rule": "last two labels"}
    assert basis("rpc.example.co.uk")["rule"] == "shared second level"
    assert basis("rpc.example.co.uk")["suffix"] == "co.uk"
    missed = basis("rpc.acme.ltd.uk")
    assert missed["hint"] == "ltd.uk" and missed["suffix"] == "uk" and missed["rule"] == "last two labels"
    assert basis("203.0.113.7:8545")["rule"] == basis("[2001:db8::1]")["rule"] == "ip literal"

    real = {1000: "0xa10", 1005: "0xa15"}
    fit = {"finalized_height": 1005, "finalized_hash": "0xa15", "host": "a.drpc.org"}
    peers = {"https://b.drpc.org": (1005, "0xa15"), "https://c.blast.io": (1000, "0xbad")}
    out = reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620),
                                        header_fn=_finalized_chain(real), sleep=lambda s: None)
    assert out["witness"]["operator_hint_basis"] == {"host": "b.drpc.org", "suffix": "org", "rule": "last two labels"}
    sides = out["contradictions"][0]["sides"]
    assert sides[0]["operator_hint_bases"] == {"c.blast.io": {"host": "c.blast.io", "suffix": "io",
                                                              "rule": "last two labels"}}


def test_a_different_hint_is_the_absence_of_a_warning_not_a_second_source():
    """A shared hint is evidence of dependence; a different one is not evidence of
    independence. So the verdict and the line are the same whether or not the witness's hint
    differs from the anchor's, except for the warning when it does not."""
    real = {1000: "0xa10", 1005: "0xa15"}

    def run(anchor_host, witness_host):
        fit = {"finalized_height": 1000, "finalized_hash": "0xa10", "host": "primary.example.net"}
        peers = {"https://%s" % anchor_host: (1005, "0xa15"), "https://%s" % witness_host: (1000, "0xa10")}
        return reconcile.endpoint_crosscheck(fit, list(peers), finality_fn=_peers(peers, 570, 620),
                                             header_fn=_finalized_chain(real), sleep=lambda s: None,
                                             primary_url="https://primary.example.net")
    shared, apart = run("base.alchemy.com", "eth.alchemy.com"), run("base.alchemy.com", "base.llamarpc.com")
    assert shared["smell"] == apart["smell"] is False
    assert "possibly one source" in shared["line"]
    assert "possibly one source" not in apart["line"]
    assert "independent" not in apart["line"] and "two sources" not in apart["line"]
