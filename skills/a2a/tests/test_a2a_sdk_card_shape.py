"""The SDK AgentCard construction must survive every card shape an SDK exports.

a2a-sdk 1.1.2 exports the PROTOBUF AgentCard, which has no ``url`` field and
raises ValueError (not TypeError) for an unknown kwarg. Passing ``url`` there
raised inside ``_build_app()`` at import time, so the host-supervised companion
crashed on every start and never bound its port. These tests pin the two
defenses: descriptor-based field filtering, and a minimal-card retry.
"""

import importlib.util
import os
import pathlib

_DAEMON = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "a2a_daemon.py"


def _load_daemon():
    os.environ["OUROBOROS_SKILL_STATE_DIR"] = "/tmp/a2a_test_state_dir_nonexistent"
    os.environ["A2A_TOOLS_REFRESHER"] = "0"
    spec = importlib.util.spec_from_file_location("a2ad_cardshape_under_test", _DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time.sleep = lambda *_a, **_k: None
    return mod


class _ProtoLikeCard:
    """Mimics a protobuf message: no model_fields, ValueError on unknown kwargs."""

    class DESCRIPTOR:  # noqa: N801 - mirrors the protobuf attribute name
        fields_by_name = {
            "name": None,
            "description": None,
            "version": None,
            "protocol_version": None,
            "preferred_transport": None,
            "capabilities": None,
            "default_input_modes": None,
            "default_output_modes": None,
            "skills": None,
        }

    def __init__(self, **kwargs):
        unknown = set(kwargs) - set(self.DESCRIPTOR.fields_by_name)
        if unknown:
            raise ValueError(f'Protocol message AgentCard has no "{sorted(unknown)[0]}" field.')
        self.kwargs = kwargs


class _StrictPydanticLikeCard:
    """A card that declares no field metadata at all and rejects extras."""

    _ALLOWED = {"name", "description", "version", "capabilities", "skills"}

    def __init__(self, **kwargs):
        unknown = set(kwargs) - self._ALLOWED
        if unknown:
            raise TypeError(f"unexpected keyword argument {sorted(unknown)[0]!r}")
        self.kwargs = kwargs


def _install_card_class(mod, card_cls):
    mod.AgentCard = card_cls
    mod.AgentCapabilities = lambda **kw: {"capabilities": kw}
    mod.AgentSkill = lambda **kw: kw
    mod._LAST_GOOD_TOOLS = []
    mod._fetch_identity = lambda: {"name": "", "description": ""}
    mod._fetch_tool_schemas = lambda *a, **k: []


def test_proto_card_without_url_field_still_builds():
    mod = _load_daemon()
    _install_card_class(mod, _ProtoLikeCard)
    card = mod._sdk_agent_card()
    assert "url" not in card.kwargs, "url must be filtered out for a proto card"
    assert card.kwargs["name"] == "Ouroboros"
    # The transport fields the proto card DOES accept are still carried.
    assert card.kwargs["protocol_version"] == "0.3.0"
    assert card.kwargs["skills"], "curated capability skills must reach the SDK card"


def test_card_rejecting_everything_falls_back_to_a_minimal_card():
    mod = _load_daemon()
    _install_card_class(mod, _StrictPydanticLikeCard)
    card = mod._sdk_agent_card()
    assert set(card.kwargs) <= {"name", "description", "version", "capabilities", "skills"}
    assert card.kwargs["name"] == "Ouroboros"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL_OK ({len(fns)} tests)")


if __name__ == "__main__":
    _run_all()
