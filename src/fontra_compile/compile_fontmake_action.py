import itertools
import os
import pathlib
import tempfile
from contextlib import aclosing, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import AsyncGenerator, ContextManager

from fontmake.__main__ import main as fontmake_main
from fontra.backends import getFileSystemBackend, newFileSystemBackend
from fontra.backends.copy import copyFont
from fontra.core.protocols import ReadableFontBackend
from fontra.workflow.actions import (
    OutputProcessorProtocol,
    registerOutputAction,
)
from fontTools.designspaceLib import DesignSpaceDocument
from fontTools.ufoLib import UFOReaderWriter


@registerOutputAction("compile-fontmake")
@dataclass(kw_only=True)
class CompileFontMakeAction:
    destination: str
    options: dict[str, str] = field(default_factory=dict)
    setOverlapSimpleFlag: bool = False
    addMinimalGaspTable: bool = False
    ufoTempDir: str | None = None
    input: ReadableFontBackend | None = field(init=False, default=None)

    @asynccontextmanager
    async def connect(
        self, input: ReadableFontBackend
    ) -> AsyncGenerator[OutputProcessorProtocol, None]:
        self.input = input
        try:
            yield self
        finally:
            self.input = None

    async def process(
        self, outputDir: os.PathLike = pathlib.Path(), *, continueOnError=False
    ) -> None:
        assert self.input is not None
        outputDir = pathlib.Path(outputDir)
        outputFontPath = outputDir / self.destination

        axes = await self.input.getAxes()
        isVariable = bool(axes.axes)

        tempDirContext: ContextManager

        if self.ufoTempDir:
            tempDirContext = nullcontext(enter_result=self.ufoTempDir)
        else:
            tempDirContext = tempfile.TemporaryDirectory()

        with tempDirContext as tmpDirName:
            tmpDir = pathlib.Path(tmpDirName)

            fileName = "temp." + ("designspace" if isVariable else "ufo")
            sourcePath = tmpDir / fileName

            dsBackend = newFileSystemBackend(sourcePath)

            if self.setOverlapSimpleFlag:
                assert hasattr(dsBackend, "setOverlapSimpleFlag")
                dsBackend.setOverlapSimpleFlag = True

            async with aclosing(dsBackend):
                await copyFont(self.input, dsBackend, continueOnError=continueOnError)
            # add function to prevent stripping of color palette data
            _fixColorLibKeys(self.input, tmpDir)    

            if isVariable:
                addInstances(sourcePath)
            addGlyphOrder(sourcePath)

            if self.addMinimalGaspTable:
                addMinimalGaspTable(sourcePath)

            extraArguments = []
            for option, value in self.options.items():
                extraArguments.append(f"--{option}")
                if value:
                    extraArguments.append(value)

            self.compileFromDesignspace(sourcePath, outputFontPath, extraArguments)

    def compileFromDesignspace(self, sourcePath, outputFontPath, extraArguments):
        isVariable = sourcePath.suffix == ".designspace"
        outputType = (
            ("variable-cff2" if isVariable else "otf")
            if outputFontPath.suffix.lower() != ".ttf"
            else ("variable" if isVariable else "ttf")
        )
        arguments = [
            "-u" if sourcePath.suffix == ".ufo" else "-m",
            os.fspath(sourcePath),
            "-o",
            outputType,
            "--output-path",
            os.fspath(outputFontPath),
        ]

        fontmake_main(arguments + extraArguments)


def addInstances(designspacePath):
    dsDoc = DesignSpaceDocument.fromfile(designspacePath)
    if dsDoc.instances:
        # There are instances
        return

    # We will make up instances based on the axis value labels

    sortOrder = {
        "wght": 0,
        "wdth": 1,
        "ital": 2,
        "slnt": 3,
    }
    axes = sorted(dsDoc.axes, key=lambda axis: sortOrder.get(axis.tag, 100))

    elidedFallbackName = dsDoc.elidedFallbackName or "Regular"
    dsDoc.elidedFallbackName = elidedFallbackName

    axisLabels = [
        [
            (axis.name, label.name if not label.elidable else None, label.userValue)
            for label in axis.axisLabels
        ]
        for axis in axes
        if axis.axisLabels
    ]

    axesByName = {axis.name: axis for axis in dsDoc.axes}

    for items in itertools.product(*axisLabels):
        location = {name: value for (name, valueLabel, value) in items}
        nameParts = [valueLabel for (name, valueLabel, value) in items if valueLabel]
        if not nameParts:
            nameParts = [elidedFallbackName]
        styleName = " ".join(nameParts)

        # TODO: styleName seems to be ignored, and the instance names are derived
        # from axis labels elsewhere. Figure out where this happens.
        location = mapLocationForward(location, axesByName)
        dsDoc.addInstanceDescriptor(
            familyName="Testing", styleName=styleName, location=location
        )

    dsDoc.write(designspacePath)


