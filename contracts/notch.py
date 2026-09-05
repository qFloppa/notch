# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import datetime
import hashlib
import json
from dataclasses import dataclass

from genlayer import *

ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_TRANSIENT = "[TRANSIENT]"
ERROR_LLM = "[LLM_ERROR]"

CLAIM_KINDS = ("not_delivered", "off_spec", "overcharged", "duplicate", "sla_breach")
OUTCOMES = ("upheld", "adjusted", "rejected")
PRECEDENT_CAP = 5

STATUS_OPEN = "open"
STATUS_ACCEPTED = "accepted"
STATUS_SETTLED = "settled"
STATUS_DISPUTED = "disputed"


@allow_storage
@dataclass
class LineItem:
    tab_id: str
    payer: Address
    payee: Address
    atto: u256
    memo: str
    evidence_uri: str
    evidence_hash: str
    claim_kind: str
    cycle: u256


@allow_storage
@dataclass
class Tab:
    creator: Address
    cycle_seconds: u256
    cycle: u256
    opened_at: str
    members: DynArray[Address]
    notch_ids: DynArray[str]


@allow_storage
@dataclass
class Statement:
    tab_id: str
    cycle: u256
    closed_at: str
    closed_by: Address
    statement_hash: str
    status: str
    settle_ref: str
    legs: DynArray[str]
    notch_ids: DynArray[str]


@allow_storage
@dataclass
class Dispute:
    statement_id: str
    claimant: Address
    claim_kind: str
    claim: str
    bond_atto: u256
    status: str
    outcome: str
    adjusted_atto: u256
    evidence_hash_matched: bool
    rationale: str
    opened_at: str
    notch_ids: DynArray[str]
    cited: DynArray[str]


