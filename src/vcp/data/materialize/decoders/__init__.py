"""Decoder registry. Importing this package registers the built-in decoders."""

from vcp.data.materialize.decoders.base import (
    DECODERS,
    Decoded,
    Decoder,
    decoder_for,
    get_decoder,
    register_decoder,
)
from vcp.data.materialize.decoders.dicom import DicomDecoder
from vcp.data.materialize.decoders.image import ImageDecoder

register_decoder(ImageDecoder())
register_decoder(DicomDecoder())

__all__ = [
    "DECODERS",
    "Decoded",
    "Decoder",
    "DicomDecoder",
    "ImageDecoder",
    "decoder_for",
    "get_decoder",
    "register_decoder",
]
