import pathlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncGenerator

from fontmake.__main__ import main as fontmake_main
from fontra.core.protocols import ReadableFontBackend
from fontra.workflow.actions import registerOutputAction


@registerOutputAction("compile-fontmake")
@dataclass(kw_only=True)
class CompileFontMakeAction:
    destination: str
    options: dict[str, str] = field(default_factory=dict)
    ufoTempDir: str | None = None

    @asynccontextmanager
    async def connect(
        self, input: ReadableFontBackend
    ) -> AsyncGenerator["CompileFontMakeAction", None]:
        self.input = input
        try:
            yield self
        finally:
            self.input = None

    async def process(
        self, outputDir: pathlib.Path = pathlib.Path(), *, continueOnError=False
    ) -> None:
        outputFontPath = outputDir / self.destination

        # Unwrap to find source path
        backend = self.input
        source_path = None
        while backend is not None:
            source_path = getattr(backend, "path", None)
            if source_path:
                break
            backend = getattr(backend, "input", None)

        source_path = pathlib.Path(source_path)
        is_variable = bool((await self.input.getAxes()).axes)

        fontmake_main(
            [
                "-m" if source_path.suffix == ".designspace" else "-u",
                str(source_path),
                "-o",
                "variable" if is_variable else "ttf",
                "--output-path",
                str(outputFontPath),
                *[f"--{k}={v}" for k, v in self.options.items()],
            ]
        )