class Notch(gl.Contract):
    bond_atto: u256
    dispute_window_seconds: u256
    tabs: TreeMap[str, Tab]
    items: TreeMap[str, LineItem]
    statements: TreeMap[str, Statement]
    disputes: TreeMap[str, Dispute]

    def __init__(self, bond_atto: u256, dispute_window_seconds: u256) -> None:
        if dispute_window_seconds == 0:
            # A zero window makes every statement final the instant it closes,
            # so nobody could ever dispute one. `open_tab` guards the analogous
            # `cycle_seconds == 0`.
            raise gl.vm.UserError(f"{ERROR_EXPECTED} zero window")
        self.bond_atto = bond_atto
        self.dispute_window_seconds = dispute_window_seconds

    @gl.public.view
    def get_bond_atto(self) -> int:
        return self.bond_atto

    @gl.public.view
    def get_dispute_window_seconds(self) -> int:
        return self.dispute_window_seconds

    @gl.public.write
    def open_tab(self, tab_id: str, members: list[str], cycle_seconds: u256) -> None:
        if tab_id in self.tabs:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} tab exists")
        if len(members) < 2:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} need two members")
        if cycle_seconds == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} zero cycle")
        t = self.tabs.get_or_insert_default(tab_id)
        t.creator = gl.message.sender_address
        t.cycle_seconds = cycle_seconds
        t.cycle = u256(0)
        t.opened_at = gl.message_raw["datetime"]
        for m in members:
            t.members.append(Address(m))

    def _member(self, t: Tab, who: Address) -> bool:
        for m in t.members:
            if m == who:
                return True
        return False

    @gl.public.write
    def add_notch(self, tab_id: str, notch_id: str, payer: str, atto: u256,
                  memo: str, evidence_uri: str, evidence_hash: str,
                  claim_kind: str) -> None:
        if tab_id not in self.tabs:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} no such tab")
        t = self.tabs[tab_id]
        payee = gl.message.sender_address          # the biller
        if not self._member(t, payee):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} not a member")
        if notch_id in self.items:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} duplicate notch")
        if claim_kind not in CLAIM_KINDS:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown claim_kind")
        if atto == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} zero amount")
        # 64 lowercase hex, never normalised: hexdigest() is lowercase, so
        # folding case here would hide a real mismatch instead of rejecting it.
        if len(evidence_hash) != 64 or any(
            c not in "0123456789abcdef" for c in evidence_hash
        ):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} bad evidence_hash")
        p = Address(payer)
        if p == payee:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} payer is payee")
        if not self._member(t, p):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} payer not a member")
        self.items[notch_id] = LineItem(
            tab_id=tab_id, payer=p, payee=payee, atto=atto, memo=memo,
            evidence_uri=evidence_uri, evidence_hash=evidence_hash,
            claim_kind=claim_kind, cycle=t.cycle,
        )
        t.notch_ids.append(notch_id)

    @gl.public.view
    def get_notch(self, notch_id: str) -> dict:
        if notch_id not in self.items:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} no such notch")
        n = self.items[notch_id]
        return {"tab_id": n.tab_id, "payer": n.payer.as_hex,
                "payee": n.payee.as_hex, "atto": n.atto, "memo": n.memo,
                "evidence_uri": n.evidence_uri, "evidence_hash": n.evidence_hash,
                "claim_kind": n.claim_kind, "cycle": n.cycle}

    @gl.public.view
    def get_tab(self, tab_id: str) -> dict:
        if tab_id not in self.tabs:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} no such tab")
        t = self.tabs[tab_id]
        return {"creator": t.creator.as_hex, "cycle": t.cycle,
                "cycle_seconds": t.cycle_seconds, "opened_at": t.opened_at,
                "members": [m.as_hex for m in t.members],
                "notch_count": len(t.notch_ids)}

    @gl.public.write
    def close(self, tab_id: str) -> str:
        if tab_id not in self.tabs:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} no such tab")
        t = self.tabs[tab_id]
        if not self._member(t, gl.message.sender_address):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} not a member")

        cycle = int(t.cycle)
        ids = [i for i in t.notch_ids if int(self.items[i].cycle) == cycle]
        if len(ids) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} nothing to close")

        # One signed running total per unordered pair. u256 cannot hold a sign,
        # so the sign lives in the ordering: positive means `lo` owes `hi`.
        pairs: dict[str, int] = {}
        for i in ids:
            n = self.items[i]
            lo, hi = sorted([n.payer.as_hex, n.payee.as_hex])
            signed = int(n.atto) if n.payer.as_hex == lo else -int(n.atto)
            pairs[lo + "|" + hi] = pairs.get(lo + "|" + hi, 0) + signed

        legs = []
        for key in sorted(pairs.keys()):
            amount = pairs[key]
            if amount == 0:
                continue
            lo, hi = key.split("|")
            debtor, creditor = (lo, hi) if amount > 0 else (hi, lo)
            legs.append({"debtor": debtor, "creditor": creditor,
                         "atto": abs(amount)})

        # Content only — no timestamps — so a counterparty can rebuild this
        # preimage off-chain and recompute the hash.
        payload = json.dumps(
            {"tab": tab_id, "cycle": cycle, "notches": sorted(ids), "legs": legs},
            sort_keys=True, separators=(",", ":"),
        )
        sid = f"{tab_id}:{cycle}"
        s = self.statements.get_or_insert_default(sid)
        s.tab_id = tab_id
        s.cycle = u256(cycle)
        s.closed_at = gl.message_raw["datetime"]
        s.closed_by = gl.message.sender_address
        s.statement_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        s.status = STATUS_OPEN
        s.settle_ref = ""
        for i in sorted(ids):
            s.notch_ids.append(i)
        for leg in legs:
            s.legs.append(json.dumps(leg, sort_keys=True, separators=(",", ":")))
        t.cycle = u256(cycle + 1)
        return sid

    @gl.public.view
    def get_statement(self, statement_id: str) -> dict:
        s = self._statement(statement_id)
        return {"tab_id": s.tab_id, "cycle": s.cycle, "closed_at": s.closed_at,
                "closed_by": s.closed_by.as_hex,
                "statement_hash": s.statement_hash, "status": s.status,
                "settle_ref": s.settle_ref,
                "legs": [json.loads(x) for x in s.legs],
                "notch_ids": [x for x in s.notch_ids]}

    def _statement(self, statement_id: str) -> Statement:
        """Every entry point that names a statement id routes through here.

        `TreeMap.__getitem__` raises a bare `KeyError()` with an empty message,
        and spec §5 has validators compare errors by prefix — an empty one
        matches nothing.
        """
        if statement_id not in self.statements:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} no such statement")
        return self.statements[statement_id]

    def _is_final(self, s: Statement) -> bool:
        if s.status in (STATUS_ACCEPTED, STATUS_SETTLED):
            return True
        if s.status == STATUS_DISPUTED:
            # Spec §4 auto-accepts a cycle that is "neither accepted nor
            # disputed". An open dispute has to outlast the window, or the biller
            # waits it out and files a receipt on a statement under judgment.
            # `resolve()` moves the status on, which lets finality resume.
            return False
        now = datetime.datetime.fromisoformat(gl.message_raw["datetime"])
        closed = datetime.datetime.fromisoformat(s.closed_at)
        # timedelta comparison, not total_seconds(): the constraints forbid
        # floats, and this is the comparison that decides finality.
        window = datetime.timedelta(seconds=int(self.dispute_window_seconds))
        return (now - closed) >= window

    @gl.public.view
    def is_final(self, statement_id: str) -> bool:
        return self._is_final(self._statement(statement_id))

    @gl.public.write
    def accept(self, statement_id: str) -> None:
        s = self._statement(statement_id)
        if not self._member(self.tabs[s.tab_id], gl.message.sender_address):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} not a member")
        if s.status != STATUS_OPEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} not open")
        # Spec §4: accept is the *counterparty* agreeing. The closer accepting
        # its own statement would collapse the dispute window to zero at its own
        # discretion — this task's stall-proofing run in reverse.
        if gl.message.sender_address == s.closed_by:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} closer cannot accept")
        s.status = STATUS_ACCEPTED

    @gl.public.write
    def file_settlement(self, statement_id: str, settle_ref: str) -> None:
        s = self._statement(statement_id)
        if not self._member(self.tabs[s.tab_id], gl.message.sender_address):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} not a member")
        if s.status == STATUS_SETTLED:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} already settled")
        if settle_ref == "":
            # "" is also close()'s unset sentinel, so an empty ref would mark a
            # statement settled with no receipt to point at.
            raise gl.vm.UserError(f"{ERROR_EXPECTED} empty settle_ref")
        # `is_final` and not `status == accepted`: an auto-accepted statement
        # never reaches that status, and letting a stalling counterparty block
        # the receipt would undo the whole point of the window.
        if not self._is_final(s):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} not final")
        s.status = STATUS_SETTLED
        s.settle_ref = settle_ref

    @gl.public.write.payable
    def open_dispute(self, statement_id: str, notch_ids: list[str],
                     claim_kind: str, claim: str) -> None:
        s = self._statement(statement_id)
        who = gl.message.sender_address
        if not self._member(self.tabs[s.tab_id], who):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} not a member")
        # The status slot of the guard order carries both messages, because
        # `disputed` is a status *and* the "no existing dispute" rule: this method
        # writes `s.status` and `self.disputes[dispute_id]` together, so the two
        # are equivalent, and a bare `not open` on a re-file would tell the filer
        # nothing about the dispute already under judgment.
        if s.status == STATUS_DISPUTED:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} already disputed")
        if s.status != STATUS_OPEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} not open")
        # `s.status == open` is guaranteed by the two guards above, and for an
        # open statement final and disputable ARE complements — so this asks
        # `_is_final` rather than recomputing the window beside it, where two
        # copies of the rule could drift.
        if self._is_final(s):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} window closed")
        # The bond is native GEN, and this is the only method that custodies
        # value. An overpay is accepted rather than refunded — a refund means an
        # outbound transfer inside intake, which §6 keeps out of every
        # consensus-critical path — so what is recorded below is what was paid.
        if int(gl.message.value) < int(self.bond_atto):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} bond too small")
        # Spec §3: "one or more notch ids". Empty buys a verdict on nothing, and
        # a repeated id inflates the disputed total that Task 5 sums and clamps
        # `adjusted_atto` against, so a duplicate is rejected rather than folded.
        if len(notch_ids) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} no notches")
        if len(set(notch_ids)) != len(notch_ids):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} duplicate notch id")
        in_statement = [x for x in s.notch_ids]
        for i in notch_ids:
            if i not in in_statement:
                raise gl.vm.UserError(f"{ERROR_EXPECTED} notch not in statement")
        # A second pass, deliberately: folding this into the loop above would
        # report whichever guard the *first bad id* trips, not the guard order.
        for i in notch_ids:
            if self.items[i].payer != who:
                # Only the debtor may contest a bill.
                raise gl.vm.UserError(f"{ERROR_EXPECTED} not the payer")
        # Task 9 credits the forfeited bond to a single winner, and notches from
        # two payees have no unambiguous one. Rejecting the filing here is a far
        # smaller diff than pro-rata distribution downstream.
        if len({self.items[i].payee for i in notch_ids}) > 1:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} mixed payees")
        if claim_kind not in CLAIM_KINDS:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown claim_kind")

        # ponytail: one dispute per statement — the id is derived, not counted.
        # Concurrent disputes over one statement would need per-leg locking and
        # buy nothing at demo scale; a DynArray of disputes if a real user asks.
        dispute_id = f"{statement_id}#d"
        # Structural, not incidental: `get_or_insert_default` would overwrite
        # `bond_atto` and *append* to `notch_ids` on a record that already exists,
        # losing a claimant's money with no way to recover it. The status guards
        # above make that unreachable today, but they are a proxy for this key —
        # so the key is checked where the money is written, not one task away.
        if dispute_id in self.disputes:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} already disputed")
        d = self.disputes.get_or_insert_default(dispute_id)
        d.statement_id = statement_id
        d.claimant = who
        d.claim_kind = claim_kind
        d.claim = claim
        d.bond_atto = u256(int(gl.message.value))
        d.status = STATUS_OPEN
        d.outcome = ""
        d.adjusted_atto = u256(0)
        d.evidence_hash_matched = False
        d.rationale = ""
        d.opened_at = gl.message_raw["datetime"]
        for i in notch_ids:
            d.notch_ids.append(i)
        s.status = STATUS_DISPUTED

    def _dispute(self, dispute_id: str) -> Dispute:
        """The statement-side `_statement` guard, for dispute ids.

        Same reason: `TreeMap.__getitem__` raises a bare `KeyError()` carrying an
        empty message, and spec §5 has validators compare errors by prefix.
        """
        if dispute_id not in self.disputes:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} no such dispute")
        return self.disputes[dispute_id]

    @gl.public.view
    def get_dispute(self, dispute_id: str) -> dict:
        d = self._dispute(dispute_id)
        return {"statement_id": d.statement_id, "claimant": d.claimant.as_hex,
                "claim_kind": d.claim_kind, "claim": d.claim,
                "bond_atto": d.bond_atto, "status": d.status,
                "outcome": d.outcome, "adjusted_atto": d.adjusted_atto,
                "evidence_hash_matched": d.evidence_hash_matched,
                "rationale": d.rationale, "opened_at": d.opened_at,
                "notch_ids": [x for x in d.notch_ids],
                "cited": [x for x in d.cited]}
