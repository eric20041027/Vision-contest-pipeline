"""Materialize: decode views once into a portable npy / png cache (spec §6.3, §15.4)."""

from vcp.data.materialize.decoders import DECODERS, Decoded, Decoder, decoder_for, get_decoder

__all__ = ["DECODERS", "Decoded", "Decoder", "decoder_for", "get_decoder"]
