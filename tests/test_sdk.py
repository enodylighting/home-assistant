"""Smoke tests for the installed enody-py binary, without device I/O."""

from custom_components.enody.api import _load_enody


def test_sdk_token_round_trip() -> None:
    """The real SDK can restore and serialize a stored pairing token."""
    enody = _load_enody()
    data = {
        "host_id": "98a316b1-bf8c-4000-8000-000000000001",
        "key_id": "98a316b1-bf8c-4000-8000-000000000002",
        "data": [7] * 32,
    }

    assert enody.Token.from_dict(data).to_dict() == data


def test_sdk_color_and_transition_types() -> None:
    """Native control types load on each supported Python test runtime."""
    enody = _load_enody()
    configurations = (
        enody.Configuration.blackbody(3000),
        enody.Configuration.chromatic(0.3, 0.4),
        enody.Configuration.flux(),
    )

    for configuration in configurations:
        assert isinstance(configuration, enody.Configuration)
        transition = enody.Transition.linear(
            configuration, enody.Flux.relative(0.5), 2.5
        )
        assert isinstance(transition, enody.Transition)
