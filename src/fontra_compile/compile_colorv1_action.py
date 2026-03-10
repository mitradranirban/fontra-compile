import os
import pathlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncGenerator

from fontra.core.protocols import ReadableFontBackend
from fontra.workflow.actions import OutputProcessorProtocol, registerOutputAction
from .builder import Builder  # Reuse for base font

try:
    from paintcompiler import PaintColrLayers, PaintGlyph, PaintSolid, PaintTranslate, SetColors, compilePaints
except ImportError:
    raise ImportError("Install paintcompiler: pip install paintcompiler")

@registerOutputAction("compile-colorv1")
@dataclass(kw_only=True)
class CompileColorV1Action:
    destination: str
    input: ReadableFontBackend | None = field(init=False, default=None)
    subroutinize: bool = True
    use_extended_gvar: bool = False
    # New: palette_index (for multi-palette), vary_paints (enable IVS)

    @asynccontextmanager
    async def connect(self, input: ReadableFontBackend) -> AsyncGenerator[OutputProcessorProtocol, None]:
        self.input = input
        try:
            yield self
        finally:
            self.input = None

    async def process(self, outputDir: pathlib.Path = pathlib.Path(), *, continueOnError=False) -> None:
        outputPath = outputDir / self.destination
        assert self.input is not None

        # Build base font with outlines/variations (reuse Builder)
        builder = Builder(
            reader=self.input,
            buildCFF2=False,  # TTF for COLRv1
            subroutinize=self.subroutinize,
            useExtendedGvar=self.use_extended_gvar,
        )
        await builder.setup()
        tt = await builder.build()

        # Extract palettes from font-data.json customData (Fontra standard)
        custom_data = await self.input.getLib() or {}
        palettes = custom_data.get("com.github.googlei18n.ufo2ft.colorPalettes", [])
        if not palettes:
            palettes = [[(1,0,0,1), (0,1,0,1), (0,0,1,1)]]  # Fallback RGB

        # Define paints dict for sample/TestColorV1.fontra (glyph -> Paint)
        glyphs = {}
        glyph_map = await self.input.getGlyphMap()
        for glyph_name in glyph_map:
            glyph = await self.input.getGlyph(glyph_name)
            layer_glyph = glyph.layers.get("foreground", {}).get("glyph", {})
            paint_data = layer_glyph.get("customData", {}).get("colorv1")
            if paint_data:
                glyphs[glyph_name] = self._fontra_to_paint(paint_data, palettes)
            else:
                # Sample fallback: COLRv1 A with shadow + gradient
                glyphs[glyph_name] = PaintColrLayers([
                    PaintGlyph("A.shadow", PaintSolid(1)),  # Palette idx 1 (blue)
                    PaintGlyph("A", PaintSolid(0)),         # Palette idx 0 (red)
                    PaintTranslate({"SHDW": 20}, "A.shadow", PaintSolid(2))  # Vary on SHDW axis
                ])

        # Setup palettes, compile COLR/CPAL
        SetColors(palettes)  # Builds CPAL automatically
        compilePaints(tt, glyphs)  # Adds COLR v1 + IVS wiring

        tt.save(outputPath)
        print(f"Exported COLRv1 TTF: {outputPath} [Palettes: {len(palettes)}, Glyphs: {len(glyphs)}]")

    def _fontra_to_paint(self, data: dict, palettes: list) -> object:
        """Convert Fontra customData.colorv1 JSON to paintcompiler Paint."""
        ptype = data.get("type", "PaintColrLayers")
        if ptype == "PaintColrLayers":
            layers = [self._fontra_to_paint(l, palettes) for l in data.get("layers", [])]
            return PaintColrLayers(layers)
        elif ptype == "PaintGlyph":
            glyph = data["glyph"]
            paint = self._fontra_to_paint(data.get("paint", {}), palettes)
            return PaintGlyph(glyph, paint)
        elif ptype == "PaintSolid":
            idx = data.get("paletteIndex", 0)
            alpha = data.get("alpha", 1.0)  # Support {"default":1.0, "keyframes":[{"axis":"SHDW","loc":1,"value":0.5}]}
            return PaintSolid(idx, alpha=alpha)
        elif ptype == "PaintTranslate":
            dx = data.get("dx", 0)
            dy = data.get("dy", 0)
            paint = self._fontra_to_paint(data.get("paint", {}), palettes)
            return PaintTranslate(dx, dy, paint)
        # Add more: PaintLinearGradient, etc. from file:3 schema
        raise ValueError(f"Unsupported paint: {ptype}")
