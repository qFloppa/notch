# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

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


class Notch(gl.Contract):
    bond_atto: u256
    tabs: TreeMap[str, Tab]
    items: TreeMap[str, LineItem]

    def __init__(self, bond_atto: u256) -> None:
        self.bond_atto = bond_atto

    @gl.public.view
    def get_bond_atto(self) -> int:
        return self.bond_atto

    @gl.public.write
    def open_tab(self, tab_id: str, members: list[str], cycle_seconds: u256) -> None:
        if tab_id in self.tabs:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} tab exists")
        if len(members) < 2:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} need two members")
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
        n = self.items[notch_id]
        return {"tab_id": n.tab_id, "payer": n.payer.as_hex,
                "payee": n.payee.as_hex, "atto": n.atto, "memo": n.memo,
                "evidence_uri": n.evidence_uri, "evidence_hash": n.evidence_hash,
                "claim_kind": n.claim_kind, "cycle": n.cycle}

    @gl.public.view
    def get_tab(self, tab_id: str) -> dict:
        t = self.tabs[tab_id]
        return {"creator": t.creator.as_hex, "cycle": t.cycle,
                "cycle_seconds": t.cycle_seconds, "opened_at": t.opened_at,
                "members": [m.as_hex for m in t.members],
                "notch_count": len(t.notch_ids)}