def mapLocationForward(location, axes):
    return {name: axes[name].map_forward(value) for name, value in location.items()}


_firstFourGIDS = {gn: gid for gid, gn in enumerate([".notdef", ".null", "CR", "space"])}
_nextGID = len(_firstFourGIDS)


def _glyphSortKeyFunc(glyphName):
    return (_firstFourGIDS.get(glyphName, _nextGID), glyphName)


def addGlyphOrder(designspacePath):
    backend = getFileSystemBackend(designspacePath)
    dsDoc = backend.dsDoc
    defaultSource = dsDoc.findDefault()
    ufo = UFOReaderWriter(defaultSource.path)
    lib = ufo.readLib()
    if "public.glyphOrder" not in lib:
        glyphSet = ufo.getGlyphSet()
        lib["public.glyphOrder"] = sorted(glyphSet.keys(), key=_glyphSortKeyFunc)
        ufo.writeLib(lib)


class UFOFontInfo:
    pass


def addMinimalGaspTable(designspacePath):
    backend = getFileSystemBackend(designspacePath)
    dsDoc = backend.dsDoc
    defaultSource = dsDoc.findDefault()
    ufo = UFOReaderWriter(defaultSource.path)
    fontInfo = UFOFontInfo()
    ufo.readInfo(fontInfo)
    fontInfo.openTypeGaspRangeRecords = [
        {"rangeMaxPPEM": 0xFFFF, "rangeGaspBehavior": [0, 1, 2, 3]}
    ]
    ufo.writeInfo(fontInfo)
def _fixColorLibKeys(sourceBackend, tmpDir: pathlib.Path):
    """
    copyFont copies glyph outlines and per-glyph colorLayerMapping correctly,
    but does not copy colorPalettes to lib.plist or color layer glyph names
    to public.glyphOrder. Without these, ufo2ft's ExplodeColorLayerGlyphs
    filter never fires and fontmake produces a monochrome font.
    """
    import plistlib
    from fontTools.designspaceLib import DesignSpaceDocument

    PALETTES_KEY = "com.github.googlei18n.ufo2ft.colorPalettes"

    # Get source UFO paths
    sourcePath = getattr(sourceBackend, "path", None)
    if sourcePath is None:
        return
    sourcePath = pathlib.Path(sourcePath)

    if sourcePath.suffix == ".ufo":
        sourceUFOs = [sourcePath]
        tempUFOs = list(tmpDir.glob("*.ufo"))
    elif sourcePath.suffix == ".designspace":
        dsDoc = DesignSpaceDocument.fromfile(sourcePath)
        sourceUFOs = [pathlib.Path(src.path) for src in dsDoc.sources]
        tempUFOs = list(tmpDir.glob("*.ufo"))
    else:
        return

    for sourceUFO in sourceUFOs:
        if not sourceUFO.exists():
            continue

        sourceLibPath = sourceUFO / "lib.plist"
        if not sourceLibPath.exists():
            continue
        sourceLib = plistlib.loads(sourceLibPath.read_bytes())

        # Match source UFO to its corresponding temp UFO by name stem
        tempUFO = next(
            (t for t in tempUFOs if sourceUFO.stem in t.stem), None
        )
        if tempUFO is None:
            continue

        tempLibPath = tempUFO / "lib.plist"
        tempLib = plistlib.loads(tempLibPath.read_bytes())
        modified = False

        # 1. Inject colorPalettes — required for ExplodeColorLayerGlyphs to fire
        if PALETTES_KEY in sourceLib and PALETTES_KEY not in tempLib:
            tempLib[PALETTES_KEY] = sourceLib[PALETTES_KEY]
            modified = True

        # 2. Add color layer glyph names to public.glyphOrder
        #    so fontmake compiles them into glyf/CFF
        layerContentsPath = tempUFO / "layercontents.plist"
        if layerContentsPath.exists():
            layerContents = plistlib.loads(layerContentsPath.read_bytes())
            glyphOrder = tempLib.get("public.glyphOrder", [])
            for layerName, layerDir in layerContents:
                if layerName == "public.default":
                    continue
                contentsPath = tempUFO / layerDir / "contents.plist"
                if contentsPath.exists():
                    for glyphName in plistlib.loads(contentsPath.read_bytes()):
                        if glyphName not in glyphOrder:
                            glyphOrder.append(glyphName)
                            modified = True
            tempLib["public.glyphOrder"] = glyphOrder

        if modified:
            tempLibPath.write_bytes(plistlib.dumps(tempLib))
