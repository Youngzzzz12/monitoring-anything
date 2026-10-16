"""Read-only research adapters; none launch a compiler or solver."""

from . import abaqus, declarative, isale, latex, script

ADAPTERS = {"latex": latex.collect, "abaqus": abaqus.collect, "isale": isale.collect}
# Engines are selected by adapter.engine, independently of the display kind.
ENGINES = {"declarative": declarative.collect, "script": script.collect}
