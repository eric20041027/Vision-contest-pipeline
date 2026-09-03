"""Materialize: decode views once into a portable npy / png cache (spec §6.3, §15.4)."""

from vcp.data.materialize.base import MODES, MaterializeResult, MaterializeSpec
from vcp.data.materialize.decoders import DECODERS, Decoded, Decoder, decoder_for, get_decoder
from vcp.data.materialize.run import materialize

__all__ = [
    "DECODERS",
    "Decoded",
    "Decoder",
    "MODES",
    "MaterializeResult",
    "MaterializeSpec",
    "decoder_for",
    "get_decoder",
    "materialize",
]
