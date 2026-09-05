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


class Notch(gl.Contract):
    bond_atto: u256

    def __init__(self, bond_atto: u256) -> None:
        self.bond_atto = bond_atto

    @gl.public.view
    def get_bond_atto(self) -> int:
        return self.bond_atto
